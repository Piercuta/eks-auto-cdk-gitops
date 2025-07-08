from aws_cdk import Stack, RemovalPolicy
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as codepipeline_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_logs as logs
from config.base_config import InfrastructureConfig
from constructs import Construct
from aws_cdk import aws_eks_v2_alpha as eks_alpha
from aws_cdk import CfnJson
from aws_cdk import custom_resources as cr


class CICDBackendStack(Stack):
    def __init__(self,
                 scope: Construct,
                 construct_id: str,
                 eks_cluster: eks_alpha.Cluster,
                 db_endpoint: str,
                 db_secret_arn: str,
                 config: InfrastructureConfig,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.eks_cluster = eks_cluster
        self.fastapi_codebuild_role = self._create_fastapi_codebuild_role()
        self.db_endpoint = db_endpoint
        self.db_secret_arn = db_secret_arn
        self.service_account_role = self._create_service_account_role()
        self.ssm_alb_dns_parameter_name = f"/{self.config.project_name}/{self.config.env_name_str}/fastapi/ingress_dns"
        self._create_backend_pipeline()
        self.config.add_stack_global_tags(self)

    def _create_service_account_role(self):
        # oidc_provider = ""self.eks_cluster.open_id_connect_provider
        # oidc_issuer = "toto"  # oidc_provider.open_id_connect_provider_issuer

        # condition = CfnJson(
        #     self, "OIDCCondition",
        #     value={
        #         f"{oidc_issuer}:sub": "system:serviceaccount:fastapi:fastapi-sa"
        #     }
        # )

        service_account_role = iam.Role(
            self, "FastApiServiceAccountRole",
            role_name=self.config.prefix("fastapi-serviceaccount-role"),
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com")
        )

        # Ajouter la policy avec permissions (à affiner avec least privilege)
        service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "ssm:GetParameter",
                    "kms:Decrypt",
                    "rds:*"
                ],
                resources=["*"]
            )
        )

        return service_account_role

    def _create_fastapi_codebuild_role(self) -> iam.Role:
        """Create IAM role for FastAPI CodeBuild."""
        role = iam.Role(
            self, "FastApiCodeBuildRole",
            role_name=self.config.prefix("fastapi-codebuild-role"),
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSCodeBuildDeveloperAccess"),
            ],
            description="IAM role for FastAPI CodeBuild service",
        )

        # Add EKS and ECR permissions
        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "eks:*",
                "ecr:*",
                "logs:*",
                "cloudwatch:*",
                "s3:*",
                "iam:GetRole",
                "iam:ListRoles",
                "iam:PassRole",
                "kms:*",
                "rds:*",
                "secretsmanager:*",
                "ssm:*",
            ],
            resources=["*"]
        ))

        return role

    def _create_backend_pipeline(self):
        backend_artifact_bucket = s3.Bucket(
            self, "BackendArtifactBucket",
            bucket_name=self.config.prefix("backend-pipeline-artifacts"),
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        )

        # Create log group for backend build
        backend_log_group = logs.LogGroup(
            self, "BackendBuildLogGroup",
            log_group_name=f"/{
                self.config.project_name}/{
                self.config.env_name_str}/codebuild/{
                self.config.prefix('backend-build')}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY
        )

        repository = ecr.Repository.from_repository_name(
            self, "BackendRepository",
            repository_name=self.config.backend.ecr_repository_name
        )

        # Dictionnaire simple des variables
        env_vars = {
            "SERVICE_ACCOUNT_ROLE_ARN": self.service_account_role.role_arn,
            "ECR_REPOSITORY_URI": repository.repository_uri,
            "BRANCH_NAME": self.config.cicd_backend.github_backend_branch,
            "CLUSTER_NAME": "toto",  # self.eks_cluster.cluster_name,
            "FASTAPI_IMAGE": f"{repository.repository_uri}:{self.config.backend.ecr_image_tag}",
            "ASG_MIN_CAPACITY": str(self.config.backend.auto_scaling_min_capacity),
            "ASG_MAX_CAPACITY": str(self.config.backend.auto_scaling_max_capacity),
            "ASG_DESIRED_CAPACITY": str(self.config.backend.desired_count),
            "CERTIFICATE_ARN": self.config.backend.certificate_arn,
            "DOMAIN_NAME": self.config.dns.backend_domain_name,
            "SSM_PARAMETER_NAME": self.ssm_alb_dns_parameter_name,
            # Environment variables for the backend pod container
            "ENV_NAME": self.config.env_name_str,
            "PROJECT_NAME": self.config.project_name,
            "POSTGRES_SERVER": self.db_endpoint,
            "POSTGRES_USER": self.config.database.master_username,
            "FRONTEND_HOST": f'https://{self.config.frontend.domain_name}',
            "AWS_REGION": self.config.aws.region_str,
            "AWS_SECRET_ARN": self.db_secret_arn
        }

        # Ajouter les variables spécifiques du backend s'il y en a
        env_vars.update(self.config.backend.container_env_vars)

        # Convertir le dict en BuildEnvironmentVariable
        codebuild_env_vars = {
            key: codebuild.BuildEnvironmentVariable(value=str(value))
            for key, value in env_vars.items()
        }

        # Créer le projet CodeBuild
        build_project = codebuild.PipelineProject(
            self, "BackendBuild",
            project_name=self.config.prefix("backend-build"),
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec-eks.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,
                environment_variables=codebuild_env_vars
            ),
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=backend_log_group,
                    prefix=self.config.prefix('backend-build')
                )
            ),
            role=self.fastapi_codebuild_role
        )

        # Grant permissions to CodeBuild
        repository.grant_pull_push(build_project)
        # Add ECS permissions
        build_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "eks:*"
                ],
                resources=[
                    f"arn:aws:eks:{self.config.aws.region_str}:{self.config.aws.account}:cluster/toto"
                    # self.eks_cluster.cluster_name
                ]
            )
        )

        build_project.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:PutParameter"
                ],
                resources=["*"]
            )
        )

        # Create Pipeline
        pipeline = codepipeline.Pipeline(
            self, "BackendPipeline",
            pipeline_name=self.config.prefix("backend-pipeline"),
            artifact_bucket=backend_artifact_bucket
        )

        # Source stage
        source_output = codepipeline.Artifact()
        source_action = codepipeline_actions.CodeStarConnectionsSourceAction(
            action_name="GitHub_Source",
            owner=self.config.cicd_backend.github_owner,
            repo=self.config.cicd_backend.github_backend_repo,
            branch=self.config.cicd_backend.github_backend_branch,
            connection_arn=self.config.cicd_backend.github_connection_arn,
            output=source_output
        )
        pipeline.add_stage(
            stage_name="Source",
            actions=[source_action]
        )

        # Build stage
        build_output = codepipeline.Artifact()
        build_action = codepipeline_actions.CodeBuildAction(
            action_name="Build",
            project=build_project,
            input=source_output,
            outputs=[build_output]
        )
        pipeline.add_stage(
            stage_name="Build",
            actions=[build_action]
        )

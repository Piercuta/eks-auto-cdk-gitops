from aws_cdk import Stack, RemovalPolicy, Duration
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as codepipeline_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_logs as logs
from aws_cdk import aws_eks as eks
from config.base_config import InfrastructureConfig
from constructs import Construct


class CICDK8sDeployStack(Stack):
    """
    CI/CD Stack for Kubernetes deployment with ArgoCD.

    Creates a complete pipeline that:
    1. Sources code from a Git repository
    2. Builds and deploys Kubernetes manifests
    3. Installs and configures ArgoCD
    4. Deploys applications via ArgoCD
    """

    def __init__(self,
                 scope: Construct,
                 construct_id: str,
                 config: InfrastructureConfig,
                 eks_cluster_name: str,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.eks_cluster_name = eks_cluster_name
        self._create_k8s_deploy_pipeline()
        self.config.add_stack_global_tags(self)

    def _create_k8s_deploy_pipeline(self):
        """Create the complete Kubernetes deployment pipeline with ArgoCD."""

        # Create S3 buckets for pipeline artifacts
        k8s_deploy_artifact_bucket = s3.Bucket(
            self, "K8sDeployArtifactBucket",
            bucket_name=self.config.prefix("k8s-deploy-pipeline-artifacts"),
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        )

        # Create IAM role for CodeBuild
        codebuild_role = self._create_codebuild_role()

        # Create CodeBuild project for Kubernetes deployment
        k8s_deploy_project = self._create_k8s_deploy_project(
            codebuild_role,
            k8s_deploy_artifact_bucket
        )

        # Create CodePipeline
        # self._create_pipeline(
        #     k8s_deploy_project,
        #     k8s_deploy_artifact_bucket
        # )

    def _create_codebuild_role(self) -> iam.Role:
        """Create IAM role for CodeBuild with necessary permissions."""

        role = iam.Role(
            self, "K8sDeployCodeBuildRole",
            role_name=self.config.prefix("k8s-deploy-codebuild-role"),
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            description="IAM role for Kubernetes deployment CodeBuild project"
        )

        # Add managed policies
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSClusterPolicy")
        )
        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy")
        )

        # Add custom policies for EKS and deployment operations
        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "eks:DescribeCluster",
                "eks:ListClusters",
                "eks:AccessKubernetesApi",
                "eks:DescribeNodegroup",
                "eks:ListNodegroups",
                "ec2:DescribeInstances",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeSubnets",
                "ec2:DescribeVpcs",
                "iam:GetRole",
                "iam:ListRoles",
                "iam:PassRole",
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket",
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "kms:Decrypt",
                "kms:GenerateDataKey",
                "secretsmanager:GetSecretValue",
                "ssm:GetParameter",
                "ssm:GetParameters"
            ],
            resources=["*"]
        ))

        access_entry = eks.CfnAccessEntry(
            self, "AccessEntry1",
            cluster_name=self.eks_cluster_name,
            principal_arn=role.role_arn,
            access_policies=[
                eks.CfnAccessEntry.AccessPolicyProperty(
                    access_scope=eks.CfnAccessEntry.AccessScopeProperty(
                        type="cluster"
                    ),
                    policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
                )
            ],
            type="STANDARD"
        )

        access_entry.node.add_dependency(role)

        return role

    def _create_k8s_deploy_project(self,
                                   codebuild_role: iam.Role,
                                   artifact_bucket: s3.Bucket) -> codebuild.Project:
        """Create CodeBuild project for Kubernetes deployment with ArgoCD."""

        # Create log group for CodeBuild
        log_group = logs.LogGroup(
            self, "K8sDeployLogGroup",
            log_group_name=f"/aws/codebuild/{self.config.prefix('k8s-deploy-project')}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY
        )

        project = codebuild.Project(
            self, "K8sDeployProject",
            project_name=self.config.prefix("k8s-deploy-project"),
            role=codebuild_role,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,
                environment_variables={
                    "CLUSTER_NAME": codebuild.BuildEnvironmentVariable(
                        value=self.eks_cluster_name
                    ),
                    "AWS_REGION": codebuild.BuildEnvironmentVariable(
                        value=self.config.aws.region_str
                    ),
                    "ENVIRONMENT": codebuild.BuildEnvironmentVariable(
                        value=self.config.env_name_str
                    ),
                    "PROJECT_NAME": codebuild.BuildEnvironmentVariable(
                        value=self.config.project_name
                    )
                }
            ),
            timeout=Duration.minutes(30),
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=log_group,
                    prefix=self.config.prefix('k8s-deploy')
                )
            ),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "install": {
                        "runtime-versions": {
                            "python": "3.10",
                            "nodejs": "18"
                        },
                        "commands": [
                            "echo Installing kubectl...",
                            "curl -LO https://storage.googleapis.com/kubernetes-release/release/$(curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl",
                            "chmod +x ./kubectl",
                            "mv ./kubectl /usr/local/bin/",
                            "curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
                        ]
                    },
                    "pre_build": {
                        "commands": [
                            "echo Configuring kubectl for EKS cluster...",
                            "aws eks update-kubeconfig --name $CLUSTER_NAME --region $AWS_REGION",
                            "echo Cloning GitOps repository...",
                            "git clone https://github.com/Piercuta/eks-auto-cdk-gitops.git || echo 'Repository already exists'",
                            "cd eks-auto-cdk-gitops/git-ops"
                        ]
                    },
                    "build": {
                        "commands": [
                            "echo Applying AWS auth configmap...",
                            "kubectl create namespace argocd || true",
                            # "kubectl apply -f apps/argocd-bootstrap.yaml",
                            "kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml",
                            "kubectl apply -f apps/apps-bootstrap.yaml",
                            "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d && echo"
                        ]
                    },
                },
                "artifacts": {
                    "files": [
                        "**/*"
                    ],
                    "name": "k8s-deploy-artifacts"
                }
            }
            )
        )

        return project

    def _create_pipeline(self,
                         deploy_project: codebuild.PipelineProject,
                         artifact_bucket: s3.Bucket) -> codepipeline.Pipeline:
        """Create CodePipeline for the Kubernetes deployment."""

        # Create pipeline role
        pipeline_role = iam.Role(
            self, "K8sDeployPipelineRole",
            role_name=self.config.prefix("k8s-deploy-pipeline-role"),
            assumed_by=iam.ServicePrincipal("codepipeline.amazonaws.com"),
            description="IAM role for Kubernetes deployment pipeline"
        )

        # Add permissions for pipeline
        pipeline_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:PutObject",
                "s3:GetBucketVersioning"
            ],
            resources=[
                artifact_bucket.bucket_arn,
                f"{artifact_bucket.bucket_arn}/*"
            ]
        ))

        pipeline_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "codebuild:BatchGetBuilds",
                "codebuild:StartBuild"
            ],
            resources=[deploy_project.project_arn]
        ))

        # Create build artifact
        build_artifact = codepipeline.Artifact("BuildArtifact")

        # Create the pipeline
        pipeline = codepipeline.Pipeline(
            self, "K8sDeployPipeline",
            pipeline_name=self.config.prefix("k8s-deploy-pipeline"),
            role=pipeline_role,
            artifact_bucket=artifact_bucket,
            stages=[
                codepipeline.StageProps(
                    stage_name="Deploy",
                    actions=[
                        codepipeline_actions.CodeBuildAction(
                            action_name="DeployToK8s",
                            project=deploy_project,
                            outputs=[build_artifact],
                            input=None
                        )
                    ]
                )
            ]
        )

        # Add CloudFormation outputs

        return pipeline

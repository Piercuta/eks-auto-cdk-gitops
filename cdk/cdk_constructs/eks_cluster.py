from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_eks as eks,
    aws_codebuild as codebuild,
    Tags,
    Duration,
    RemovalPolicy,
    CfnOutput,
    CfnTag
)
from typing import List, Optional
from config.base_config import InfrastructureConfig
from constructs import Construct
import json


class EksCluster(Construct):
    """
    EKS Cluster construct for GitOps architecture.

    Creates a minimal EKS cluster with essential addons (kube-proxy, coredns, vpc-cni, pod-identity-agent).
    Additional addons and workloads will be managed via ArgoCD.
    Includes IAM roles for future services like AWS Load Balancer Controller,
    Karpenter, and other EKS addons.
    """

    def __init__(self, scope: Construct, id: str,
                 vpc: ec2.Vpc,
                 public_subnets: List[ec2.ISubnet],
                 eks_private_nat_subnets: List[ec2.ISubnet],
                 eks_fastapi_sg: ec2.SecurityGroup,
                 eks_cluster_additional_sg: ec2.SecurityGroup,
                 db_endpoint: str,
                 db_secret_arn: str,
                 config: InfrastructureConfig,
                 **kwargs) -> None:
        super().__init__(scope, id)

        self.vpc = vpc
        self.public_subnets = public_subnets
        self.eks_private_nat_subnets = eks_private_nat_subnets
        self.eks_fastapi_sg = eks_fastapi_sg
        self.eks_cluster_additional_sg = eks_cluster_additional_sg
        self.db_endpoint = db_endpoint
        self.db_secret_arn = db_secret_arn
        self.config = config

        self.cluster_name = self.config.prefix("eks-cluster")

        # Create IAM roles first
        self.cluster_role = self._create_cluster_role()
        self.auto_node_role = self._create_node_role()

        # Create the EKS cluster using low-level constructs
        self.eks_cluster = self._create_eks_cluster()

        # Create essential EKS addons
        self.vpc_cni_addon = self._create_vpc_cni_addon()
        self.kube_proxy_addon = self._create_kube_proxy_addon()
        self.coredns_addon = self._create_coredns_addon()
        self.pod_identity_agent_addon = self._create_pod_identity_agent_addon()
        # self.cloudwatch_observability_addon = self._create_cloudwatch_observability_addon()

        self.alb_controller_role = self._create_alb_controller_role()
        self._create_pod_identity_association(
            id="AlbController",
            namespace="kube-system",
            service_account="aws-load-balancer-controller",
            role_arn=self.alb_controller_role.get_att("Arn").to_string()
        )

        # Create access entries...
        self._create_access_entries()

        # Add security group ingress
        self._add_security_group_ingress()

        # # Create outputs for GitOps
        self._create_outputs()

    def _create_access_entries(self):
        """Create access entry."""
        # TODO: too may privileges, need to be more specific

        access_entry = eks.CfnAccessEntry(
            self, "AccessEntry2",
            cluster_name=self.cluster_name,
            principal_arn="arn:aws:iam::532673134317:role/aws-reserved/sso.amazonaws.com/eu-west-1/AWSReservedSSO_AdministratorAccess_ecdb820f0c77380d",
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

        access_entry.node.add_dependency(self.eks_cluster)

    def _create_pod_identity_association(self, id: str, namespace: str, service_account: str,
                                         role_arn: str) -> eks.CfnPodIdentityAssociation:
        """Create pod identity association."""

        association = eks.CfnPodIdentityAssociation(
            self, f"PodIdentityAssociation{id}",
            cluster_name=self.eks_cluster.name,
            namespace=namespace,
            role_arn=role_arn,
            service_account=service_account,
            tags=[
                CfnTag(key="Name", value=self.config.prefix(f"{service_account}-pod-identity-association")),
            ]
        )

        association.node.add_dependency(self.pod_identity_agent_addon)

        return association

    def _create_cluster_role(self) -> iam.Role:
        """Create IAM role for EKS cluster."""
        trust_policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    principals=[
                        iam.ServicePrincipal("eks.amazonaws.com")
                    ],
                    actions=[
                        "sts:AssumeRole",
                        "sts:TagSession"
                    ]
                )
            ]
        )
        role = iam.CfnRole(
            self, "EksClusterRole",
            role_name=self.config.prefix("eks-cluster-role"),
            assume_role_policy_document=trust_policy,
            description="IAM role for EKS cluster",
            managed_policy_arns=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSBlockStoragePolicy").managed_policy_arn,
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSClusterPolicy").managed_policy_arn,
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSComputePolicy").managed_policy_arn,
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSLoadBalancingPolicy").managed_policy_arn,
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSNetworkingPolicy").managed_policy_arn,
            ]
        )

        return role

    def _create_node_role(self) -> iam.Role:
        """Create IAM role for EKS nodes."""
        # cloudwatch_observability_managed_policy = self._create_cloudwatch_observability_managed_policy()

        role = iam.Role(
            self, "EksNodeRole",
            role_name=self.config.prefix("eks-node-role"),
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodeMinimalPolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryPullOnly"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                # cloudwatch_observability_managed_policy,
            ],
            description="IAM role for EKS worker nodes",
        )

        return role

    def _create_cloudwatch_observability_managed_policy(self) -> iam.ManagedPolicy:
        """Create IAM policy statement for CloudWatch Observability."""
        return iam.ManagedPolicy(
            self, "CloudWatchObservabilityManagedPolicy",
            managed_policy_name=self.config.prefix("cloudwatch-observability-managed-policy"),
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "cloudwatch:*",
                        "logs:*",
                    ],
                    resources=[
                        "*"
                    ]
                )
            ]
        )

    def _create_alb_controller_role(self) -> iam.CfnRole:
        """Create IAM role for AWS Load Balancer Controller."""

        with open("assets/alb_controller_iam_policy.json", "r") as f:
            policy_doc = json.load(f)

        trust_policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    principals=[
                        iam.ServicePrincipal("pods.eks.amazonaws.com")
                    ],
                    actions=[
                        "sts:AssumeRole",
                        "sts:TagSession"
                    ]
                )
            ]
        )

        role = iam.CfnRole(
            self, "AlbControllerRole",
            role_name=self.config.prefix("alb-controller-role"),
            assume_role_policy_document=trust_policy,
            description="IAM role for AWS Load Balancer Controller",
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="AlbControllerPolicy",
                    policy_document=iam.PolicyDocument.from_json(policy_doc)
                )
            ]
        )

        return role

    def _create_eks_cluster(self) -> eks.CfnCluster:
        """Create minimal EKS cluster using low-level constructs."""

        eks_private_subnet_selection = self.vpc.select_subnets(
            subnet_group_name="eks-private-nat"
        )
        public_selection = self.vpc.select_subnets(
            subnet_group_name="public"
        )

        # for alb controller ingress
        for subnet in eks_private_subnet_selection.subnets:
            Tags.of(subnet).add("kubernetes.io/cluster/" + self.cluster_name, "owned")
            Tags.of(subnet).add("kubernetes.io/role/internal-elb", "1")
        for subnet in public_selection.subnets:
            Tags.of(subnet).add("kubernetes.io/cluster/" + self.cluster_name, "owned")
            Tags.of(subnet).add("kubernetes.io/role/elb", "1")

        # Prepare subnet configuration
        subnet_ids = [subnet.subnet_id for subnet in eks_private_subnet_selection.subnets + public_selection.subnets]

        # Create cluster using CfnCluster for maximum control
        cluster = eks.CfnCluster(
            self, "EksCluster",
            name=self.cluster_name,
            version="1.32",
            access_config=eks.CfnCluster.AccessConfigProperty(
                authentication_mode="API_AND_CONFIG_MAP",
            ),
            compute_config=eks.CfnCluster.ComputeConfigProperty(
                enabled=True,
                node_pools=["system", "general-purpose"],
                node_role_arn=self.auto_node_role.role_arn
            ),
            kubernetes_network_config=eks.CfnCluster.KubernetesNetworkConfigProperty(
                service_ipv4_cidr="172.20.0.0/16",
                ip_family="ipv4",
                elastic_load_balancing=eks.CfnCluster.ElasticLoadBalancingProperty(
                    enabled=True
                ),
            ),
            storage_config=eks.CfnCluster.StorageConfigProperty(
                block_storage=eks.CfnCluster.BlockStorageProperty(
                    enabled=True
                )
            ),
            # we install addons later
            bootstrap_self_managed_addons=False,
            role_arn=self.cluster_role.get_att("Arn").to_string(),
            resources_vpc_config=eks.CfnCluster.ResourcesVpcConfigProperty(
                subnet_ids=subnet_ids,
                security_group_ids=[self.eks_cluster_additional_sg.security_group_id],
                endpoint_private_access=False,
                endpoint_public_access=True
            ),
            logging=eks.CfnCluster.LoggingProperty(
                cluster_logging=eks.CfnCluster.ClusterLoggingProperty(
                    enabled_types=[
                        eks.CfnCluster.LoggingTypeConfigProperty(
                            type="api",
                        ),
                        eks.CfnCluster.LoggingTypeConfigProperty(
                            type="audit",
                        ),
                        eks.CfnCluster.LoggingTypeConfigProperty(
                            type="authenticator",
                        ),
                        eks.CfnCluster.LoggingTypeConfigProperty(
                            type="controllerManager",
                        ),
                        eks.CfnCluster.LoggingTypeConfigProperty(
                            type="scheduler",
                        ),
                    ]
                )
            ),
            # Essential addons will be added separately for better control
        )

        # Add tags
        Tags.of(cluster).add("Purpose", "GitOps-Cluster")
        Tags.of(cluster).add("ManagedBy", "CDK")
        Tags.of(cluster).add("Environment", self.config.env_name_str)
        Tags.of(cluster).add("Project", self.config.project_name)

        cluster.node.add_dependency(self.auto_node_role)

        return cluster

    def _create_vpc_cni_addon(self) -> eks.CfnAddon:
        """Create Amazon VPC CNI addon."""
        addon = eks.CfnAddon(
            self, "VpcCniAddon",
            addon_name="vpc-cni",
            cluster_name=self.eks_cluster.name,
            addon_version="v1.19.2-eksbuild.1",
            resolve_conflicts="OVERWRITE",
            configuration_values='{"env":{"ENABLE_PREFIX_DELEGATION":"true","WARM_PREFIX_TARGET":"1"}}'
        )

        # Add dependency on cluster
        addon.add_dependency(self.eks_cluster)

        return addon

    def _create_kube_proxy_addon(self) -> eks.CfnAddon:
        """Create kube-proxy addon."""
        addon = eks.CfnAddon(
            self, "KubeProxyAddon",
            addon_name="kube-proxy",
            cluster_name=self.eks_cluster.name,
            addon_version="v1.32.0-eksbuild.2",
            resolve_conflicts="OVERWRITE"
        )

        # Add dependency on cluster
        addon.add_dependency(self.eks_cluster)

        return addon

    def _create_coredns_addon(self) -> eks.CfnAddon:
        """Create CoreDNS addon."""
        addon = eks.CfnAddon(
            self, "CoreDnsAddon",
            addon_name="coredns",
            cluster_name=self.eks_cluster.name,
            addon_version="v1.11.4-eksbuild.2",
            resolve_conflicts="OVERWRITE",
            configuration_values='{"replicaCount":2,"resources":{"limits":{"memory":"170Mi"},"requests":{"cpu":"100m","memory":"70Mi"}}}'
        )

        # Add dependency on cluster
        addon.add_dependency(self.eks_cluster)

        return addon

    def _create_pod_identity_agent_addon(self) -> eks.CfnAddon:
        """Create Pod Identity Agent addon."""
        addon = eks.CfnAddon(
            self, "PodIdentityAgentAddon",
            addon_name="eks-pod-identity-agent",
            cluster_name=self.eks_cluster.name,
            addon_version="v1.3.7-eksbuild.2",
            resolve_conflicts="OVERWRITE"
        )

        # Add dependency on cluster
        addon.add_dependency(self.eks_cluster)

        return addon

    def _create_cloudwatch_observability_addon(self) -> eks.CfnAddon:
        """Create CloudWatch Observability addon."""
        addon = eks.CfnAddon(
            self, "CloudWatchObservabilityAddon",
            addon_name="amazon-cloudwatch-observability",
            cluster_name=self.eks_cluster.name,
            addon_version="v4.2.0-eksbuild.1",
            resolve_conflicts="OVERWRITE"
        )

        # Add dependency on cluster
        addon.add_dependency(self.eks_cluster)

        return addon

    def _add_security_group_ingress(self):
        """Add security group ingress to the cluster."""

        ingress_1 = ec2.CfnSecurityGroupIngress(
            self, "EksClusterToFastApiIngress",
            group_id=self.eks_cluster.get_att("ClusterSecurityGroupId").to_string(),
            from_port=0,
            to_port=65535,
            ip_protocol="-1",
            source_security_group_id=self.eks_fastapi_sg.security_group_id,
            description="Allow all traffic between EKS cluster and FastAPI pods"
        )

        ingress_2 = ec2.CfnSecurityGroupIngress(
            self, "FastApiToEksClusterIngress",
            group_id=self.eks_fastapi_sg.security_group_id,
            from_port=0,
            to_port=65535,
            ip_protocol="-1",
            source_security_group_id=self.eks_cluster.get_att("ClusterSecurityGroupId").to_string(),
            description="Allow all traffic between EKS cluster and FastAPI pods"
        )

        ingress_1.node.add_dependency(self.eks_cluster)
        ingress_2.node.add_dependency(self.eks_cluster)

    def _create_outputs(self):
        """Create CloudFormation outputs for GitOps integration."""

        CfnOutput(
            self, "ClusterName",
            value=self.eks_cluster.name,
            description="EKS Cluster Name",
            export_name=self.config.prefix("cluster-name")
        )

        CfnOutput(
            self, "ClusterEndpoint",
            value=self.eks_cluster.attr_endpoint,
            description="EKS Cluster Endpoint",
            export_name=self.config.prefix("cluster-endpoint")
        )

        CfnOutput(
            self, "ClusterArn",
            value=self.eks_cluster.attr_arn,
            description="EKS Cluster ARN",
            export_name=self.config.prefix("cluster-arn")
        )

        CfnOutput(
            self, "NodeRoleArn",
            value=self.auto_node_role.role_arn,
            description="EKS Node Role ARN",
            export_name=self.config.prefix("node-role-arn")
        )

        CfnOutput(
            self, "AlbControllerRoleArn",
            value=self.alb_controller_role.get_att("Arn").to_string(),
            description="ALB Controller Role ARN",
            export_name=self.config.prefix("alb-controller-role-arn")
        )

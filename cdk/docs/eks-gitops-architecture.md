# EKS GitOps Architecture

## Vue d'ensemble

Cette architecture utilise AWS CDK pour créer un cluster EKS minimal, puis délègue la gestion de tous les addons et workloads à ArgoCD via GitOps.

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   AWS CDK       │    │   ArgoCD        │    │   Git Repo      │
│                 │    │                 │    │                 │
│ ┌─────────────┐ │    │ ┌─────────────┐ │    │ ┌─────────────┐ │
│ │ EKS Cluster │ │    │ │ ArgoCD      │ │    │ │ Manifests   │ │
│ │ (Minimal)   │ │    │ │ Server      │ │    │ │ - CoreDNS   │ │
│ │             │ │    │ │             │ │    │ │ - kube-proxy│ │
│ │ IAM Roles   │ │    │ │             │ │    │ │ - ALB Ctrl  │ │
│ │             │ │    │ │             │ │    │ │ - Karpenter │ │
│ │ Outputs     │ │    │ │             │ │    │ │ - FastAPI   │ │
│ └─────────────┘ │    │ └─────────────┘ │    │ │ - Ingress   │ │
└─────────────────┘    └─────────────────┘    │ └─────────────┘ │
                                              └─────────────────┘
```

## Composants CDK

### Cluster EKS Minimal
- **Version Kubernetes**: 1.28
- **Addons**: Aucun (gérés par ArgoCD)
- **Node Groups**: Aucun (gérés par Karpenter)
- **Logging**: Activé pour tous les composants

### Rôles IAM Créés

1. **EksClusterRole** - Rôle pour le cluster EKS
2. **EksNodeRole** - Rôle pour les nœuds EKS standard
3. **KarpenterNodeRole** - Rôle pour les nœuds gérés par Karpenter
4. **FastApiCodeBuildRole** - Rôle pour CodeBuild FastAPI
5. **AlbControllerRole** - Rôle pour AWS Load Balancer Controller
6. **ArgoCDRole** - Rôle pour ArgoCD

## Outputs CloudFormation

Les outputs suivants sont créés pour l'intégration GitOps :

### Cluster Information
- `ClusterName` - Nom du cluster EKS
- `ClusterEndpoint` - Endpoint du cluster
- `ClusterArn` - ARN du cluster

### IAM Roles
- `NodeRoleArn` - ARN du rôle des nœuds EKS
- `KarpenterNodeRoleArn` - ARN du rôle des nœuds Karpenter
- `AlbControllerRoleArn` - ARN du rôle ALB Controller
- `ArgoCDRoleArn` - ARN du rôle ArgoCD

### Network Information
- `VpcId` - ID du VPC
- `PrivateSubnetIds` - IDs des sous-réseaux privés
- `PublicSubnetIds` - IDs des sous-réseaux publics

## Utilisation avec ArgoCD

### 1. Récupération des Outputs

Dans votre repo GitOps, vous pouvez récupérer les outputs via AWS CLI :

```bash
# Récupérer le nom du cluster
CLUSTER_NAME=$(aws cloudformation describe-stacks \
  --stack-name piercuta-dev-eks-backend-stack \
  --query 'Stacks[0].Outputs[?OutputKey==`ClusterName`].OutputValue' \
  --output text)

# Récupérer l'ARN du rôle Karpenter
KARPENTER_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name piercuta-dev-eks-backend-stack \
  --query 'Stacks[0].Outputs[?OutputKey==`KarpenterNodeRoleArn`].OutputValue' \
  --output text)
```

### 2. Manifests GitOps

Voici un exemple de structure pour votre repo GitOps :

```
manifests/
├── argocd/
│   ├── applications/
│   │   ├── coredns.yaml
│   │   ├── kube-proxy.yaml
│   │   ├── aws-load-balancer-controller.yaml
│   │   ├── karpenter.yaml
│   │   └── fastapi-app.yaml
│   └── projects/
│       └── default.yaml
├── coredns/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml
│   ├── configmap.yaml
│   ├── deployment.yaml
│   └── service.yaml
├── karpenter/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml
│   ├── nodeclass.yaml
│   ├── nodepool.yaml
│   └── provisioner.yaml
├── aws-load-balancer-controller/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml
│   ├── clusterrole.yaml
│   ├── clusterrolebinding.yaml
│   └── deployment.yaml
└── fastapi/
    ├── namespace.yaml
    ├── deployment.yaml
    ├── service.yaml
    └── ingress.yaml
```

### 3. Exemple de Manifest Karpenter

```yaml
# manifests/karpenter/nodeclass.yaml
apiVersion: karpenter.k8s.aws/v1beta1
kind: NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2
  role: ${KARPENTER_ROLE_ARN}  # Utilise l'output CDK
  subnetSelectorTerms:
    - tags:
        k8s.io/cluster-autoscaler/node-template/label/node.kubernetes.io/role: worker
  securityGroupSelectorTerms:
    - tags:
        k8s.io/cluster-autoscaler/node-template/label/node.kubernetes.io/role: worker
```

### 4. Exemple de Manifest ALB Controller

```yaml
# manifests/aws-load-balancer-controller/serviceaccount.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: aws-load-balancer-controller
  namespace: kube-system
  annotations:
    eks.amazonaws.com/role-arn: ${ALB_CONTROLLER_ROLE_ARN}  # Utilise l'output CDK
```

## Déploiement

### 1. Déployer l'infrastructure CDK

```bash
cdk deploy EksBackendStack
```

### 2. Configurer ArgoCD

```bash
# Installer ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Récupérer le mot de passe admin
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

### 3. Créer les Applications ArgoCD

```yaml
# manifests/argocd/applications/coredns.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: coredns
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/votre-org/votre-repo-gitops
    targetRevision: HEAD
    path: manifests/coredns
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

## Sécurité

### IAM Roles
- Tous les rôles utilisent le principe du moindre privilège
- Les politiques sont larges au départ pour faciliter le développement
- Affinez les permissions selon vos besoins spécifiques

### Network Security
- Le cluster utilise des sous-réseaux privés pour les workloads
- Les sous-réseaux publics sont utilisés pour les load balancers
- Les security groups sont configurés pour isoler les services

## Monitoring

### CloudWatch Logs
- Tous les logs du cluster sont envoyés vers CloudWatch
- Les logs incluent : API, Audit, Authenticator, Controller Manager, Scheduler

### Métriques
- Utilisez CloudWatch Container Insights pour les métriques EKS
- Configurez des alertes sur les métriques critiques

## Maintenance

### Mises à jour
- Les mises à jour du cluster se font via CDK
- Les mises à jour des addons se font via ArgoCD
- Utilisez des stratégies de déploiement blue/green

### Sauvegarde
- Les données persistantes doivent être sauvegardées séparément
- Utilisez Velero pour les sauvegardes de cluster
- Configurez des sauvegardes automatiques des bases de données

## Troubleshooting

### Problèmes courants

1. **Erreurs de permissions IAM**
   - Vérifiez que les rôles ont les bonnes politiques
   - Utilisez AWS IAM Access Analyzer pour identifier les permissions manquantes

2. **Problèmes de networking**
   - Vérifiez les security groups
   - Vérifiez les routes dans les route tables
   - Testez la connectivité entre les sous-réseaux

3. **Problèmes ArgoCD**
   - Vérifiez les logs ArgoCD : `kubectl logs -n argocd -l app.kubernetes.io/name=argocd-server`
   - Vérifiez la synchronisation des applications
   - Vérifiez les permissions RBAC

## Ressources additionnelles

- [Documentation EKS](https://docs.aws.amazon.com/eks/)
- [Documentation ArgoCD](https://argo-cd.readthedocs.io/)
- [Documentation Karpenter](https://karpenter.sh/)
- [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) 
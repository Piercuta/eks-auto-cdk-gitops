from enum import Enum


class EnvironmentName(str, Enum):
    """Available environments."""
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class AwsRegion(str, Enum):
    """Available AWS regions."""
    EU_WEST_1 = "eu-west-1"
    EU_WEST_2 = "eu-west-2"
    EU_WEST_3 = "eu-west-3"
    EU_CENTRAL_1 = "eu-central-1"
    US_EAST_1 = "us-east-1"
    US_EAST_2 = "us-east-2"
    US_WEST_1 = "us-west-1"
    US_WEST_2 = "us-west-2"


class EcsTaskCpu(int, Enum):
    """Valid CPU values for ECS Fargate tasks."""
    CPU_256 = 256    # 0.25 vCPU
    CPU_512 = 512    # 0.5 vCPU
    CPU_1024 = 1024  # 1 vCPU
    CPU_2048 = 2048  # 2 vCPU
    CPU_4096 = 4096  # 4 vCPU


class EcsTaskMemory(int, Enum):
    """Valid memory values for ECS Fargate tasks."""
    MEM_512 = 512    # 0.5 GB
    MEM_1024 = 1024  # 1 GB
    MEM_2048 = 2048  # 2 GB
    MEM_3072 = 3072  # 3 GB
    MEM_4096 = 4096  # 4 GB
    MEM_5120 = 5120  # 5 GB
    MEM_6144 = 6144  # 6 GB
    MEM_7168 = 7168  # 7 GB
    MEM_8192 = 8192  # 8 GB
    MEM_16384 = 16384  # 16 GB
    MEM_30720 = 30720  # 30 GB

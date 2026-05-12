# No remote backend is configured — state is stored locally (gitignored).
# Before sharing this module with other operators or running it from CI,
# add a backend block (e.g. S3 + DynamoDB locking) here.

terraform {
  required_version = ">= 1.9"
  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "confluent" {
  cloud_api_key    = var.confluent_cloud_api_key
  cloud_api_secret = var.confluent_cloud_api_secret
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = var.common_tags
  }
}

resource "confluent_environment" "main" {
  display_name = var.confluent_environment_name
  stream_governance {
    package = "ESSENTIALS"
  }
}

resource "confluent_kafka_cluster" "main" {
  display_name = var.confluent_kafka_cluster_name
  availability = "SINGLE_ZONE"
  cloud        = "AWS"
  region       = var.aws_region
  basic {}
  environment {
    id = confluent_environment.main.id
  }
}

# SR is auto-provisioned by Confluent Cloud when stream_governance is set on
# the environment. This data source reads the resulting cluster after it exists.
data "confluent_schema_registry_cluster" "main" {
  environment {
    id = confluent_environment.main.id
  }
  depends_on = [confluent_kafka_cluster.main]
}

# Service account used by Terraform itself to register SR schemas
resource "confluent_service_account" "tf_sr" {
  display_name = "sa-yt-tf-sr"
  description  = "Terraform schema registry admin"
}

resource "confluent_api_key" "tf_sr" {
  display_name = "sa-yt-tf-sr-key"
  owner {
    id          = confluent_service_account.tf_sr.id
    api_version = confluent_service_account.tf_sr.api_version
    kind        = confluent_service_account.tf_sr.kind
  }
  managed_resource {
    id          = data.confluent_schema_registry_cluster.main.id
    api_version = data.confluent_schema_registry_cluster.main.api_version
    kind        = data.confluent_schema_registry_cluster.main.kind
    environment {
      id = confluent_environment.main.id
    }
  }
}

resource "confluent_role_binding" "tf_sr_admin" {
  principal   = "User:${confluent_service_account.tf_sr.id}"
  role_name   = "ResourceOwner"
  crn_pattern = "${data.confluent_schema_registry_cluster.main.resource_name}/subject=*"
}

# Service account used by Terraform itself to create topics and ACLs
resource "confluent_service_account" "tf_kafka" {
  display_name = "sa-yt-tf-kafka"
  description  = "Terraform Kafka admin for topic and ACL management"
}

resource "confluent_api_key" "tf_kafka" {
  display_name = "sa-yt-tf-kafka-key"
  owner {
    id          = confluent_service_account.tf_kafka.id
    api_version = confluent_service_account.tf_kafka.api_version
    kind        = confluent_service_account.tf_kafka.kind
  }
  managed_resource {
    id          = confluent_kafka_cluster.main.id
    api_version = confluent_kafka_cluster.main.api_version
    kind        = confluent_kafka_cluster.main.kind
    environment {
      id = confluent_environment.main.id
    }
  }
}

resource "confluent_role_binding" "tf_kafka_admin" {
  principal   = "User:${confluent_service_account.tf_kafka.id}"
  role_name   = "CloudClusterAdmin"
  crn_pattern = confluent_kafka_cluster.main.rbac_crn
}

data "aws_caller_identity" "current" {}

variable "confluent_cloud_api_key" {
  description = "Confluent Cloud API key (provider auth, not Kafka-level)"
  type        = string
  sensitive   = true
}

variable "confluent_cloud_api_secret" {
  description = "Confluent Cloud API secret"
  type        = string
  sensitive   = true
}

variable "confluent_environment_name" {
  description = "Display name for the Confluent Cloud environment"
  type        = string
  default     = "yt-history"
}

variable "confluent_kafka_cluster_name" {
  description = "Display name for the Confluent Cloud Kafka cluster"
  type        = string
  default     = "yt-kafka"
}

variable "aws_region" {
  description = "AWS region for Secrets Manager and IAM"
  type        = string
  default     = "us-east-1"
}

variable "eks_oidc_issuer" {
  description = "EKS OIDC issuer URL without https:// prefix (e.g. oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E). Set to a placeholder until an EKS cluster exists — update to the real value before deploying External Secrets Operator."
  type        = string
  default     = "placeholder.example.com/id/PLACEHOLDER"
}

variable "eso_k8s_namespace" {
  description = "Kubernetes namespace where External Secrets Operator runs"
  type        = string
  default     = "external-secrets"
}

variable "eso_k8s_service_account" {
  description = "Kubernetes service account name for External Secrets Operator"
  type        = string
  default     = "external-secrets"
}

variable "youtube_api_key" {
  description = "YouTube Data API v3 key for the Flink enricher job"
  type        = string
  sensitive   = true
}

variable "common_tags" {
  description = "Mandatory tags applied to all AWS resources via provider default_tags. Must be overridden as a complete map — partial overrides drop omitted keys. cflt_keep_until is required and has no default."
  type        = map(string)
  default = {
    cflt_environment = "devel"
    cflt_partition   = "onprem"
    cflt_service     = "osowski/youtube-history"
    cflt_managed_by  = "terraform"
    cflt_managed_id  = "osowski/youtube-history"
    cflt_protected   = "false"
    cflt_keep_until  = ""
  }
}

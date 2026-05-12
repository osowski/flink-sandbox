output "sm_secret_arns" {
  description = "AWS SM secret ARNs for each trust boundary — reference these in GitOps ExternalSecret CRs"
  value = {
    producer        = aws_secretsmanager_secret.producer.arn
    enricher        = aws_secretsmanager_secret.enricher.arn
    counter         = aws_secretsmanager_secret.counter.arn
    youtube_api_key = aws_secretsmanager_secret.youtube_api_key.arn
  }
}

output "eso_iam_role_arn" {
  description = "IAM role ARN for External Secrets Operator IRSA annotation"
  value       = aws_iam_role.eso.arn
}

output "bootstrap_servers" {
  description = "Confluent Cloud Kafka bootstrap servers (non-secret, use in ConfigMaps)"
  value       = confluent_kafka_cluster.main.bootstrap_endpoint
}

output "schema_registry_url" {
  description = "Confluent Cloud Schema Registry REST endpoint (non-secret, use in ConfigMaps)"
  value       = data.confluent_schema_registry_cluster.main.rest_endpoint
}

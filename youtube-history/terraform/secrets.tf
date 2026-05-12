# recovery_window_in_days = 0 on all secrets: intentional for this lab environment.
# Allows destroy/recreate cycles without the 7–30 day AWS recovery window blocking re-use of the same secret name.
# Do not carry this setting into a production environment.

resource "aws_secretsmanager_secret" "producer" {
  name                    = "/yt-pipeline/confluent/producer"
  description             = "Confluent Cloud Kafka API credentials for the YouTube history producer"
  recovery_window_in_days = 0
  lifecycle {
    ignore_changes = [tags["divvy_last_modified_by"], tags["divvy_owner"]]
  }
}

resource "aws_secretsmanager_secret_version" "producer" {
  secret_id = aws_secretsmanager_secret.producer.id
  secret_string = jsonencode({
    kafka_api_key    = confluent_api_key.producer_kafka.id
    kafka_api_secret = confluent_api_key.producer_kafka.secret
    sr_api_key       = confluent_api_key.producer_sr.id
    sr_api_secret    = confluent_api_key.producer_sr.secret
  })
}

resource "aws_secretsmanager_secret" "enricher" {
  name                    = "/yt-pipeline/confluent/enricher"
  description             = "Confluent Cloud credentials for the YouTube history Flink enricher job"
  recovery_window_in_days = 0
  lifecycle {
    ignore_changes = [tags["divvy_last_modified_by"], tags["divvy_owner"]]
  }
}

resource "aws_secretsmanager_secret_version" "enricher" {
  secret_id = aws_secretsmanager_secret.enricher.id
  secret_string = jsonencode({
    kafka_api_key    = confluent_api_key.enricher_kafka.id
    kafka_api_secret = confluent_api_key.enricher_kafka.secret
    sr_api_key       = confluent_api_key.enricher_sr.id
    sr_api_secret    = confluent_api_key.enricher_sr.secret
  })
}

resource "aws_secretsmanager_secret" "counter" {
  name                    = "/yt-pipeline/confluent/counter"
  description             = "Confluent Cloud credentials for the YouTube history Flink counter job"
  recovery_window_in_days = 0
  lifecycle {
    ignore_changes = [tags["divvy_last_modified_by"], tags["divvy_owner"]]
  }
}

resource "aws_secretsmanager_secret_version" "counter" {
  secret_id = aws_secretsmanager_secret.counter.id
  secret_string = jsonencode({
    kafka_api_key    = confluent_api_key.counter_kafka.id
    kafka_api_secret = confluent_api_key.counter_kafka.secret
    sr_api_key       = confluent_api_key.counter_sr.id
    sr_api_secret    = confluent_api_key.counter_sr.secret
  })
}

resource "aws_secretsmanager_secret" "youtube_api_key" {
  name                    = "/yt-pipeline/youtube/api-key"
  description             = "YouTube Data API v3 key for the Flink enricher job"
  recovery_window_in_days = 0
  lifecycle {
    ignore_changes = [tags["divvy_last_modified_by"], tags["divvy_owner"]]
  }
}

resource "aws_secretsmanager_secret_version" "youtube_api_key" {
  secret_id = aws_secretsmanager_secret.youtube_api_key.id
  secret_string = jsonencode({
    api_key = var.youtube_api_key
  })
}

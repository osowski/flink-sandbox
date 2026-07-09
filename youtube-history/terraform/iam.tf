# ── Service Accounts ──────────────────────────────────────────────────────────

resource "confluent_service_account" "producer" {
  display_name = "sa-yt-producer"
  description  = "YouTube history Python producer"
}

resource "confluent_service_account" "enricher" {
  display_name = "sa-yt-enricher"
  description  = "YouTube history Flink enricher + classifier job"
}

resource "confluent_service_account" "counter" {
  display_name = "sa-yt-counter"
  description  = "YouTube history Flink counter job"
}

# ── Kafka API Keys ────────────────────────────────────────────────────────────

resource "confluent_api_key" "producer_kafka" {
  display_name = "sa-yt-producer-kafka"
  owner {
    id          = confluent_service_account.producer.id
    api_version = confluent_service_account.producer.api_version
    kind        = confluent_service_account.producer.kind
  }
  managed_resource {
    id          = confluent_kafka_cluster.main.id
    api_version = confluent_kafka_cluster.main.api_version
    kind        = confluent_kafka_cluster.main.kind
    environment { id = confluent_environment.main.id }
  }
}

resource "confluent_api_key" "enricher_kafka" {
  display_name = "sa-yt-enricher-kafka"
  owner {
    id          = confluent_service_account.enricher.id
    api_version = confluent_service_account.enricher.api_version
    kind        = confluent_service_account.enricher.kind
  }
  managed_resource {
    id          = confluent_kafka_cluster.main.id
    api_version = confluent_kafka_cluster.main.api_version
    kind        = confluent_kafka_cluster.main.kind
    environment { id = confluent_environment.main.id }
  }
}

resource "confluent_api_key" "enricher_sr" {
  display_name = "sa-yt-enricher-sr"
  owner {
    id          = confluent_service_account.enricher.id
    api_version = confluent_service_account.enricher.api_version
    kind        = confluent_service_account.enricher.kind
  }
  managed_resource {
    id          = data.confluent_schema_registry_cluster.main.id
    api_version = data.confluent_schema_registry_cluster.main.api_version
    kind        = data.confluent_schema_registry_cluster.main.kind
    environment { id = confluent_environment.main.id }
  }
}

resource "confluent_api_key" "counter_kafka" {
  display_name = "sa-yt-counter-kafka"
  owner {
    id          = confluent_service_account.counter.id
    api_version = confluent_service_account.counter.api_version
    kind        = confluent_service_account.counter.kind
  }
  managed_resource {
    id          = confluent_kafka_cluster.main.id
    api_version = confluent_kafka_cluster.main.api_version
    kind        = confluent_kafka_cluster.main.kind
    environment { id = confluent_environment.main.id }
  }
}

resource "confluent_api_key" "counter_sr" {
  display_name = "sa-yt-counter-sr"
  owner {
    id          = confluent_service_account.counter.id
    api_version = confluent_service_account.counter.api_version
    kind        = confluent_service_account.counter.kind
  }
  managed_resource {
    id          = data.confluent_schema_registry_cluster.main.id
    api_version = data.confluent_schema_registry_cluster.main.api_version
    kind        = data.confluent_schema_registry_cluster.main.kind
    environment { id = confluent_environment.main.id }
  }
}

resource "confluent_api_key" "producer_sr" {
  display_name = "sa-yt-producer-sr"
  owner {
    id          = confluent_service_account.producer.id
    api_version = confluent_service_account.producer.api_version
    kind        = confluent_service_account.producer.kind
  }
  managed_resource {
    id          = data.confluent_schema_registry_cluster.main.id
    api_version = data.confluent_schema_registry_cluster.main.api_version
    kind        = data.confluent_schema_registry_cluster.main.kind
    environment { id = confluent_environment.main.id }
  }
}

# ── Kafka ACLs ────────────────────────────────────────────────────────────────

locals {
  kafka_rest  = confluent_kafka_cluster.main.rest_endpoint
  kafka_creds = { key = confluent_api_key.tf_kafka.id, secret = confluent_api_key.tf_kafka.secret }
}

# ── sa-yt-enricher read access for CP Flink / CMF (SELECT from any yt.* topic) ──
# CMF / CP Flink authenticates as sa-yt-enricher and needs READ + DESCRIBE +
# DESCRIBE_CONFIGS to run Flink SQL. DESCRIBE_CONFIGS is distinct from DESCRIBE and is
# fetched during SQL validation for BOTH DESCRIBE and SELECT. A single PREFIXED grant per
# operation over the "yt." topic family covers every topic in the solution at once and
# supersedes the former per-topic read grants. WRITE stays per-topic (see below).
resource "confluent_kafka_acl" "enricher_read" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = "yt."
  pattern_type  = "PREFIXED"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "enricher_describe" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = "yt."
  pattern_type  = "PREFIXED"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "DESCRIBE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "enricher_describe_configs" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = "yt."
  pattern_type  = "PREFIXED"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "DESCRIBE_CONFIGS"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

# _confluent_sr_catalog is CMF's internal SR catalog topic (not under the yt. prefix).
# READ resolves "Failed to initialize cache for Database"; DESCRIBE covers client paths
# that describe before reading.
resource "confluent_kafka_acl" "enricher_read_sr_catalog" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = "_confluent_sr_catalog"
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "enricher_describe_sr_catalog" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = "_confluent_sr_catalog"
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "DESCRIBE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "producer_write_raw" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.raw_watch_events.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.producer.id}"
  host          = "*"
  operation     = "WRITE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "enricher_write_metadata" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.video_metadata.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "WRITE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "enricher_write_enriched" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.enriched_watch_events.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "WRITE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "counter_read_enriched" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.enriched_watch_events.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.counter.id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "counter_write_counts" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.music_watch_counts.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.counter.id}"
  host          = "*"
  operation     = "WRITE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

# Consumer group ACLs for enricher and counter
resource "confluent_kafka_acl" "enricher_group" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "GROUP"
  resource_name = "yt-enricher"
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "counter_group" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "GROUP"
  resource_name = "yt-counter"
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.counter.id}"
  host          = "*"
  operation     = "READ"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

# DESCRIBE ACLs — required for Kafka clients to resolve topic metadata
resource "confluent_kafka_acl" "producer_describe_raw" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.raw_watch_events.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.producer.id}"
  host          = "*"
  operation     = "DESCRIBE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "enricher_write_dlq" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.raw_watch_events_dlq.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.enricher.id}"
  host          = "*"
  operation     = "WRITE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "counter_describe_enriched" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.enriched_watch_events.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.counter.id}"
  host          = "*"
  operation     = "DESCRIBE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_acl" "counter_describe_counts" {
  kafka_cluster { id = confluent_kafka_cluster.main.id }
  resource_type = "TOPIC"
  resource_name = confluent_kafka_topic.music_watch_counts.topic_name
  pattern_type  = "LITERAL"
  principal     = "User:${confluent_service_account.counter.id}"
  host          = "*"
  operation     = "DESCRIBE"
  permission    = "ALLOW"
  rest_endpoint = local.kafka_rest
  credentials {
    key    = local.kafka_creds.key
    secret = local.kafka_creds.secret
  }
  depends_on = [confluent_role_binding.tf_kafka_admin]
}

# ── AWS IAM: ESO IRSA Role ────────────────────────────────────────────────────

resource "aws_iam_role" "eso" {
  name = "yt-pipeline-eso"

  lifecycle {
    ignore_changes = [tags["divvy_last_modified_by"], tags["divvy_owner"]]
  }

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${var.eks_oidc_issuer}"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.eks_oidc_issuer}:sub" = "system:serviceaccount:${var.eso_k8s_namespace}:${var.eso_k8s_service_account}"
          "${var.eks_oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_policy" "eso_sm_read" {
  name = "yt-pipeline-eso-sm-read"

  lifecycle {
    ignore_changes = [tags["divvy_last_modified_by"], tags["divvy_owner"]]
  }

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = [
        "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:/yt-pipeline/confluent/*",
        "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:/yt-pipeline/youtube/*"
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eso_sm_read" {
  role       = aws_iam_role.eso.name
  policy_arn = aws_iam_policy.eso_sm_read.arn
}

# ── Schema Registry RBAC role bindings for pipeline service accounts ──────────
# All pipeline accounts are granted DeveloperRead only. Terraform owns all schema
# registration via sa-yt-tf-sr. Producers and consumers must set
# auto.register.schemas=false so the SR client never attempts to register schemas
# at runtime — it only looks up existing schema IDs, which DeveloperRead covers.

resource "confluent_role_binding" "producer_sr_read" {
  principal   = "User:${confluent_service_account.producer.id}"
  role_name   = "DeveloperRead"
  crn_pattern = "${data.confluent_schema_registry_cluster.main.resource_name}/subject=*"
}

resource "confluent_role_binding" "enricher_sr_read" {
  principal   = "User:${confluent_service_account.enricher.id}"
  role_name   = "DeveloperRead"
  crn_pattern = "${data.confluent_schema_registry_cluster.main.resource_name}/subject=*"
}

resource "confluent_role_binding" "counter_sr_read" {
  principal   = "User:${confluent_service_account.counter.id}"
  role_name   = "DeveloperRead"
  crn_pattern = "${data.confluent_schema_registry_cluster.main.resource_name}/subject=*"
}

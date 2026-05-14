resource "confluent_kafka_topic" "raw_watch_events" {
  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }
  topic_name       = "yt.raw.watch.events"
  partitions_count = 6
  rest_endpoint    = confluent_kafka_cluster.main.rest_endpoint

  config = {
    "cleanup.policy"        = "compact"
    "retention.ms"          = "-1"
    "min.compaction.lag.ms" = "3600000"
  }

  credentials {
    key    = confluent_api_key.tf_kafka.id
    secret = confluent_api_key.tf_kafka.secret
  }

  depends_on = [confluent_role_binding.tf_kafka_admin]

  lifecycle {
    prevent_destroy = true
  }
}

resource "confluent_kafka_topic" "video_metadata" {
  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }
  topic_name       = "yt.video.metadata"
  partitions_count = 6
  rest_endpoint    = confluent_kafka_cluster.main.rest_endpoint

  config = {
    "cleanup.policy" = "compact"
    "retention.ms"   = "-1"
  }

  credentials {
    key    = confluent_api_key.tf_kafka.id
    secret = confluent_api_key.tf_kafka.secret
  }

  depends_on = [confluent_role_binding.tf_kafka_admin]

  lifecycle {
    prevent_destroy = true
  }
}

resource "confluent_kafka_topic" "enriched_watch_events" {
  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }
  topic_name       = "yt.enriched.watch.events"
  partitions_count = 6
  rest_endpoint    = confluent_kafka_cluster.main.rest_endpoint

  config = {
    "cleanup.policy" = "compact"
    "retention.ms"   = "-1"
  }

  credentials {
    key    = confluent_api_key.tf_kafka.id
    secret = confluent_api_key.tf_kafka.secret
  }

  depends_on = [confluent_role_binding.tf_kafka_admin]
}

resource "confluent_kafka_topic" "raw_watch_events_dlq" {
  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }
  topic_name       = "yt.raw.watch.events.dlq"
  partitions_count = 1
  rest_endpoint    = confluent_kafka_cluster.main.rest_endpoint

  config = {
    "cleanup.policy" = "delete"
    "retention.ms"   = "2592000000"
  }

  credentials {
    key    = confluent_api_key.tf_kafka.id
    secret = confluent_api_key.tf_kafka.secret
  }

  depends_on = [confluent_role_binding.tf_kafka_admin]

  lifecycle {
    prevent_destroy = true
  }
}

resource "confluent_kafka_topic" "music_watch_counts" {
  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }
  topic_name       = "yt.music.watch.counts"
  partitions_count = 6
  rest_endpoint    = confluent_kafka_cluster.main.rest_endpoint

  config = {
    "cleanup.policy" = "compact"
    "retention.ms"   = "-1"
  }

  credentials {
    key    = confluent_api_key.tf_kafka.id
    secret = confluent_api_key.tf_kafka.secret
  }

  depends_on = [confluent_role_binding.tf_kafka_admin]

  lifecycle {
    prevent_destroy = true
  }
}

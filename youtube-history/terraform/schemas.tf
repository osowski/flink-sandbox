resource "confluent_schema" "raw_watch_event" {
  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint
  subject_name  = "yt.raw.watch.events-value"
  format        = "AVRO"
  schema        = file("${path.module}/../schemas/RawWatchEvent.avsc")

  credentials {
    key    = confluent_api_key.tf_sr.id
    secret = confluent_api_key.tf_sr.secret
  }

  depends_on = [confluent_role_binding.tf_sr_admin]
}

resource "confluent_schema" "video_metadata" {
  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint
  subject_name  = "yt.video.metadata-value"
  format        = "AVRO"
  schema        = file("${path.module}/../schemas/VideoMetadata.avsc")

  credentials {
    key    = confluent_api_key.tf_sr.id
    secret = confluent_api_key.tf_sr.secret
  }

  depends_on = [confluent_role_binding.tf_sr_admin]
}

resource "confluent_schema" "enriched_watch_event" {
  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint
  subject_name  = "yt.enriched.watch.events-value"
  format        = "AVRO"
  schema        = file("${path.module}/../schemas/EnrichedWatchEvent.avsc")

  credentials {
    key    = confluent_api_key.tf_sr.id
    secret = confluent_api_key.tf_sr.secret
  }

  depends_on = [confluent_role_binding.tf_sr_admin]
}

resource "confluent_schema" "music_watch_count" {
  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint
  subject_name  = "yt.music.watch.counts-value"
  format        = "AVRO"
  schema        = file("${path.module}/../schemas/MusicWatchCount.avsc")

  credentials {
    key    = confluent_api_key.tf_sr.id
    secret = confluent_api_key.tf_sr.secret
  }

  depends_on = [confluent_role_binding.tf_sr_admin]
}

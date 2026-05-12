package io.youtube.history;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Map;
import java.util.Properties;

public class EnricherJob {

    private static final Logger LOG = LoggerFactory.getLogger(EnricherJob.class);

    public static void main(String[] args) throws Exception {
        String bootstrapServers  = required("BOOTSTRAP_SERVERS");
        String kafkaApiKey       = required("KAFKA_API_KEY");
        String kafkaApiSecret    = required("KAFKA_API_SECRET");
        String srUrl             = required("SCHEMA_REGISTRY_URL");
        String srApiKey          = required("SR_API_KEY");
        String srApiSecret       = required("SR_API_SECRET");
        String youtubeApiKey     = required("YOUTUBE_API_KEY");
        String rawTopic          = required("TOPIC_RAW_WATCH_EVENTS");
        String metadataTopic     = required("TOPIC_VIDEO_METADATA");
        String enrichedTopic     = required("TOPIC_ENRICHED_WATCH_EVENTS");
        String dlqTopic          = required("TOPIC_RAW_WATCH_EVENTS_DLQ");
        String consumerGroupId   = required("CONSUMER_GROUP_ID");

        LOG.info("Starting yt-enricher bootstrapServers={} srUrl={}", bootstrapServers, srUrl);

        Map<String, VideoMetadata> cache = MetadataBootstrap.load(
            bootstrapServers, kafkaApiKey, kafkaApiSecret,
            srUrl, srApiKey, srApiSecret,
            metadataTopic
        );
        LOG.info("Metadata cache ready entries={}", cache.size());

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(1);
        env.enableCheckpointing(60_000L);

        Properties kafkaProps = kafkaProps(bootstrapServers, kafkaApiKey, kafkaApiSecret,
            srUrl, srApiKey, srApiSecret);

        KafkaSource<RawWatchEvent> source = KafkaSource.<RawWatchEvent>builder()
            .setBootstrapServers(bootstrapServers)
            .setTopics(rawTopic)
            .setGroupId(consumerGroupId)
            // earliest() is intentional: all source topics are fact tables (compacted, idempotent).
            // On first run the job replays full history; yt.video.metadata cache skips re-enrichment
            // for already-seen videos. See README §3 "Start from beginning" for full rationale.
            .setStartingOffsets(OffsetsInitializer.earliest())
            .setProperties(kafkaProps)
            .setValueOnlyDeserializer(new ConfluentAvroDeserializationSchema<>(
                RawWatchEvent.class, srUrl, srApiKey, srApiSecret))
            .build();

        EnrichmentFunction fn = new EnrichmentFunction(
            cache, youtubeApiKey,
            bootstrapServers, kafkaApiKey, kafkaApiSecret,
            srUrl, srApiKey, srApiSecret,
            metadataTopic,
            null
        );

        KafkaSink<EnrichedWatchEvent> sink = KafkaSink.<EnrichedWatchEvent>builder()
            .setBootstrapServers(bootstrapServers)
            .setKafkaProducerConfig(producerProps(bootstrapServers, kafkaApiKey, kafkaApiSecret,
                srUrl, srApiKey, srApiSecret))
            .setRecordSerializer(KafkaRecordSerializationSchema.<EnrichedWatchEvent>builder()
                .setTopic(enrichedTopic)
                .setKeySerializationSchema(e -> Base64.getEncoder().encode(
                    (e.getWatchedAt() + "-" + e.getVideoId()).getBytes(StandardCharsets.UTF_8)))
                .setValueSerializationSchema(new ConfluentAvroSerializationSchema<>(
                    enrichedTopic, srUrl, srApiKey, srApiSecret))
                .build())
            .build();

        KafkaSink<RawWatchEvent> dlqSink = KafkaSink.<RawWatchEvent>builder()
            .setBootstrapServers(bootstrapServers)
            .setKafkaProducerConfig(producerProps(bootstrapServers, kafkaApiKey, kafkaApiSecret,
                srUrl, srApiKey, srApiSecret))
            .setRecordSerializer(KafkaRecordSerializationSchema.<RawWatchEvent>builder()
                .setTopic(dlqTopic)
                .setKeySerializationSchema(e -> e.getVideoId().toString().getBytes(StandardCharsets.UTF_8))
                .setValueSerializationSchema(new ConfluentAvroSerializationSchema<>(
                    dlqTopic, srUrl, srApiKey, srApiSecret))
                .build())
            .build();

        SingleOutputStreamOperator<EnrichedWatchEvent> enriched =
            env.fromSource(source, WatermarkStrategy.noWatermarks(), "raw-watch-events")
               .keyBy(event -> EnrichmentFunction.SINGLETON_KEY)
               .process(fn);

        enriched.sinkTo(sink);
        enriched.getSideOutput(EnrichmentFunction.DLQ_TAG).sinkTo(dlqSink);

        env.execute("yt-enricher");
    }

    private static String required(String envVar) {
        String val = System.getenv(envVar);
        if (val == null || val.isBlank())
            throw new IllegalStateException("Required env var not set: " + envVar);
        return val;
    }

    private static Properties kafkaProps(String servers, String key, String secret,
            String srUrl, String srKey, String srSecret) {
        Properties p = new Properties();
        p.put("security.protocol", "SASL_SSL");
        p.put("sasl.mechanism", "PLAIN");
        p.put("sasl.jaas.config",
            "org.apache.kafka.common.security.plain.PlainLoginModule required " +
            "username=\"" + key + "\" password=\"" + secret + "\";");
        p.put("schema.registry.url", srUrl);
        p.put("basic.auth.credentials.source", "USER_INFO");
        p.put("basic.auth.user.info", srKey + ":" + srSecret);
        p.put("specific.avro.reader", "true");
        return p;
    }

    private static Properties producerProps(String servers, String key, String secret,
            String srUrl, String srKey, String srSecret) {
        Properties p = new Properties();
        p.put("security.protocol", "SASL_SSL");
        p.put("sasl.mechanism", "PLAIN");
        p.put("sasl.jaas.config",
            "org.apache.kafka.common.security.plain.PlainLoginModule required " +
            "username=\"" + key + "\" password=\"" + secret + "\";");
        p.put("schema.registry.url", srUrl);
        p.put("basic.auth.credentials.source", "USER_INFO");
        p.put("basic.auth.user.info", srKey + ":" + srSecret);
        p.put("acks", "all");
        p.put("enable.idempotence", "true");
        return p;
    }
}

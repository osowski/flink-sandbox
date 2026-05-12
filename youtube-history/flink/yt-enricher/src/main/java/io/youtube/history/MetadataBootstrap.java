package io.youtube.history;

import io.confluent.kafka.serializers.KafkaAvroDeserializer;
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.common.TopicPartition;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;

public final class MetadataBootstrap {

    private static final Logger LOG = LoggerFactory.getLogger(MetadataBootstrap.class);

    private MetadataBootstrap() {}

    public static Map<String, VideoMetadata> load(
            String bootstrapServers,
            String kafkaApiKey,
            String kafkaApiSecret,
            String srUrl,
            String srApiKey,
            String srApiSecret,
            String topic) {

        Properties props = new Properties();
        props.put("bootstrap.servers", bootstrapServers);
        props.put("security.protocol", "SASL_SSL");
        props.put("sasl.mechanism", "PLAIN");
        props.put("sasl.jaas.config",
            "org.apache.kafka.common.security.plain.PlainLoginModule required " +
            "username=\"" + kafkaApiKey + "\" password=\"" + kafkaApiSecret + "\";");
        props.put("key.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
        props.put("value.deserializer", KafkaAvroDeserializer.class.getName());
        props.put("schema.registry.url", srUrl);
        props.put("basic.auth.credentials.source", "USER_INFO");
        props.put("basic.auth.user.info", srApiKey + ":" + srApiSecret);
        props.put("specific.avro.reader", "true");
        // assign() + seekToBeginning() handles offset management directly — no group.id needed,
        // so no consumer group is ever registered on the broker (no orphaned groups).
        props.put("enable.auto.commit", "false");

        Map<String, VideoMetadata> cache = new HashMap<>();

        try (KafkaConsumer<String, VideoMetadata> consumer = new KafkaConsumer<>(props)) {
            List<TopicPartition> partitions = consumer.partitionsFor(topic).stream()
                .map(p -> new TopicPartition(topic, p.partition()))
                .toList();
            consumer.assign(partitions);
            consumer.seekToBeginning(partitions);
            LOG.info("Bootstrap starting topic={} partitions={}", topic, partitions.size());

            Map<TopicPartition, Long> endOffsets = consumer.endOffsets(partitions);
            boolean done = partitions.stream().allMatch(tp -> endOffsets.get(tp) == 0L);

            while (!done) {
                ConsumerRecords<String, VideoMetadata> records = consumer.poll(Duration.ofSeconds(5));
                for (ConsumerRecord<String, VideoMetadata> r : records) {
                    cache.put(r.key(), r.value());
                }
                done = partitions.stream().allMatch(tp ->
                    consumer.position(tp) >= endOffsets.get(tp));
            }
        }
        LOG.info("Bootstrap complete topic={} entries={}", topic, cache.size());
        return cache;
    }
}

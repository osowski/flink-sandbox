package com.example;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.reader.deserializer.KafkaRecordDeserializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.formats.avro.registry.confluent.ConfluentRegistryAvroDeserializationSchema;
import org.apache.flink.formats.avro.registry.confluent.ConfluentRegistryAvroSerializationSchema;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.ProcessFunction;
import org.apache.flink.util.Collector;
import org.apache.kafka.clients.consumer.ConsumerRecord;

import com.confluent.examples.sensors.SensorEvent;
import com.confluent.examples.sensors.ProcessedSensorEvent;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;

public class KafkaFlinkJob {

    /**
     * Substitution cipher mapping for CPU-intensive character transformation
     */
    private static final Map<Character, Character> CIPHER_MAP = new HashMap<>();

    static {
        // Build a substitution cipher (ROT13-style with custom mappings)
        String alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 :-.,/";
        String substitution = "NOPQRSTUVWXYZABCDEFGHIJKLM9876543210 -:.,/";

        for (int i = 0; i < alphabet.length(); i++) {
            CIPHER_MAP.put(alphabet.charAt(i), substitution.charAt(i));
        }
    }

    /**
     * Apply substitution cipher to input string
     */
    private static String applySubstitutionCipher(String input) {
        StringBuilder result = new StringBuilder(input.length());
        for (char c : input.toCharArray()) {
            result.append(CIPHER_MAP.getOrDefault(c, c));
        }
        return result.toString();
    }

    /**
     * Custom deserializer that extracts both key and value from Kafka records.
     * Key: plain UTF-8 string. Value: Avro deserialization via Schema Registry.
     */
    public static class KeyValueDeserializer implements KafkaRecordDeserializationSchema<Tuple2<String, SensorEvent>> {
        private final ConfluentRegistryAvroDeserializationSchema<SensorEvent> avroDeserializer;

        public KeyValueDeserializer(String schemaRegistryUrl, Map<String, String> schemaRegistryConfig) {
            this.avroDeserializer = ConfluentRegistryAvroDeserializationSchema.forSpecific(
                SensorEvent.class,
                schemaRegistryUrl,
                schemaRegistryConfig
            );
        }

        @Override
        public void deserialize(ConsumerRecord<byte[], byte[]> record, Collector<Tuple2<String, SensorEvent>> out) throws IOException {
            String key = record.key() != null ? new String(record.key(), StandardCharsets.UTF_8) : null;
            SensorEvent value = record.value() != null ? avroDeserializer.deserialize(record.value()) : null;
            out.collect(Tuple2.of(key, value));
        }

        @Override
        public TypeInformation<Tuple2<String, SensorEvent>> getProducedType() {
            return Types.TUPLE(Types.STRING, Types.GENERIC(SensorEvent.class));
        }
    }

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

        // Get Flink configuration to read kafka.* / schema.registry.* properties
        org.apache.flink.configuration.Configuration flinkConfig =
            (org.apache.flink.configuration.Configuration) env.getConfiguration();

        // Extract all kafka.* properties from Flink configuration
        // These properties are set in FlinkApplication spec.flinkConfiguration
        Properties kafkaProps = new Properties();
        for (String key : flinkConfig.keySet()) {
            if (key.startsWith("kafka.")) {
                // Remove "kafka." prefix to get actual Kafka client property name
                String kafkaKey = key.substring(6);
                kafkaProps.setProperty(kafkaKey, flinkConfig.getString(key, ""));
            }
        }

        // Extract all schema.registry.* properties from Flink configuration
        // (e.g. schema.registry.bearer.auth.* for OAuth-secured Schema Registry)
        Map<String, String> schemaRegistryConfig = new HashMap<>();
        for (String key : flinkConfig.keySet()) {
            if (key.startsWith("schema.registry.")) {
                schemaRegistryConfig.put(key, flinkConfig.getString(key, ""));
            }
        }

        // Get configuration from Flink config with environment variable fallback
        // This maintains backward compatibility with existing deployments
        String kafkaBootstrapServers = flinkConfig.getString("kafka.bootstrap.servers",
                System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka.kafka.svc.cluster.local:9092"));
        String kafkaTopic = flinkConfig.getString("kafka.input.topic",
                System.getenv().getOrDefault("KAFKA_TOPIC", "autoscale-demo"));
        String consumerGroup = flinkConfig.getString("kafka.consumer.group.id",
                System.getenv().getOrDefault("KAFKA_CONSUMER_GROUP", "flink-consumer"));
        String schemaRegistryUrl = flinkConfig.getString("schema.registry.url",
                System.getenv().getOrDefault("SCHEMA_REGISTRY_URL", "http://schemaregistry.kafka.svc.cluster.local:8081"));

        System.out.println("Starting Kafka Flink Job");
        System.out.println("Kafka Bootstrap Servers: " + kafkaBootstrapServers);
        System.out.println("Kafka Topic: " + kafkaTopic);
        System.out.println("Consumer Group: " + consumerGroup);
        System.out.println("Kafka Properties: " + kafkaProps.size() + " properties loaded from Flink configuration");
        System.out.println("Schema Registry URL: " + schemaRegistryUrl);
        System.out.println("Schema Registry Properties: " + schemaRegistryConfig.size() + " properties loaded from Flink configuration");

        // Disable operator chaining for better visibility in Flink UI
        env.disableOperatorChaining();

        // Configure Kafka source with key-value deserialization.
        // Note: no explicit setStartingOffsets() call here — the job respects
        // whatever kafka.consumer.auto.offset.reset is set in FlinkApplication
        // config (or the Kafka client default) rather than hardcoding a value.
        KafkaSource<Tuple2<String, SensorEvent>> source = KafkaSource.<Tuple2<String, SensorEvent>>builder()
                .setBootstrapServers(kafkaBootstrapServers)
                .setTopics(kafkaTopic)
                .setGroupId(consumerGroup)
                .setProperties(kafkaProps)  // Pass all Kafka properties including security config
                .setDeserializer(new KeyValueDeserializer(schemaRegistryUrl, schemaRegistryConfig))
                .build();

        // Create data stream from Kafka
        DataStream<Tuple2<String, SensorEvent>> stream = env.fromSource(source, WatermarkStrategy.noWatermarks(), "Kafka Source")
                .disableChaining();

        // Process records with CPU-intensive transformations
        DataStream<Tuple2<String, ProcessedSensorEvent>> processedStream = stream.process(new ProcessFunction<Tuple2<String, SensorEvent>, Tuple2<String, ProcessedSensorEvent>>() {
            @Override
            public void processElement(Tuple2<String, SensorEvent> keyValue, Context ctx, Collector<Tuple2<String, ProcessedSensorEvent>> out) throws Exception {
                String key = keyValue.f0;
                SensorEvent sensorEvent = keyValue.f1;

                if (sensorEvent == null) {
                    return;  // Skip null events
                }

                try {
                    // Step 1: Join timestamp and location
                    String combined = sensorEvent.getTimestamp() + ":" + sensorEvent.getLocation();

                    // Step 2: Convert to uppercase
                    String uppercase = combined.toUpperCase();

                    // Step 3: Apply substitution cipher (CPU-intensive character-by-character transformation)
                    String ciphered = applySubstitutionCipher(uppercase);

                    // Step 4: Additional CPU work - apply cipher multiple times for extra load
                    for (int i = 0; i < 5; i++) {
                        ciphered = applySubstitutionCipher(ciphered);
                    }

                    // Step 5: Base64 encode the result
                    String encoded = Base64.getEncoder().encodeToString(ciphered.getBytes());

                    // Step 6: Emit the enriched event, same key
                    ProcessedSensorEvent processedEvent = ProcessedSensorEvent.newBuilder()
                            .setTimestamp(sensorEvent.getTimestamp())
                            .setType(sensorEvent.getType())
                            .setLocation(sensorEvent.getLocation())
                            .setValue(sensorEvent.getValue())
                            .setStatus(sensorEvent.getStatus())
                            .setId(sensorEvent.getId())
                            .setEncoded(encoded)
                            .setError(null)
                            .setOriginal(null)
                            .build();

                    out.collect(Tuple2.of(key, processedEvent));
                } catch (Exception e) {
                    // Emit an error record instead of dropping it — mirrors the
                    // original JSON version's error object, now as a valid
                    // ProcessedSensorEvent (error/original are the fields that
                    // exist specifically to carry this).
                    ProcessedSensorEvent errorEvent = ProcessedSensorEvent.newBuilder()
                            .setTimestamp(sensorEvent.getTimestamp() != null ? sensorEvent.getTimestamp() : "unknown")
                            .setType(sensorEvent.getType() != null ? sensorEvent.getType() : "unknown")
                            .setLocation(sensorEvent.getLocation() != null ? sensorEvent.getLocation() : "unknown")
                            .setValue(sensorEvent.getValue())
                            .setStatus(sensorEvent.getStatus() != null ? sensorEvent.getStatus() : "unknown")
                            .setId(sensorEvent.getId() != null ? sensorEvent.getId() : "unknown")
                            .setEncoded(null)
                            .setError(e.getMessage())
                            .setOriginal(sensorEvent.toString())
                            .build();
                    out.collect(Tuple2.of(key, errorEvent));
                }
            }
        })
        .name("Process Records")
        .disableChaining();

        // Configure Kafka sink to write processed messages back to Kafka with keys
        String outputTopic = flinkConfig.getString("kafka.output.topic",
                System.getenv().getOrDefault("KAFKA_OUTPUT_TOPIC", "autoscale-demo-out"));

        // Avro serializer for ProcessedSensorEvent values. Subject defaults to
        // "<outputTopic>-value" (Confluent's TopicNameStrategy), so it tracks
        // whatever output topic name this deployment is configured with.
        ConfluentRegistryAvroSerializationSchema<ProcessedSensorEvent> avroSerializer =
                ConfluentRegistryAvroSerializationSchema.forSpecific(
                        ProcessedSensorEvent.class,
                        outputTopic + "-value",
                        schemaRegistryUrl,
                        schemaRegistryConfig
                );

        KafkaSink<Tuple2<String, ProcessedSensorEvent>> sink = KafkaSink.<Tuple2<String, ProcessedSensorEvent>>builder()
                .setBootstrapServers(kafkaBootstrapServers)
                .setKafkaProducerConfig(kafkaProps)  // Pass all Kafka properties including security config
                .setRecordSerializer(KafkaRecordSerializationSchema.builder()
                        .setTopic(outputTopic)
                        .setKeySerializationSchema(new org.apache.flink.api.common.serialization.SerializationSchema<Tuple2<String, ProcessedSensorEvent>>() {
                            @Override
                            public byte[] serialize(Tuple2<String, ProcessedSensorEvent> element) {
                                return element.f0 != null ? element.f0.getBytes(StandardCharsets.UTF_8) : null;
                            }
                        })
                        .setValueSerializationSchema(new org.apache.flink.api.common.serialization.SerializationSchema<Tuple2<String, ProcessedSensorEvent>>() {
                            @Override
                            public byte[] serialize(Tuple2<String, ProcessedSensorEvent> element) {
                                try {
                                    return element.f1 != null ? avroSerializer.serialize(element.f1) : null;
                                } catch (Exception e) {
                                    throw new RuntimeException("Failed to serialize ProcessedSensorEvent", e);
                                }
                            }
                        })
                        .build()
                )
                .build();

        // Write processed records to output topic
        processedStream.sinkTo(sink)
                .name("Sink: Print Output")
                .disableChaining();

        // Execute the job
        env.execute("Kafka Flink Autoscale Job");
    }
}

package com.example;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
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
     * Custom deserializer that extracts both key and value from Kafka records
     * Key: String deserialization
     * Value: Avro deserialization with Schema Registry
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
            SensorEvent value = null;

            if (record.value() != null) {
                value = avroDeserializer.deserialize(record.value());
            }

            out.collect(Tuple2.of(key, value));
        }

        @Override
        public TypeInformation<Tuple2<String, SensorEvent>> getProducedType() {
            return Types.TUPLE(Types.STRING, Types.GENERIC(SensorEvent.class));
        }
    }

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

        // Get Flink configuration to read kafka.* properties
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

        // Extract Schema Registry configuration from Flink config
        // Properties starting with "schema.registry." are passed to Avro deserializer/serializer
        Map<String, String> schemaRegistryConfig = new HashMap<>();
        for (String key : flinkConfig.keySet()) {
            if (key.startsWith("schema.registry.")) {
                schemaRegistryConfig.put(key, flinkConfig.getString(key, ""));
            }
        }

        // Get Schema Registry URL from Flink config
        String schemaRegistryUrl = flinkConfig.getString("schema.registry.url",
                System.getenv().getOrDefault("SCHEMA_REGISTRY_URL", "http://schemaregistry.kafka.svc.cluster.local:8081"));

        // Get configuration from Flink config with environment variable fallback
        // This maintains backward compatibility with existing deployments
        String kafkaBootstrapServers = flinkConfig.getString("kafka.bootstrap.servers",
                System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka.kafka.svc.cluster.local:9092"));
        String kafkaTopic = flinkConfig.getString("kafka.input.topic",
                System.getenv().getOrDefault("KAFKA_TOPIC", "autoscale-demo"));
        String consumerGroup = flinkConfig.getString("kafka.consumer.group.id",
                System.getenv().getOrDefault("KAFKA_CONSUMER_GROUP", "flink-consumer"));

        System.out.println("Starting Kafka Flink Job");
        System.out.println("Kafka Bootstrap Servers: " + kafkaBootstrapServers);
        System.out.println("Kafka Topic: " + kafkaTopic);
        System.out.println("Consumer Group: " + consumerGroup);
        System.out.println("Kafka Properties: " + kafkaProps.size() + " properties loaded from Flink configuration");
        System.out.println("Schema Registry URL: " + schemaRegistryUrl);
        System.out.println("Schema Registry Properties: " + schemaRegistryConfig.size() + " properties loaded from Flink configuration");

        // Disable operator chaining for better visibility in Flink UI
        env.disableOperatorChaining();

        // Configure Kafka source with key-value deserialization
        // Now includes all kafka.* properties from Flink configuration for OAuth/security support
        // Value deserialization uses Avro with Schema Registry
        KafkaSource<Tuple2<String, SensorEvent>> source = KafkaSource.<Tuple2<String, SensorEvent>>builder()
                .setBootstrapServers(kafkaBootstrapServers)
                .setTopics(kafkaTopic)
                .setGroupId(consumerGroup)
                .setProperties(kafkaProps)  // Pass all Kafka properties including security config
                .setStartingOffsets(OffsetsInitializer.latest())
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
                    // Extract timestamp and location fields from Avro object
                    String timestamp = sensorEvent.getTimestamp() != null ? sensorEvent.getTimestamp().toString() : "unknown";
                    String location = sensorEvent.getLocation() != null ? sensorEvent.getLocation().toString() : "unknown";

                    // Step 1: Join timestamp and location
                    String combined = timestamp + ":" + location;

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

                    // Step 6: Create ProcessedSensorEvent with original fields plus encoded field
                    ProcessedSensorEvent processedEvent = ProcessedSensorEvent.newBuilder()
                            .setTimestamp(sensorEvent.getTimestamp())
                            .setType(sensorEvent.getType())
                            .setLocation(sensorEvent.getLocation())
                            .setValue(sensorEvent.getValue())
                            .setStatus(sensorEvent.getStatus())
                            .setId(sensorEvent.getId())
                            .setEncoded(encoded)
                            .build();

                    // Emit the processed event with the same key
                    out.collect(Tuple2.of(key, processedEvent));
                } catch (Exception e) {
                    // Log processing errors but continue - skip failed records
                    System.err.println("Error processing record: " + e.getMessage());
                }
            }
        })
        .name("Process Records")
        .disableChaining();

        // Configure Kafka sink to write processed messages back to Kafka with keys
        String outputTopic = flinkConfig.getString("kafka.output.topic",
                System.getenv().getOrDefault("KAFKA_OUTPUT_TOPIC", "autoscale-demo-out"));

        // Create Avro serializer for ProcessedSensorEvent values
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

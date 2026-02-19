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
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.ProcessFunction;
import org.apache.flink.util.Collector;
import org.apache.kafka.clients.consumer.ConsumerRecord;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

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
     */
    public static class KeyValueDeserializer implements KafkaRecordDeserializationSchema<Tuple2<String, String>> {
        @Override
        public void deserialize(ConsumerRecord<byte[], byte[]> record, Collector<Tuple2<String, String>> out) throws IOException {
            String key = record.key() != null ? new String(record.key(), StandardCharsets.UTF_8) : null;
            String value = record.value() != null ? new String(record.value(), StandardCharsets.UTF_8) : null;
            out.collect(Tuple2.of(key, value));
        }

        @Override
        public TypeInformation<Tuple2<String, String>> getProducedType() {
            return Types.TUPLE(Types.STRING, Types.STRING);
        }
    }

    public static void main(String[] args) throws Exception {
        // Get configuration from environment variables
        String kafkaBootstrapServers = System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka.kafka.svc.cluster.local:9092");
        String kafkaTopic = System.getenv().getOrDefault("KAFKA_TOPIC", "autoscale-demo");
        String consumerGroup = System.getenv().getOrDefault("KAFKA_CONSUMER_GROUP", "flink-consumer");

        System.out.println("Starting Kafka Flink Job");
        System.out.println("Kafka Bootstrap Servers: " + kafkaBootstrapServers);
        System.out.println("Kafka Topic: " + kafkaTopic);
        System.out.println("Consumer Group: " + consumerGroup);

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

        // Disable operator chaining for better visibility in Flink UI
        env.disableOperatorChaining();

        // Configure Kafka source with key-value deserialization
        KafkaSource<Tuple2<String, String>> source = KafkaSource.<Tuple2<String, String>>builder()
                .setBootstrapServers(kafkaBootstrapServers)
                .setTopics(kafkaTopic)
                .setGroupId(consumerGroup)
                .setStartingOffsets(OffsetsInitializer.latest())
                .setDeserializer(new KeyValueDeserializer())
                .build();

        // Create data stream from Kafka
        DataStream<Tuple2<String, String>> stream = env.fromSource(source, WatermarkStrategy.noWatermarks(), "Kafka Source")
                .disableChaining();

        // Process records with CPU-intensive transformations
        DataStream<Tuple2<String, String>> processedStream = stream.process(new ProcessFunction<Tuple2<String, String>, Tuple2<String, String>>() {
            private final ObjectMapper objectMapper = new ObjectMapper();

            @Override
            public void processElement(Tuple2<String, String> keyValue, Context ctx, Collector<Tuple2<String, String>> out) throws Exception {
                String key = keyValue.f0;
                String value = keyValue.f1;

                try {
                    // Parse JSON message
                    JsonNode jsonNode = objectMapper.readTree(value);

                    // Extract timestamp and location fields
                    String timestamp = jsonNode.has("timestamp") ? jsonNode.get("timestamp").asText() : "unknown";
                    String location = jsonNode.has("location") ? jsonNode.get("location").asText() : "unknown";

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

                    // Step 6: Add the encoded field to the original message
                    com.fasterxml.jackson.databind.node.ObjectNode outputNode;
                    if (jsonNode.isObject()) {
                        outputNode = (com.fasterxml.jackson.databind.node.ObjectNode) jsonNode;
                    } else {
                        outputNode = objectMapper.createObjectNode();
                        outputNode.put("original", value);
                    }
                    outputNode.put("encoded", encoded);

                    // Emit the updated JSON message with the same key
                    out.collect(Tuple2.of(key, objectMapper.writeValueAsString(outputNode)));
                } catch (Exception e) {
                    // Log parsing errors but continue processing - emit error as JSON
                    com.fasterxml.jackson.databind.node.ObjectNode errorNode = objectMapper.createObjectNode();
                    errorNode.put("error", e.getMessage());
                    errorNode.put("original", value);
                    out.collect(Tuple2.of(key, objectMapper.writeValueAsString(errorNode)));
                }
            }
        })
        .name("Process Records")
        .disableChaining();

        // Configure Kafka sink to write processed messages back to Kafka with keys
        String outputTopic = System.getenv().getOrDefault("KAFKA_OUTPUT_TOPIC", "autoscale-demo-out");

        KafkaSink<Tuple2<String, String>> sink = KafkaSink.<Tuple2<String, String>>builder()
                .setBootstrapServers(kafkaBootstrapServers)
                .setRecordSerializer(KafkaRecordSerializationSchema.builder()
                        .setTopic(outputTopic)
                        .setKeySerializationSchema(new org.apache.flink.api.common.serialization.SerializationSchema<Tuple2<String, String>>() {
                            @Override
                            public byte[] serialize(Tuple2<String, String> element) {
                                return element.f0 != null ? element.f0.getBytes(StandardCharsets.UTF_8) : null;
                            }
                        })
                        .setValueSerializationSchema(new org.apache.flink.api.common.serialization.SerializationSchema<Tuple2<String, String>>() {
                            @Override
                            public byte[] serialize(Tuple2<String, String> element) {
                                return element.f1 != null ? element.f1.getBytes(StandardCharsets.UTF_8) : null;
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

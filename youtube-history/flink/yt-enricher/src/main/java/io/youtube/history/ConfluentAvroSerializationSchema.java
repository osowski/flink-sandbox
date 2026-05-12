package io.youtube.history;

import io.confluent.kafka.serializers.KafkaAvroSerializer;
import org.apache.avro.specific.SpecificRecord;
import org.apache.flink.api.common.serialization.SerializationSchema;

import java.util.HashMap;
import java.util.Map;

public class ConfluentAvroSerializationSchema<T extends SpecificRecord>
        implements SerializationSchema<T> {

    private final String topicName;
    private final String srUrl;
    private final String srApiKey;
    private final String srApiSecret;
    private transient KafkaAvroSerializer serializer;

    public ConfluentAvroSerializationSchema(String topicName,
            String srUrl, String srApiKey, String srApiSecret) {
        this.topicName   = topicName;
        this.srUrl       = srUrl;
        this.srApiKey    = srApiKey;
        this.srApiSecret = srApiSecret;
    }

    @Override
    public void open(InitializationContext ctx) {
        Map<String, Object> config = new HashMap<>();
        config.put("schema.registry.url", srUrl);
        config.put("basic.auth.credentials.source", "USER_INFO");
        config.put("basic.auth.user.info", srApiKey + ":" + srApiSecret);
        serializer = new KafkaAvroSerializer();
        serializer.configure(config, false);
    }

    @Override
    public byte[] serialize(T record) {
        return serializer.serialize(topicName, record);
    }
}

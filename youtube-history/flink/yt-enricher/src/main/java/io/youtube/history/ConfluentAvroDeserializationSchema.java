package io.youtube.history;

import io.confluent.kafka.serializers.KafkaAvroDeserializer;
import org.apache.avro.specific.SpecificRecord;
import org.apache.flink.api.common.serialization.DeserializationSchema;
import org.apache.flink.api.common.typeinfo.TypeInformation;

import java.util.HashMap;
import java.util.Map;

public class ConfluentAvroDeserializationSchema<T extends SpecificRecord>
        implements DeserializationSchema<T> {

    private final Class<T> clazz;
    private final String srUrl;
    private final String srApiKey;
    private final String srApiSecret;
    private transient KafkaAvroDeserializer deserializer;

    public ConfluentAvroDeserializationSchema(Class<T> clazz,
            String srUrl, String srApiKey, String srApiSecret) {
        this.clazz       = clazz;
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
        config.put("specific.avro.reader", true);
        deserializer = new KafkaAvroDeserializer();
        deserializer.configure(config, false);
    }

    @Override
    @SuppressWarnings("unchecked")
    public T deserialize(byte[] bytes) {
        // null topic is fine: KafkaAvroDeserializer resolves the schema from the wire-format
        // magic byte + schema ID, not from the topic name, when specific.avro.reader=true.
        return (T) deserializer.deserialize(null, bytes);
    }

    @Override
    public boolean isEndOfStream(T t) { return false; }

    @Override
    public TypeInformation<T> getProducedType() {
        return TypeInformation.of(clazz);
    }
}

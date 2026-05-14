package io.youtube.history;

import org.apache.flink.api.common.functions.OpenContext;
import org.apache.flink.api.common.state.ListState;
import org.apache.flink.api.common.state.ListStateDescriptor;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.util.Collector;
import org.apache.flink.util.OutputTag;
import org.apache.kafka.clients.producer.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.*;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

public class EnrichmentFunction extends KeyedProcessFunction<String, RawWatchEvent, EnrichedWatchEvent> {

    private static final Logger LOG = LoggerFactory.getLogger(EnrichmentFunction.class);

    static final String SINGLETON_KEY = "all";

    // Events that cannot be enriched (video unavailable/private/deleted) are routed here
    // rather than dropped silently. Sink this stream to yt.raw.watch.events.dlq for inspection.
    public static final OutputTag<RawWatchEvent> DLQ_TAG =
        new OutputTag<RawWatchEvent>("dlq") {};

    private static final int BATCH_SIZE = 50;
    private static final long BATCH_WINDOW_MS = 5_000L;

    // Preloaded cache: rebuilt from MetadataBootstrap on restart, not Flink state
    private final Map<String, VideoMetadata> metadataCache;

    private final String apiKey;
    private final String bootstrapServers;
    private final String kafkaApiKey;
    private final String kafkaApiSecret;
    private final String srUrl;
    private final String srApiKey;
    private final String srApiSecret;
    private final String metadataTopic;
    private final SerializableLongSupplier clock;

    // Flink managed state: checkpointed and restored on restart
    private transient ListState<RawWatchEvent> pendingEventsState;
    private transient ValueState<Long> timerTimestampState;

    // Transient resources: reconstructed in open()
    private transient YouTubeApiClientPort apiClient;
    private transient KafkaProducer<String, VideoMetadata> metadataProducer;

    // Incremental unique-video-ID count — avoids O(n) ListState scan per event.
    // Starts empty on restart; timer restored via timerTimestampState ensures eventual flush.
    private transient Set<String> pendingVideoIds;

    public EnrichmentFunction(
            Map<String, VideoMetadata> preloadedCache,
            String apiKey,
            String bootstrapServers,
            String kafkaApiKey,
            String kafkaApiSecret,
            String srUrl,
            String srApiKey,
            String srApiSecret,
            String metadataTopic,
            SerializableLongSupplier clock) {
        this.clock            = clock != null ? clock : System::currentTimeMillis;
        this.metadataCache    = preloadedCache != null ? preloadedCache : new HashMap<>();
        this.apiKey           = apiKey;
        this.bootstrapServers = bootstrapServers;
        this.kafkaApiKey      = kafkaApiKey;
        this.kafkaApiSecret   = kafkaApiSecret;
        this.srUrl            = srUrl;
        this.srApiKey         = srApiKey;
        this.srApiSecret      = srApiSecret;
        this.metadataTopic    = metadataTopic;
    }

    @Override
    public void open(OpenContext openContext) throws Exception {
        pendingEventsState = getRuntimeContext().getListState(
            new ListStateDescriptor<>("pending-events", RawWatchEvent.class));
        timerTimestampState = getRuntimeContext().getState(
            new ValueStateDescriptor<>("timer-ts", Long.class));

        if (apiKey != null) {
            apiClient = new YouTubeApiClient(apiKey);
        }
        if (bootstrapServers != null) {
            metadataProducer = buildMetadataProducer();
        }

        pendingVideoIds = new HashSet<>();

        LOG.info("EnrichmentFunction open cacheSize={} apiClientReady={}", metadataCache.size(), apiClient != null);
    }

    void setApiClientForTest(YouTubeApiClientPort client) {
        this.apiClient = client;
    }

    @Override
    public void processElement(RawWatchEvent event, Context ctx, Collector<EnrichedWatchEvent> out) throws Exception {
        String videoId = event.getVideoId().toString();

        VideoMetadata cached = metadataCache.get(videoId);
        if (cached != null) {
            LOG.debug("Cache hit videoId={}", videoId);
            out.collect(enrich(event, cached));
            return;
        }

        pendingEventsState.add(event);
        pendingVideoIds.add(videoId);

        // Register a processing-time timer when the first event of a new batch arrives
        if (timerTimestampState.value() == null) {
            long fireAt = ctx.timerService().currentProcessingTime() + BATCH_WINDOW_MS;
            ctx.timerService().registerProcessingTimeTimer(fireAt);
            timerTimestampState.update(fireAt);
            LOG.debug("Batch timer registered fireAt={}", fireAt);
        }

        // Flush immediately if the batch has reached the YouTube API batch limit
        if (pendingVideoIds.size() >= BATCH_SIZE) {
            Long fireAt = timerTimestampState.value();
            if (fireAt != null) {
                ctx.timerService().deleteProcessingTimeTimer(fireAt);
            }
            flushBatch(ctx, out);
        }
    }

    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<EnrichedWatchEvent> out) throws Exception {
        flushBatch(ctx, out);
    }

    private void flushBatch(Context ctx, Collector<EnrichedWatchEvent> out) throws Exception {
        Map<String, List<RawWatchEvent>> pendingByVideoId = new HashMap<>();
        for (RawWatchEvent event : pendingEventsState.get()) {
            pendingByVideoId.computeIfAbsent(
                event.getVideoId().toString(), k -> new ArrayList<>()).add(event);
        }

        pendingEventsState.clear();
        timerTimestampState.clear();
        pendingVideoIds.clear();

        if (pendingByVideoId.isEmpty()) return;

        if (apiClient == null) {
            throw new IllegalStateException("apiClient not initialized; YOUTUBE_API_KEY was not provided");
        }

        // State is cleared before the API call. If fetchBatch throws after all retries,
        // Flink restores from the last checkpoint — the clear above is not committed because
        // this execution never completed successfully. Events between the last checkpoint and
        // this failure will be replayed from Kafka on recovery.
        long flushStart = clock.getAsLong();
        List<String> batchIds = new ArrayList<>(pendingByVideoId.keySet());
        Map<String, ApiVideoData> apiResults = apiClient.fetchBatch(batchIds);
        LOG.info("Flushed batch: videoIds={} durationMs={}", batchIds.size(), clock.getAsLong() - flushStart);

        for (Map.Entry<String, List<RawWatchEvent>> entry : pendingByVideoId.entrySet()) {
            String videoId = entry.getKey();
            ApiVideoData data = apiResults.get(videoId);
            List<RawWatchEvent> pending = entry.getValue();

            if (data == null) {
                LOG.warn("Video unavailable, routing to DLQ: videoId={} droppedEvents={}", videoId, pending.size());
                for (RawWatchEvent evt : pending) {
                    ctx.output(DLQ_TAG, evt);
                }
                continue;
            }

            VideoMetadata metadata = buildMetadata(videoId, data);
            String title = pending.get(0).getTitle().toString();
            ClassificationService.classifyWithTitle(metadata, title);
            metadataCache.put(videoId, metadata);

            if (metadataProducer != null) {
                try {
                    metadataProducer.send(new ProducerRecord<>(metadataTopic, videoId, metadata))
                        .get(10, TimeUnit.SECONDS);
                } catch (ExecutionException | TimeoutException e) {
                    throw new RuntimeException("Failed to publish VideoMetadata videoId=" + videoId, e);
                }
            }

            for (RawWatchEvent evt : pending) {
                out.collect(enrich(evt, metadata));
            }
        }
    }

    private VideoMetadata buildMetadata(String videoId, ApiVideoData data) {
        VideoMetadata m = new VideoMetadata();
        m.setVideoId(videoId);
        m.setCategoryId(data.categoryId());
        m.setCategoryName(data.categoryName());
        m.setTopicCategories(new ArrayList<>(data.topicCategories()));
        m.setTags(new ArrayList<>(data.tags()));
        m.setChannelTitle(data.channelTitle());
        m.setIsMusic(false);
        m.setClassificationReason("none");
        m.setFetchedAt(clock.getAsLong());
        return m;
    }

    private EnrichedWatchEvent enrich(RawWatchEvent event, VideoMetadata meta) {
        EnrichedWatchEvent e = new EnrichedWatchEvent();
        e.setVideoId(event.getVideoId());
        e.setUrl(event.getUrl());
        e.setTitle(event.getTitle());
        e.setChannelName(event.getChannelName());
        e.setWatchedAt(event.getWatchedAt());
        e.setCategoryId(meta.getCategoryId());
        e.setCategoryName(meta.getCategoryName());
        e.setTopicCategories(meta.getTopicCategories());
        e.setTags(meta.getTags());
        e.setChannelTitle(meta.getChannelTitle());
        e.setIsMusic(meta.getIsMusic());
        e.setClassificationReason(meta.getClassificationReason());
        e.setProducedAt(clock.getAsLong());
        return e;
    }

    private KafkaProducer<String, VideoMetadata> buildMetadataProducer() {
        Properties props = new Properties();
        props.put("bootstrap.servers", bootstrapServers);
        props.put("security.protocol", "SASL_SSL");
        props.put("sasl.mechanism", "PLAIN");
        props.put("sasl.jaas.config",
            "org.apache.kafka.common.security.plain.PlainLoginModule required " +
            "username=\"" + kafkaApiKey + "\" password=\"" + kafkaApiSecret + "\";");
        props.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
        props.put("value.serializer", "io.confluent.kafka.serializers.KafkaAvroSerializer");
        props.put("schema.registry.url", srUrl);
        props.put("basic.auth.credentials.source", "USER_INFO");
        props.put("basic.auth.user.info", srApiKey + ":" + srApiSecret);
        props.put("auto.register.schemas", "false");
        props.put("use.latest.version", "true");
        props.put("acks", "all");
        props.put("enable.idempotence", "true");
        return new KafkaProducer<>(props);
    }

    @Override
    public void close() throws Exception {
        if (metadataProducer != null) metadataProducer.close();
    }
}

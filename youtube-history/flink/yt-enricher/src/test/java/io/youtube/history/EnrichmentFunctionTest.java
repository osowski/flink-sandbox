package io.youtube.history;

import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.streaming.api.operators.KeyedProcessOperator;
import org.apache.flink.streaming.runtime.streamrecord.StreamRecord;
import org.apache.flink.streaming.util.KeyedOneInputStreamOperatorTestHarness;
import org.junit.jupiter.api.Test;

import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.*;

class EnrichmentFunctionTest {

    private RawWatchEvent rawEvent(String videoId) {
        RawWatchEvent e = new RawWatchEvent();
        e.setVideoId(videoId);
        e.setUrl("https://www.youtube.com/watch?v=" + videoId);
        e.setTitle("Test Video " + videoId);
        e.setChannelName("Test Channel");
        e.setChannelUrl("https://www.youtube.com/channel/UC123");
        e.setWatchedAt("2024-01-15T10:30:00Z");
        e.setUsername("no-username-provided");
        e.setProducedAt(System.currentTimeMillis());
        return e;
    }

    private VideoMetadata musicMetadata(String videoId) {
        VideoMetadata m = new VideoMetadata();
        m.setVideoId(videoId);
        m.setCategoryId("10");
        m.setCategoryName("Music");
        m.setTopicCategories(Collections.emptyList());
        m.setTags(Collections.emptyList());
        m.setChannelTitle("Test Channel");
        m.setIsMusic(true);
        m.setClassificationReason("category_id");
        m.setFetchedAt(System.currentTimeMillis());
        return m;
    }

    private KeyedOneInputStreamOperatorTestHarness<String, RawWatchEvent, EnrichedWatchEvent>
            buildHarness(EnrichmentFunction fn) throws Exception {
        return new KeyedOneInputStreamOperatorTestHarness<>(
            new KeyedProcessOperator<>(fn),
            event -> EnrichmentFunction.SINGLETON_KEY,
            Types.STRING
        );
    }

    private List<EnrichedWatchEvent> outputValues(
            KeyedOneInputStreamOperatorTestHarness<?, ?, EnrichedWatchEvent> harness) {
        return harness.getOutput().stream()
            .filter(r -> r instanceof StreamRecord)
            .map(r -> ((StreamRecord<EnrichedWatchEvent>) r).getValue())
            .collect(Collectors.toList());
    }

    @Test
    void cacheHitEnrichesAndEmitsImmediately() throws Exception {
        Map<String, VideoMetadata> preloadedCache = new HashMap<>();
        preloadedCache.put("vid1", musicMetadata("vid1"));

        EnrichmentFunction fn = new EnrichmentFunction(
            preloadedCache,
            "fake-api-key",
            null, null, null, null, null, null,
            null
        );

        try (var harness = buildHarness(fn)) {
            harness.open();
            harness.processElement(rawEvent("vid1"), 0L);

            List<EnrichedWatchEvent> output = outputValues(harness);
            assertEquals(1, output.size());
            assertEquals("vid1", output.get(0).getVideoId().toString());
            assertTrue(output.get(0).getIsMusic());
        }
    }

    @Test
    void cacheMissBuffersEventsAndFlushesOnTimer() throws Exception {
        YouTubeApiClientPort mockApi = videoIds -> {
            Map<String, ApiVideoData> result = new HashMap<>();
            for (String id : videoIds) {
                result.put(id, new ApiVideoData(
                    "10", "Music", "Test Channel",
                    Collections.emptyList(), Collections.emptyList()
                ));
            }
            return result;
        };

        EnrichmentFunction fn = new EnrichmentFunction(
            new HashMap<>(),
            null, null, null, null, null, null, null,
            null
        );
        fn.setApiClientForTest(mockApi);

        try (var harness = buildHarness(fn)) {
            harness.open();

            // Both events are cache misses — buffered, no output yet
            harness.processElement(rawEvent("vid2"), 0L);
            harness.processElement(rawEvent("vid3"), 0L);
            assertTrue(outputValues(harness).isEmpty());

            // Advance processing time past BATCH_WINDOW_MS — timer fires, both flushed
            harness.setProcessingTime(6_000L);

            List<EnrichedWatchEvent> output = outputValues(harness);
            assertEquals(2, output.size());
            assertTrue(output.stream().anyMatch(e -> e.getVideoId().toString().equals("vid2")));
            assertTrue(output.stream().anyMatch(e -> e.getVideoId().toString().equals("vid3")));
        }
    }

    @Test
    void unavailableVideoRoutesToDlq() throws Exception {
        // API returns empty map — video is deleted/private/unavailable
        YouTubeApiClientPort mockApi = videoIds -> Map.of();

        EnrichmentFunction fn = new EnrichmentFunction(
            new HashMap<>(),
            null, null, null, null, null, null, null,
            null
        );
        fn.setApiClientForTest(mockApi);

        try (var harness = buildHarness(fn)) {
            harness.open();
            harness.processElement(rawEvent("deleted-vid"), 0L);
            harness.setProcessingTime(6_000L);

            // Main output is empty — no enriched event emitted
            assertTrue(outputValues(harness).isEmpty());

            // DLQ side output contains the original event
            List<RawWatchEvent> dlq = harness.getSideOutput(EnrichmentFunction.DLQ_TAG).stream()
                .filter(r -> r instanceof StreamRecord)
                .map(r -> ((StreamRecord<RawWatchEvent>) r).getValue())
                .collect(Collectors.toList());
            assertEquals(1, dlq.size());
            assertEquals("deleted-vid", dlq.get(0).getVideoId().toString());
        }
    }

    @Test
    void multipleEventsForSameVideoIdResultInOneApiCall() throws Exception {
        AtomicInteger apiCallCount = new AtomicInteger(0);
        YouTubeApiClientPort mockApi = videoIds -> {
            apiCallCount.incrementAndGet();
            Map<String, ApiVideoData> result = new HashMap<>();
            for (String id : videoIds) {
                result.put(id, new ApiVideoData(
                    "10", "Music", "Test Channel",
                    Collections.emptyList(), Collections.emptyList()
                ));
            }
            return result;
        };

        EnrichmentFunction fn = new EnrichmentFunction(
            new HashMap<>(),
            null, null, null, null, null, null, null,
            null
        );
        fn.setApiClientForTest(mockApi);

        try (var harness = buildHarness(fn)) {
            harness.open();

            // Three events for the same video — should batch into a single API call
            harness.processElement(rawEvent("same-vid"), 0L);
            harness.processElement(rawEvent("same-vid"), 0L);
            harness.processElement(rawEvent("same-vid"), 0L);
            harness.setProcessingTime(6_000L);

            assertEquals(1, apiCallCount.get(), "Expected exactly one API call for repeated videoId");
            assertEquals(3, outputValues(harness).size(), "All three events should be enriched");
        }
    }

    @Test
    void batchSizeTriggerFlushesWithoutWaitingForTimer() throws Exception {
        YouTubeApiClientPort mockApi = videoIds -> {
            Map<String, ApiVideoData> result = new HashMap<>();
            for (String id : videoIds) {
                result.put(id, new ApiVideoData(
                    "22", "Entertainment", "Test Channel",
                    Collections.emptyList(), Collections.emptyList()
                ));
            }
            return result;
        };

        EnrichmentFunction fn = new EnrichmentFunction(
            new HashMap<>(),
            null, null, null, null, null, null, null,
            null
        );
        fn.setApiClientForTest(mockApi);

        try (var harness = buildHarness(fn)) {
            harness.open();

            // Send 50 events for 50 unique video IDs — batch size limit triggers immediate flush
            for (int i = 0; i < 50; i++) {
                harness.processElement(rawEvent("vid-batch-" + i), 0L);
            }

            // Timer has NOT been advanced, but batch is full — output should be present
            List<EnrichedWatchEvent> output = outputValues(harness);
            assertEquals(50, output.size());
        }
    }
}

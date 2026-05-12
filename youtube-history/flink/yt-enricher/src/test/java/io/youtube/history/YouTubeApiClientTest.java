package io.youtube.history;

import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import java.util.List;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;

class YouTubeApiClientTest {

    private MockWebServer server;
    private YouTubeApiClient client;

    @BeforeEach
    void setUp() throws Exception {
        server = new MockWebServer();
        server.start();
        client = new YouTubeApiClient("fake-api-key", server.url("/").toString());
        client.setSleeperForTest(ms -> {}); // no-op: eliminates real Thread.sleep in retry tests
    }

    @AfterEach
    void tearDown() throws Exception {
        server.shutdown();
    }

    @Test
    void fetchReturnsMetadataForKnownVideoId() throws Exception {
        server.enqueue(new MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "application/json")
            .setBody("""
                {
                  "items": [{
                    "id": "dQw4w9WgXcQ",
                    "snippet": {
                      "categoryId": "10",
                      "channelTitle": "Rick Astley",
                      "tags": ["pop", "music"]
                    },
                    "topicDetails": {
                      "topicCategories": ["https://en.wikipedia.org/wiki/Music"]
                    }
                  }]
                }"""));

        Map<String, ApiVideoData> result = client.fetchBatch(List.of("dQw4w9WgXcQ"));

        assertTrue(result.containsKey("dQw4w9WgXcQ"));
        ApiVideoData data = result.get("dQw4w9WgXcQ");
        assertEquals("10", data.categoryId());
        assertEquals("Music", data.categoryName());
        assertEquals("Rick Astley", data.channelTitle());
    }

    @Test
    void fetchReturnsEmptyMapForMissingVideoId() throws Exception {
        server.enqueue(new MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "application/json")
            .setBody("{\"items\": []}"));

        Map<String, ApiVideoData> result = client.fetchBatch(List.of("deleted123"));

        assertTrue(result.isEmpty());
    }

    @Test
    void fetchRetriesOn5xxAndEventuallyThrows() throws Exception {
        server.enqueue(new MockResponse().setResponseCode(503));
        server.enqueue(new MockResponse().setResponseCode(503));
        server.enqueue(new MockResponse().setResponseCode(503));

        assertThrows(RuntimeException.class,
            () -> client.fetchBatch(List.of("vid1")));

        assertEquals(3, server.getRequestCount());
    }

    @Test
    void fetchThrowsImmediatelyOn4xx() throws Exception {
        server.enqueue(new MockResponse().setResponseCode(403));

        RuntimeException ex = assertThrows(RuntimeException.class,
            () -> client.fetchBatch(List.of("vid1")));

        assertTrue(ex.getMessage().contains("Non-retryable HTTP 403"));
        assertEquals(1, server.getRequestCount());
    }
}

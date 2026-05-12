package io.youtube.history;

import okhttp3.HttpUrl;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.Serializable;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

public class YouTubeApiClient implements YouTubeApiClientPort, Serializable {

    private static final Logger LOG = LoggerFactory.getLogger(YouTubeApiClient.class);

    private static final Map<String, String> CATEGORY_NAMES = Map.ofEntries(
        Map.entry("1",  "Film & Animation"),
        Map.entry("2",  "Autos & Vehicles"),
        Map.entry("10", "Music"),
        Map.entry("15", "Pets & Animals"),
        Map.entry("17", "Sports"),
        Map.entry("18", "Short Movies"),
        Map.entry("19", "Travel & Events"),
        Map.entry("20", "Gaming"),
        Map.entry("21", "Videoblogging"),
        Map.entry("22", "People & Blogs"),
        Map.entry("23", "Comedy"),
        Map.entry("24", "Entertainment"),
        Map.entry("25", "News & Politics"),
        Map.entry("26", "Howto & Style"),
        Map.entry("27", "Education"),
        Map.entry("28", "Science & Technology"),
        Map.entry("29", "Nonprofits & Activism")
    );

    private static final String DEFAULT_BASE_URL = "https://www.googleapis.com/youtube/v3/";
    private static final int MAX_RETRIES = 3;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String apiKey;
    private final String baseUrl;
    private SerializableSleeper sleeper = Thread::sleep;
    private transient OkHttpClient http;

    public YouTubeApiClient(String apiKey) {
        this(apiKey, DEFAULT_BASE_URL);
    }

    YouTubeApiClient(String apiKey, String baseUrl) {
        this.apiKey = apiKey;
        this.baseUrl = baseUrl;
        this.http = buildHttpClient();
    }

    void setSleeperForTest(SerializableSleeper s) {
        this.sleeper = s;
    }

    private OkHttpClient buildHttpClient() {
        return new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build();
    }

    @Override
    public Map<String, ApiVideoData> fetchBatch(List<String> videoIds) throws Exception {
        if (http == null) http = buildHttpClient();

        String ids = String.join(",", videoIds);
        HttpUrl url = HttpUrl.parse(baseUrl + "videos").newBuilder()
            .addQueryParameter("part", "snippet,topicDetails")
            .addQueryParameter("id", ids)
            .build();

        Request request = new Request.Builder()
            .url(url)
            .addHeader("X-Goog-Api-Key", apiKey)
            .get()
            .build();
        Exception lastException = null;

        LOG.debug("Fetching batch videoIds={}", videoIds.size());
        for (int attempt = 0; attempt < MAX_RETRIES; attempt++) {
            if (attempt > 0) {
                long backoffMs = (long) (1000 * Math.pow(2, attempt - 1));
                LOG.warn("YouTube API retry attempt={} backoffMs={}", attempt, backoffMs);
                sleeper.sleep(Math.min(backoffMs, 30_000));
            }
            try (Response response = http.newCall(request).execute()) {
                int code = response.code();
                if (code == 200) {
                    ResponseBody body = response.body();
                    if (body == null) return Map.of();
                    return parseResponse(body.string());
                }
                if (code >= 400 && code < 500) {
                    LOG.error("YouTube API non-retryable error code={} videoIds={}", code, videoIds.size());
                    throw new RuntimeException("Non-retryable HTTP " + code);
                }
                lastException = new RuntimeException("YouTube API returned HTTP " + code);
            } catch (IOException e) {
                lastException = e;
            }
        }
        LOG.error("YouTube API exhausted retries videoIds={}", videoIds.size());
        throw new RuntimeException("YouTube API failed after " + MAX_RETRIES + " attempts", lastException);
    }

    private Map<String, ApiVideoData> parseResponse(String json) throws Exception {
        JsonNode root = MAPPER.readTree(json);
        Map<String, ApiVideoData> result = new HashMap<>();

        for (JsonNode item : root.path("items")) {
            String videoId = item.path("id").asText();
            JsonNode snippet = item.path("snippet");
            JsonNode topicDetails = item.path("topicDetails");

            List<String> topics = new ArrayList<>();
            for (JsonNode t : topicDetails.path("topicCategories")) topics.add(t.asText());

            List<String> tags = new ArrayList<>();
            for (JsonNode t : snippet.path("tags")) tags.add(t.asText());

            String categoryId = snippet.path("categoryId").asText("");
            String categoryName = CATEGORY_NAMES.getOrDefault(categoryId, "");

            result.put(videoId, new ApiVideoData(
                categoryId,
                categoryName,
                snippet.path("channelTitle").asText(""),
                topics,
                tags
            ));
        }
        return result;
    }
}

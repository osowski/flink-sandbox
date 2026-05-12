package io.youtube.history;

import java.util.List;
import java.util.Map;

@FunctionalInterface
public interface YouTubeApiClientPort {
    Map<String, ApiVideoData> fetchBatch(List<String> videoIds) throws Exception;
}

package io.youtube.history;

import java.util.List;

public record ApiVideoData(
    String categoryId,
    String categoryName,
    String channelTitle,
    List<String> topicCategories,
    List<String> tags
) {}

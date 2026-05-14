package io.youtube.history;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;

public final class ClassificationService {

    private static final Logger LOG = LoggerFactory.getLogger(ClassificationService.class);

    private static final List<String> TITLE_KEYWORDS = List.of(
        "Official Music Video", "Official Video", "Lyric Video", "Official Audio"
    );

    private ClassificationService() {}

    public static void classify(VideoMetadata m) {
        classifyWithTitle(m, "");
    }

    // Mutates m: sets isMusic and classificationReason based on API metadata + watch-event title.
    public static void classifyWithTitle(VideoMetadata m, String title) {
        if ("10".equals(m.getCategoryId())) {
            m.setIsMusic(true);
            m.setClassificationReason("category_id");
            log(m);
            return;
        }
        List<String> topics = m.getTopicCategories();
        if (topics != null && topics.stream().anyMatch(t -> t.contains("wikipedia.org/wiki/Music"))) {
            m.setIsMusic(true);
            m.setClassificationReason("topic_music");
            log(m);
            return;
        }
        String channel = m.getChannelTitle() != null ? m.getChannelTitle() : "";
        if (channel.endsWith("VEVO")) {
            m.setIsMusic(true);
            m.setClassificationReason("channel_vevo");
            log(m);
            return;
        }
        if (channel.endsWith(" - Topic")) {
            m.setIsMusic(true);
            m.setClassificationReason("channel_topic");
            log(m);
            return;
        }
        List<String> tags = m.getTags();
        if (tags != null && tags.stream().anyMatch(t -> t.equalsIgnoreCase("music"))) {
            m.setIsMusic(true);
            m.setClassificationReason("tag_music");
            log(m);
            return;
        }
        if (!title.isEmpty() && TITLE_KEYWORDS.stream().anyMatch(title::contains)) {
            m.setIsMusic(true);
            m.setClassificationReason("title_keyword");
            log(m);
            return;
        }
        m.setIsMusic(false);
        m.setClassificationReason("none");
        log(m);
    }

    private static void log(VideoMetadata m) {
        LOG.debug("classify videoId={} isMusic={} reason={}", m.getVideoId(), m.getIsMusic(), m.getClassificationReason());
    }
}

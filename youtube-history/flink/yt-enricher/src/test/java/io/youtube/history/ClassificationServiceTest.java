package io.youtube.history;

import org.junit.jupiter.api.Test;
import java.util.Arrays;
import java.util.Collections;
import static org.junit.jupiter.api.Assertions.*;

class ClassificationServiceTest {

    private VideoMetadata base() {
        VideoMetadata m = new VideoMetadata();
        m.setVideoId("vid");
        m.setCategoryId("1");
        m.setCategoryName("Film & Animation");
        m.setTopicCategories(Collections.emptyList());
        m.setTags(Collections.emptyList());
        m.setChannelTitle("Some Channel");
        m.setIsMusic(false);
        m.setClassificationReason("none");
        m.setFetchedAt(0L);
        return m;
    }

    @Test
    void categoryId10IsMusic() {
        VideoMetadata m = base();
        m.setCategoryId("10");
        ClassificationService.classify(m);
        assertTrue(m.getIsMusic());
        assertEquals("category_id", m.getClassificationReason());
    }

    @Test
    void topicCategoryMusicIsMusic() {
        VideoMetadata m = base();
        m.setTopicCategories(Arrays.asList("https://en.wikipedia.org/wiki/Music"));
        ClassificationService.classify(m);
        assertTrue(m.getIsMusic());
        assertEquals("topic_music", m.getClassificationReason());
    }

    @Test
    void vevoChannelIsMusic() {
        VideoMetadata m = base();
        m.setChannelTitle("TaylorSwiftVEVO");
        ClassificationService.classify(m);
        assertTrue(m.getIsMusic());
        assertEquals("channel_vevo", m.getClassificationReason());
    }

    @Test
    void topicChannelIsMusic() {
        VideoMetadata m = base();
        m.setChannelTitle("Beethoven - Topic");
        ClassificationService.classify(m);
        assertTrue(m.getIsMusic());
        assertEquals("channel_topic", m.getClassificationReason());
    }

    @Test
    void musicTagIsMusic() {
        VideoMetadata m = base();
        m.setTags(Arrays.asList("pop", "music", "dance"));
        ClassificationService.classify(m);
        assertTrue(m.getIsMusic());
        assertEquals("tag_music", m.getClassificationReason());
    }

    @Test
    void officialMusicVideoTitleIsMusic() {
        VideoMetadata m = base();
        ClassificationService.classifyWithTitle(m, "Song Name (Official Music Video)");
        assertTrue(m.getIsMusic());
        assertEquals("title_keyword", m.getClassificationReason());
    }

    @Test
    void nonMusicVideoIsNotMusic() {
        VideoMetadata m = base();
        ClassificationService.classify(m);
        assertFalse(m.getIsMusic());
        assertEquals("none", m.getClassificationReason());
    }

    @Test
    void nullChannelTitleDoesNotThrow() {
        VideoMetadata m = base();
        m.setChannelTitle(null);
        ClassificationService.classify(m);
        assertFalse(m.getIsMusic());
        assertEquals("none", m.getClassificationReason());
    }

    @Test
    void categoryIdTakesPriorityOverTopicCategory() {
        VideoMetadata m = base();
        m.setCategoryId("10");
        m.setTopicCategories(Arrays.asList("https://en.wikipedia.org/wiki/Music"));
        ClassificationService.classify(m);
        assertEquals("category_id", m.getClassificationReason());
    }
}

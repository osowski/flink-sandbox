package io.youtube.history;

import java.io.Serializable;

@FunctionalInterface
public interface SerializableSleeper extends Serializable {
    void sleep(long ms) throws InterruptedException;
}

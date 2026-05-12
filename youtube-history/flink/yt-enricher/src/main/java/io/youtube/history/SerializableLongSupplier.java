package io.youtube.history;

import java.io.Serializable;
import java.util.function.LongSupplier;

public interface SerializableLongSupplier extends LongSupplier, Serializable {}
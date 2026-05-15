# 2. Metadata publish uses a plain Kafka producer, not a Flink sink

Date: 2026-05-15

## Status

Accepted

## Context

After each batch flush, the enricher writes a `VideoMetadata` record to `yt.video.metadata` for every video ID successfully returned by the YouTube Data API. This write serves as cache persistence: future job restarts re-read it via `MetadataBootstrap`, and the in-memory `metadataCache` is updated immediately so subsequent events for the same video ID are served from memory without another API call.

The Flink-native alternative would be a third `DataStream<VideoMetadata>` branch in `EnricherJob`, wired to a `KafkaSink`. That approach conflicts with the ordering invariant required inside `flushBatch()`:

1. **Synchronous ack requirement.** The enriched event for a video must not be emitted downstream until the corresponding metadata record is confirmed durable on the broker. Flink sinks are asynchronous — they buffer records and flush as part of the two-phase commit at checkpoint time. There is no mechanism to block on a specific record's broker ack from within an operator's `onTimer()` or `processElement()` path using a Flink sink.

2. **In-memory cache consistency.** `metadataCache.put(videoId, metadata)` runs immediately after the API result is processed, before the next event arrives. If the topic write were deferred to a Flink sink's checkpoint flush, a crash between that in-memory update and the checkpoint commit would leave the cache claiming a video is known while the topic does not yet contain it. The bootstrap on the next restart would miss the entry, causing an unnecessary API re-fetch and wasted quota.

3. **Structural mismatch.** The metadata write is a side effect of batch processing, not a primary output of the operator. `EnrichmentFunction`'s primary outputs are `EnrichedWatchEvent` via `Collector` and DLQ events via `OutputTag`. Routing metadata through a Flink sink would split a single logical unit of work — one flush, one API call, one set of outputs — across two operator lifecycles, making the control flow harder to reason about and test.

## Decision

A `KafkaProducer<String, VideoMetadata>` is built inside `EnrichmentFunction.open()` and stored as a `transient` field. Within `flushBatch()`, after API results are processed, each metadata record is sent synchronously via `.send(...).get(10, TimeUnit.SECONDS)`. This blocks until the broker acks the write before the corresponding `EnrichedWatchEvent` is collected. The producer is closed in `close()`. It is configured with `acks=all` and `enable.idempotence=true`, consistent with the main pipeline sinks.

## Consequences

- The metadata write is synchronous per video ID within a batch. For a full batch of 50 videos, sends are sequential. This adds latency to the flush path proportional to broker round-trip time. A pipelined approach (collect all `Future`s, then await all) would be faster but adds complexity. Given singleton parallelism and the 5-second batch window, throughput is not the binding constraint.
- The invariant "metadata is durable before enriched event is emitted" is enforced in code, not by checkpoint protocol. This is simpler to verify and reason about than a two-phase commit across two sinks.
- If the broker is slow or unavailable, the 10-second timeout surfaces as a job failure, triggering a Flink restart from the last checkpoint. No silent data loss occurs.
- The producer is `transient` and is rebuilt in `open()` on task restart, consistent with other non-serializable resources in the operator.

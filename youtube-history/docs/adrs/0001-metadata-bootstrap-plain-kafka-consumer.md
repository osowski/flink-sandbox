# 1. Metadata bootstrap uses a plain Kafka consumer, not a Flink source

Date: 2026-05-15

## Status

Accepted

## Context

The enricher job needs a pre-populated in-memory map of `videoId → VideoMetadata` before it can serve cache hits on the fast path. This map is sourced from `yt.video.metadata`, a compacted Kafka topic that acts as the persistent metadata store.

The map must be fully populated before the Flink DAG starts accepting events from `yt.raw.watch.events`. The Flink-native alternative would be a `BroadcastProcessFunction` where `yt.video.metadata` is a broadcast stream and raw events are the main stream. That approach has several problems in this context:

- Flink has no built-in primitive for "read a compacted topic to its current end, then switch to steady-state." Implementing that signal requires custom completion logic in broadcast state.
- A race window exists: early raw events arrive concurrently with broadcast records being replayed, forcing a policy decision (buffer, drop, or accept misses) where the bootstrap's synchronous block has none.
- Broadcast state is checkpointed by Flink, but `yt.video.metadata` is already the authoritative durable source. Checkpointing a copy is redundant — on restart the topic would be re-read anyway, making Flink's copy a potentially stale shadow.
- A Flink Kafka source registers a consumer group and manages offsets on the broker. For a one-shot, read-to-end operation this is purely overhead, and it leaves orphaned consumer group state behind across restarts.

## Decision

`MetadataBootstrap.load()` is a plain `KafkaConsumer` that runs synchronously in `main()` before `StreamExecutionEnvironment.execute()` is called. It uses `assign()` + `seekToBeginning()` rather than `subscribe()`, bypassing the consumer group protocol entirely. It polls until the consumer's position in every partition reaches the end offset captured before polling began. Tombstone records (null value) are handled by removing the key from the cache rather than inserting a null. The resulting map is passed directly to the `EnrichmentFunction` constructor.

## Consequences

- The Flink job does not start until the bootstrap completes. On a large history replay with tens of thousands of cached entries, this adds startup latency proportional to topic size, but it is a one-time cost.
- No consumer group is registered on the broker; no orphaned group state accumulates across restarts.
- The cache cannot be read in `open()` (no active key context at that point), so the pre-loaded map is the only mechanism for warm-cache enrichment on restarts. This is acceptable because the timer restored via `timerTimestampState` ensures any events buffered before a crash are eventually flushed and re-enriched.
- The bootstrap is straightforward to reason about and test in isolation without Flink infrastructure.

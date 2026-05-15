# 3. Enricher source uses no watermark strategy

Date: 2026-05-15

## Status

Accepted

## Context

Flink's `WatermarkStrategy` controls how the event-time clock advances through a stream. Watermarks are required for event-time windows, event-time joins, and event-time timers. The alternative — `WatermarkStrategy.noWatermarks()` — is the affirmative declaration that an operator does not participate in event-time semantics.

The enricher's only stateful operation is accumulating raw watch events into a buffer and flushing that buffer in batches to the YouTube Data API. No event-time windowing, no event-time joins, and no event-time timers are used anywhere in `EnrichmentFunction`.

## Decision

`WatermarkStrategy.noWatermarks()` is passed to `env.fromSource()` at `EnricherJob.java:102`. No watermark strategy is assigned to the enricher source.

The reasons are:

1. **The batch window is processing-time.** `EnrichmentFunction` registers timers via `ctx.timerService().registerProcessingTimeTimer()`. Processing-time timers fire on wall-clock time and are independent of watermarks. Emitting watermarks into the stream would have no effect on when `onTimer()` fires.

2. **The enricher does not reason about event ordering.** Events are buffered and flushed as a group. Whether a `RawWatchEvent` with an earlier `watchedAt` arrives after one with a later `watchedAt` is immaterial to the enrichment logic.

3. **`watchedAt` is pass-through payload, not a processing signal.** The timestamp is copied from `RawWatchEvent` to `EnrichedWatchEvent` without being used for aggregation, sorting, or windowing within the enricher.

4. **There is no second stream to coordinate against.** Event-time joins require watermarks to synchronize event-time progress across two inputs. The enricher is single-input.

5. **Watermark generation has a cost with no benefit here.** Strategies such as `forBoundedOutOfOrderness()` track the maximum event timestamp across all messages and periodically inject punctuation into the stream. This bookkeeping consumes resources for no downstream effect in this job.

6. **`noWatermarks()` is a correctness statement.** Using a real watermark strategy would imply that something in the pipeline is event-time sensitive, which would be misleading to future readers and create false expectations about the temporal semantics in play.

## Consequences

- No event-time semantics are available to `EnrichmentFunction` or to any operator chained after it in this pipeline. This is intentional and correct for the enricher's role.
- If a downstream job consuming `yt.enriched.watch.events` requires event-time windowed aggregations, it must assign its own watermark strategy when reading from that topic. The enricher does not pre-assign watermarks on behalf of unknown downstream consumers.
- If a future version of `EnrichmentFunction` introduces event-time operations (e.g., a session window over `watchedAt` to detect binge-watching patterns), this decision must be revisited and an appropriate watermark strategy selected at that time.

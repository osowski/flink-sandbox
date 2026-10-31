# device-trust-mesh

Source for the **Agentic Device Trust Mesh** demo: a device simulator, an RTCE-style context server (`context-mcp`) and A2A agents that run on Ray Serve (KubeRay) against Confluent Platform and CP Flink.

The GitOps side (topics, schemas, Flink statements, KubeRay, the `RayService` manifest) lives in [osowski/confluent-platform-gitops](https://github.com/osowski/confluent-platform-gitops), under `workloads/device-trust-mesh/`. Its README is the use-case doc: architecture, the A2A-over-Kafka binding, the topic catalogue and runtime configuration. Work is tracked in [Epic #441](https://github.com/osowski/confluent-platform-gitops/issues/441).

## Layout

| Path | Contents |
|---|---|
| `dtm/common/` | `envelope` (mesh envelope), `ids` (deterministic ids), `a2a` (A2A v1.0 record builders and validation), `bus` (`AgentBus`, the agents' only Kafka access: A2A Kafka binding, consume allow-list, replay knobs), `kafka` (client settings), `metrics` and `schemas` (vendored schema loaders) |
| `dtm/simulator/` | Device simulator: device pool bounded by `DEVICE_CAP`, risk scenarios, closed loop on `device.policy-commands` |
| `dtm/mesh/` | Ray Serve application: `runtime` (`AgentRuntime`: poll loop, hot reload, Agent Card lifecycle), `llm` (`LLMProvider`: Ollama, recorded replay, llm-interactions records), `context-mcp` (materializer + FastMCP tools); the risk, enrolment, step-up, compliance and revocation agents are added by later stories |
| `dtm/schemas/` | Vendored copy of the GitOps repo's generated Avro and A2A JSON Schemas (`SOURCE` records the commit), shipped as package data; `scripts/sync_schemas.py` refreshes it |
| `tests/` | Unit tests |

Agents read business data (device and customer context, risk signals) **only** through `context-mcp`. They use Kafka directly only for A2A agent-to-agent traffic.

## Runtime configuration reload

`AgentRuntime` re-reads `agents.yaml` and the prompt files before every message. An agent's `enabled`, `llm` and `promptRef` changes, and the `bus.policyCommands` mode, take effect on the next message; a disabled agent (or one removed from `agents.yaml`) withdraws its Agent Card and pauses its consumer. Changes to `bus.topicPrefix`, `bus.startPosition`, `bus.startValue`, `bus.allowedConsume`, `bus.mode` and `bus.replayRunId` need a pod restart: the runtime refuses such a config, logs an error and keeps its previous settings.

`agents.yaml` accepts only the top-level keys `llm`, `agents`, `bus` and `context`, and in an agent block only `enabled`, `replicas`, `promptRef` and `llm`; an unknown key (in those or in `bus`) makes the config invalid. Every agent's settings carry the `context` block: `mcpUrl` (required) is the `context-mcp` endpoint the agent calls, and `waitTimeoutMs` (optional, a positive integer) is validated but not read by `context-mcp`, whose server-side wait comes from `DTM_CONTEXT_WAIT_TIMEOUT_MS`.

An agent consumes its own `agent.tasks.<name>` topic and its card advertises that topic. An observer of `agent.task-events`, such as Compliance, passes `topics=["agent.task-events"]` and `request_topic=None` to `AgentRuntime`; its card lists no interfaces.

## Images

`quay.io/osowski/device-trust-mesh-simulator` and `quay.io/osowski/device-trust-mesh-ray` are built by the `device-trust-mesh-build-images.yml` workflow ([#30](https://github.com/osowski/flink-sandbox/issues/30)).

## Development

```bash
cd device-trust-mesh
uv venv -p 3.12 .venv && uv pip install -p .venv -e .
.venv/bin/python -m unittest discover -s tests
```

Refresh the vendored schemas in `dtm/schemas/` from a GitOps checkout, or verify they are current:

```bash
python3 scripts/sync_schemas.py <gitops-checkout>
python3 scripts/sync_schemas.py <gitops-checkout> --check
```

# device-trust-mesh

Source for the **Agentic Device Trust Mesh** demo: a device simulator, an RTCE-style context server (`context-mcp`) and A2A agents that run on Ray Serve (KubeRay) against Confluent Platform and CP Flink.

The GitOps side (topics, schemas, Flink statements, KubeRay, the `RayService` manifest) lives in [osowski/confluent-platform-gitops](https://github.com/osowski/confluent-platform-gitops), under `workloads/device-trust-mesh/`. Its README is the use-case doc: architecture, the A2A-over-Kafka binding, the topic catalogue and runtime configuration. Work is tracked in [Epic #441](https://github.com/osowski/confluent-platform-gitops/issues/441).

## Layout

| Path | Contents |
|---|---|
| `dtm/common/` | `envelope` (mesh envelope), `ids` (deterministic ids), `a2a` (A2A v1.0 record builders and validation), `bus` (`AgentBus`, the agents' only Kafka access: A2A Kafka binding, consume allow-list, replay knobs), `kafka` (client settings), `metrics` and `schemas` (vendored schema loaders) |
| `dtm/simulator/` | Device simulator: device pool bounded by `DEVICE_CAP`, risk scenarios, closed loop on `device.policy-commands` |
| `dtm/mesh/` | Ray Serve application: `runtime` (`AgentRuntime`: poll loop, hot reload, Agent Card lifecycle), `llm` (`LLMProvider`: Ollama, recorded replay, llm-interactions records), `context/` (context table, materializer, MCP tools and REST mirror), `context_mcp` (the `dtm.mesh.context_mcp:app` Serve entry point) and the risk, enrolment, step-up, compliance and revocation agents |
| `schemas/` | Vendored copy of the GitOps repo's generated Avro and A2A JSON Schemas (`SOURCE` records the commit); `scripts/sync_schemas.py` refreshes it |
| `tests/` | Unit tests |

Agents read business data (device and customer context, risk signals) **only** through `context-mcp`. They use Kafka directly only for A2A agent-to-agent traffic.

## Runtime configuration reload

`AgentRuntime` re-reads `agents.yaml` and the prompt files before every message. An agent's `enabled`, `llm` and `promptRef` changes, and the `bus.policyCommands` mode, take effect on the next message; a disabled agent (or one removed from `agents.yaml`) withdraws its Agent Card and pauses its consumer. Changes to `bus.topicPrefix`, `bus.startPosition`, `bus.startValue`, `bus.allowedConsume`, `bus.mode` and `bus.replayRunId` need a pod restart: the runtime refuses such a config, logs an error and keeps its previous settings.

## Images

`quay.io/osowski/device-trust-mesh-simulator` and `quay.io/osowski/device-trust-mesh-ray` are built by the `device-trust-mesh-build-images.yml` workflow ([#30](https://github.com/osowski/flink-sandbox/issues/30)).

## Development

```bash
cd device-trust-mesh
uv venv -p 3.12 .venv && uv pip install -p .venv -e .
.venv/bin/python -m unittest discover -s tests
```

`context-mcp` serves four MCP tools over streamable HTTP at `/context-mcp/mcp`:

- `get_device_context(device_id, min_seq=None)`: with `min_seq` it waits up to `DTM_CONTEXT_WAIT_TIMEOUT_MS` (default 2000) for that context sequence, then answers `OK`, `STALE` or `NOT_FOUND`.
- `get_customer_context(customer_id)`
- `list_risk_signals(device_id, limit=10)`
- `list_devices(customer_id=None, trust_status=None)`

The same data is mirrored over REST, next to the MCP endpoint: `GET /v1/devices/{device_id}?min_seq=`, `GET /v1/devices?customer_id=&trust_status=`, `GET /v1/customers/{customer_id}`, `GET /v1/devices/{device_id}/signals?limit=`, `GET /healthz` (503 until the materializer has caught up) and `GET /metrics`. Agents call the tools through `dtm.common.context_client.ContextClient`, which retries a `STALE` lookup. The MCP endpoint accepts loopback and `device-trust-mesh-serve-svc` Host headers; `DTM_CONTEXT_ALLOWED_HOSTS` adds more (comma-separated host names).

Refresh the vendored schemas from a GitOps checkout, or verify they are current:

```bash
python3 scripts/sync_schemas.py <gitops-checkout>
python3 scripts/sync_schemas.py <gitops-checkout> --check
```

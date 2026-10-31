# device-trust-mesh

Source for the **Agentic Device Trust Mesh** demo: a device simulator, an RTCE-style context server (`context-mcp`) and A2A agents that run on Ray Serve (KubeRay) against Confluent Platform and CP Flink.

The GitOps side (topics, schemas, Flink statements, KubeRay, the `RayService` manifest) lives in [osowski/confluent-platform-gitops](https://github.com/osowski/confluent-platform-gitops), under `workloads/device-trust-mesh/`. Its README is the use-case doc: architecture, the A2A-over-Kafka binding, the topic catalogue and runtime configuration. Work is tracked in [Epic #441](https://github.com/osowski/confluent-platform-gitops/issues/441).

## Layout

| Path | Contents |
|---|---|
| `dtm/common/` | `envelope` (mesh envelope), `ids` (deterministic ids), `a2a` (A2A v1.0 record builders and validation), `bus` (`AgentBus`, the agents' only Kafka access: A2A Kafka binding, consume allow-list, replay knobs), `kafka` (client settings), `metrics` and `schemas` (vendored schema loaders) |
| `dtm/simulator/` | Device simulator: device pool bounded by `DEVICE_CAP`, risk scenarios, closed loop on `device.policy-commands` |
| `dtm/mesh/` | Ray Serve application: `runtime` (`AgentRuntime`: poll loop, hot reload, Agent Card lifecycle), `llm` (`LLMProvider`: Ollama, recorded replay, llm-interactions records), `context/` (context table, materializer, MCP tools and REST mirror), `context_mcp` (the `dtm.mesh.context_mcp:app` Serve entry point); the risk, enrolment, step-up, compliance and revocation agents are added by later stories |
| `dtm/schemas/` | Vendored copy of the GitOps repo's generated Avro and A2A JSON Schemas (`SOURCE` records the commit), shipped as package data; `scripts/sync_schemas.py` refreshes it |
| `tests/` | Unit tests |

Agents read business data (device and customer context, risk signals) **only** through `context-mcp`. They use Kafka directly only for A2A agent-to-agent traffic.

## Runtime configuration reload

`AgentRuntime` re-reads `agents.yaml` and the prompt files before every message. An agent's `enabled`, `llm` and `promptRef` changes, and the `bus.policyCommands` mode, take effect on the next message; a disabled agent (or one removed from `agents.yaml`) withdraws its Agent Card and pauses its consumer. Changes to `bus.topicPrefix`, `bus.startPosition`, `bus.startValue`, `bus.allowedConsume`, `bus.mode` and `bus.replayRunId` need a pod restart: the runtime refuses such a config, logs an error and keeps its previous settings.

`agents.yaml` accepts only the top-level keys `llm`, `agents`, `bus` and `context`, and in an agent block only `enabled`, `replicas`, `promptRef` and `llm`; an unknown key (in those or in `bus`) makes the config invalid. Every agent's settings carry the `context` block: `mcpUrl` (required) is the `context-mcp` endpoint the agent calls, and `waitTimeoutMs` (optional, a positive integer) is validated but not read by `context-mcp`, whose server-side wait comes from `DTM_CONTEXT_WAIT_TIMEOUT_MS`.

An agent consumes its own `agent.tasks.<name>` topic and its card advertises that topic. An observer of `agent.task-events`, such as Compliance, passes `topics=["agent.task-events"]` and `request_topic=None` to `AgentRuntime`; its card lists no interfaces.

## Simulator

`python -m dtm.simulator` runs the device simulator. It produces `ENROL_REQUESTED` and `UNENROL_REQUESTED` events to `device.events`, keyed by `device_id`, and closes the loop: it consumes `device.policy-commands` and answers every `CHALLENGE` command (once, echoing the enrolment's `correlation_id` and `enrolment_task_id`) with `CHALLENGE_PASSED` or `CHALLENGE_FAILED` after 2 s. A `CHALLENGE` command without an `enrolment_task_id` is not answered, because the router drops such an answer. Presenter commands arrive on `simulator.control`.

| Variable | Default | Meaning |
|---|---|---|
| `DEVICE_CAP` | required | Size of the fixed device pool (1 to 500): `dev-0001`..., three devices per customer |
| `SIM_RATE_PER_SEC` | `0.2` | Paced enrolments per second (about one every 5 s) |
| `SIM_SEED` | random | Seed for the scenario mix, device choice and challenge outcomes |
| `DTM_VERSION` | `dev` | Written to each event's `agent_version` |
| `KAFKA_BOOTSTRAP_SERVERS`, `SCHEMA_REGISTRY_URL` | in-cluster addresses | Kafka and Schema Registry endpoints |

Each paced token goes to the injected scenarios first, then to a draw from the mix: 70% `CLEAN` and 7.5% each of `COMPROMISED_OS`, `OUTDATED_OS`, `NEW_DEVICE_ESTABLISHED_ACCOUNT` and `IMPOSSIBLE_TRAVEL`. A device is chosen among those that are new, unenrolled or blocked (or pending for longer than 120 s); when none is left, the simulator unenrols a random trusted device.

| Scenario | What it emits |
|---|---|
| `CLEAN` | Enrolment from a clean device in its home country |
| `COMPROMISED_OS` | Enrolment from a rooted device |
| `OUTDATED_OS` | Enrolment from a device with an OS below the Flink threshold |
| `NEW_DEVICE_ESTABLISHED_ACCOUNT` | Enrolment of a new device on an account older than 90 days that already has a trusted device |
| `IMPOSSIBLE_TRAVEL` | Enrolment from `AU` (`GB` for an `AU` home) for a customer with an event in the last 10 minutes |
| `CHALLENGE` | Enrolment with an outdated OS, to drive the step-up challenge |
| `UNENROL` | Unenrolment of a trusted device |

A scenario with no feasible device falls back to `CLEAN`.

`dtm-ctl` sends the control commands (`python -m dtm.simulator.ctl ...`; it needs the same Kafka and Schema Registry variables). Records are keyed by the scenario name, or `control` when there is none.

```bash
python -m dtm.simulator.ctl start
python -m dtm.simulator.ctl stop
python -m dtm.simulator.ctl rate 1.5                  # 0 pauses emission
python -m dtm.simulator.ctl inject IMPOSSIBLE_TRAVEL  # queue one ahead of the mix
python -m dtm.simulator.ctl inject OUTDATED_OS --count 3
```

## Images

`quay.io/osowski/device-trust-mesh-simulator` and `quay.io/osowski/device-trust-mesh-ray` are built by the `device-trust-mesh-build-images.yml` workflow ([#30](https://github.com/osowski/flink-sandbox/issues/30)).

## Development

```bash
cd device-trust-mesh
uv venv -p 3.12 .venv && uv pip install -p .venv -e .
.venv/bin/python -m unittest discover -s tests
```

`context-mcp` serves four MCP tools over streamable HTTP at `/context-mcp/mcp`:

- `get_device_context(device_id, min_seq=None)`: with `min_seq` it waits up to `DTM_CONTEXT_WAIT_TIMEOUT_MS` (default 2000) for that context sequence, then answers `OK`, `STALE` or `NOT_FOUND`. The result's `as_of` carries `materializedAt`, `sourceOffsets` and, only when the device has a context sequence, `contextSeq` (never null). Agents copy `as_of` into an artifact's `asOf` only from an `OK` result that has `contextSeq`.
- `get_customer_context(customer_id)`
- `list_risk_signals(device_id, limit=10)`
- `list_devices(customer_id=None, trust_status=None)`

The same data is mirrored over REST, next to the MCP endpoint: `GET /v1/devices/{device_id}?min_seq=`, `GET /v1/devices?customer_id=&trust_status=`, `GET /v1/customers/{customer_id}`, `GET /v1/devices/{device_id}/signals?limit=`, `GET /healthz` (503 until the materializer has caught up) and `GET /metrics`. Agents call the tools through `dtm.common.context_client.ContextClient`, which retries a `STALE` lookup. The MCP endpoint accepts loopback and `device-trust-mesh-serve-svc` Host headers; `DTM_CONTEXT_ALLOWED_HOSTS` adds more (comma-separated host names).

Refresh the vendored schemas in `dtm/schemas/` from a GitOps checkout, or verify they are current:

```bash
python3 scripts/sync_schemas.py <gitops-checkout>
python3 scripts/sync_schemas.py <gitops-checkout> --check
```

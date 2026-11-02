# device-trust-mesh

Source for the **Agentic Device Trust Mesh** demo: a device simulator, an RTCE-style context server (`context-mcp`) and A2A agents that run on Ray Serve (KubeRay) against Confluent Platform and CP Flink.

The GitOps side (topics, schemas, Flink statements, KubeRay, the `RayService` manifest) lives in [osowski/confluent-platform-gitops](https://github.com/osowski/confluent-platform-gitops), under `workloads/device-trust-mesh/`. Its README is the use-case doc: architecture, the A2A-over-Kafka binding, the topic catalogue and runtime configuration. Work is tracked in [Epic #441](https://github.com/osowski/confluent-platform-gitops/issues/441).

## Layout

| Path | Contents |
|---|---|
| `dtm/common/` | `envelope` (mesh envelope), `ids` (deterministic ids), `a2a` (A2A v1.0 record builders and validation), `bus` (`AgentBus`, the agents' only Kafka access: A2A Kafka binding, consume allow-list, replay knobs), `kafka` (client settings), `metrics` and `schemas` (vendored schema loaders) |
| `dtm/simulator/` | Device simulator: device pool bounded by `DEVICE_CAP`, risk scenarios, closed loop on `device.policy-commands` |
| `dtm/mesh/` | Ray Serve application: `runtime` (`AgentRuntime`: poll loop, hot reload, Agent Card lifecycle), `llm` (`LLMProvider`: Ollama, recorded replay, llm-interactions records), `context/` (context table, materializer, MCP tools and REST mirror), `context_mcp` (the `dtm.mesh.context_mcp:app` Serve entry point), `agents/` (the five agents, their shared `kit` and the `AgentHost` Serve wrapper) and one Serve entry point per agent |
| `dtm/prompts/` | Packaged default prompts: `risk.v1`, `enrolment.v1`, `revocation.v1` |
| `dtm/schemas/` | Vendored copy of the GitOps repo's generated Avro and A2A JSON Schemas (`SOURCE` records the commit), shipped as package data; `scripts/sync_schemas.py` refreshes it |
| `tests/` | Unit tests |

Agents read business data (device and customer context, risk signals) **only** through `context-mcp`. They use Kafka directly only for A2A agent-to-agent traffic.

## Agents

Each agent is a Ray Serve deployment that consumes one topic through `AgentBus`, reads business data only from `context-mcp`, and keys every output by `device_id`. Every output carries the mesh envelope: `causationId` is the inbound `messageId`, and `messageId` is derived from the inbound record, so a redelivered record reproduces the same ids.

| Agent | Consumes | Produces | LLM | Serve import path |
|---|---|---|---|---|
| Risk | `agent.tasks.risk` | `agent.task-events`, `agent.tasks.enrolment`, `agent.llm-interactions` | yes | `dtm.mesh.risk:app` |
| Enrolment | `agent.tasks.enrolment` | `agent.task-events`, `device.policy-commands`, `agent.tasks.step-up`, `agent.llm-interactions` | yes | `dtm.mesh.enrolment:app` |
| Step-up | `agent.tasks.step-up` | `agent.task-events`, `device.policy-commands` | no | `dtm.mesh.step_up:app` |
| Compliance | `agent.task-events` | `audit.log`, `agent.task-events` | no | `dtm.mesh.compliance:app` |
| Revocation | `agent.tasks.revocation` | `agent.task-events` | no | `dtm.mesh.revocation:app` |

A task is a `task` (`SUBMITTED`), a `WORKING` status, an artifact and a final status: `COMPLETED`, `REJECTED`, `FAILED` or `AUTH_REQUIRED`. A task that raises ends `FAILED` with `ExceptionType: message` as its status text, and the record is committed.

- **Risk failure:** if the model call fails or its output is invalid, or context is not `OK` with a `contextSeq`, the risk task ends `FAILED` with no hand-off. The `agent.llm-interactions` record is still written.
- **Enrolment failure:** if the model call fails or its output is invalid, the decision becomes `CHALLENGE` with guardrail `LLM_OUTPUT_INVALID`.
- **Challenge decision:** a `CHALLENGE` emits an `enrolment-decision` artifact (`CHALLENGED`) and hands off to step-up before the task ends `AUTH_REQUIRED`. `RESOLVE_CHALLENGE`, carrying the enrolment `taskId`, resumes that task, emits the final `enrolment-decision` and a policy command (`CHALLENGE_PASSED` allows, `CHALLENGE_FAILED` blocks), and ends it `COMPLETED`.
- **Compliance:** it audits every `artifactUpdate` except `audit-record`. Per audited artifact it owns one task, which emits an `audit.log` record and an `audit-record` artifact and ends `COMPLETED`. It skips every event from `agent:compliance`.

### Guardrails

The Enrolment agent decides without the model for a rooted device or a `COMPROMISED_OS` signal (`DENY`, `COMPROMISED_OS`) and for an already trusted one (`GRANT`, `ALREADY_TRUSTED`). A model `GRANT` becomes `CHALLENGE` when:

| Guardrail | Fires when |
|---|---|
| `NO_RISK_ASSESSMENT` | the request carries no risk assessment (`GRANT` or `GRANT_READ_ONLY`) |
| `RISK_THRESHOLD` | the risk score is at least `challengeAtRiskScore` |
| `UNRESOLVED_SIGNALS` | the risk assessment cites any risk signal |

`challengeAtRiskScore` is an integer from 0 to 100 (default 60), set per agent in `agents.yaml`:

```yaml
agents:
  enrolment:
    guardrails:
      challengeAtRiskScore: 70
```

The Step-up agent picks the method without a model: `MFA_PUSH` when the customer has another `TRUSTED` device, else `OTP` when a cited signal is `OUTDATED_OS` or `COMPROMISED_OS` or the device is rooted, else `BIOMETRIC`.

### Prompts

The packaged prompts in `dtm/prompts/` are the defaults. A `promptRef` resolves to the matching key in the mounted `device-trust-mesh-prompts` ConfigMap first and falls back to the packaged prompt; a ref found in neither makes the config invalid. A ConfigMap key overrides the packaged prompt live, with no pod restart.

## Runtime configuration reload

`AgentRuntime` re-reads `agents.yaml` and the prompt files before every message. An agent's `enabled`, `llm` and `promptRef` changes, and the `bus.policyCommands` mode, take effect on the next message; a disabled agent (or one removed from `agents.yaml`) withdraws its Agent Card and pauses its consumer. Changes to `bus.topicPrefix`, `bus.startPosition`, `bus.startValue`, `bus.allowedConsume`, `bus.mode` and `bus.replayRunId` need a pod restart: the runtime refuses such a config, logs an error and keeps its previous settings.

`agents.yaml` accepts only the top-level keys `llm`, `agents`, `bus` and `context`, and in an agent block only `enabled`, `replicas`, `promptRef`, `llm` and `guardrails`; an unknown key (in those or in `bus`) makes the config invalid. Every agent's settings carry the `context` block: `mcpUrl` (required) is the `context-mcp` endpoint the agent calls, and `waitTimeoutMs` (optional, a positive integer) is validated but not read by `context-mcp`, whose server-side wait comes from `DTM_CONTEXT_WAIT_TIMEOUT_MS`.

An agent consumes its own `agent.tasks.<name>` topic and its card advertises that topic. An observer of `agent.task-events`, such as Compliance, passes `topics=["agent.task-events"]` and `request_topic=None` to `AgentRuntime`; its card lists no interfaces.

## Simulator

`python -m dtm.simulator` runs the device simulator. It produces `ENROL_REQUESTED` and `UNENROL_REQUESTED` events to `device.events`, keyed by `device_id`, and closes the loop: it consumes `device.policy-commands` and answers every `CHALLENGE` command (once, echoing the enrolment's `correlation_id` and `enrolment_task_id`) with `CHALLENGE_PASSED` or `CHALLENGE_FAILED` after 2 s. A `CHALLENGE` command without an `enrolment_task_id` is not answered, because the router drops such an answer. Presenter commands arrive on `simulator.control`.

| Variable | Default | Meaning |
|---|---|---|
| `DEVICE_CAP` | required | Size of the fixed device pool (1 to 500): `dev-0001`..., three devices per customer |
| `SIM_RATE_PER_SEC` | `0.2` | Paced enrolments per second (about one every 5 s), a finite number from 0 to 100 |
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
python -m dtm.simulator.ctl rate 1.5                  # 0 to 100; 0 pauses emission
python -m dtm.simulator.ctl inject IMPOSSIBLE_TRAVEL  # queue one ahead of the mix
python -m dtm.simulator.ctl inject OUTDATED_OS --count 3
```

## Images

| Image | Dockerfile | Base | Runs |
|---|---|---|---|
| `quay.io/osowski/device-trust-mesh-simulator` | `Dockerfile.simulator` | `python:3.12.16-slim` | `python -m dtm.simulator` (non-root) |
| `quay.io/osowski/device-trust-mesh-ray` | `Dockerfile.ray` | `rayproject/ray:2.59.0-py312-cpu` | The RayService head and workers; Serve imports `dtm.mesh.*:app` |

The [`device-trust-mesh-build-images.yml`](../.github/workflows/device-trust-mesh-build-images.yml) workflow builds both for `linux/amd64` and `linux/arm64`. Pull requests build without pushing; pushes to `main`, `device-trust-mesh-v*` tags and manual runs push to Quay. Both images share one version: tags look like `v1.0.0-202611021200` (`<repo-version>-<UTC build timestamp>`), plus a floating `:latest`. `<repo-version>` comes from the most recent `device-trust-mesh-v*` tag (`v0.0.0` until one exists). Cut a release by pushing a tag, then take the exact `<version_tag>` from the run it triggered:

```bash
git tag device-trust-mesh-v1.0.0
git push-external origin device-trust-mesh-v1.0.0
```

The dependency pins live in `pyproject.toml` and the base images in the two Dockerfiles. The simulator installs the package without the `serve` extra, so it carries no Ray. When the Ray base image changes, change the `ray[serve]` pin and the RayService `rayVersion` with it.

`./build.sh [tag]` builds both images locally for the host platform (default tag `dev`).

## Development

```bash
cd device-trust-mesh
uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[serve]'
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

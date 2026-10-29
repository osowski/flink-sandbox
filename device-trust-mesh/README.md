# device-trust-mesh

Source for the **Agentic Device Trust Mesh** demo: a device simulator, an RTCE-style context server (`context-mcp`) and A2A agents that run on Ray Serve (KubeRay) against Confluent Platform and CP Flink.

The GitOps side (topics, schemas, Flink statements, KubeRay, the `RayService` manifest) lives in [osowski/confluent-platform-gitops](https://github.com/osowski/confluent-platform-gitops), under `workloads/device-trust-mesh/`. Its README is the use-case doc: architecture, the A2A-over-Kafka binding, the topic catalogue and runtime configuration. Work is tracked in [Epic #441](https://github.com/osowski/confluent-platform-gitops/issues/441).

## Layout

| Path | Contents |
|---|---|
| `dtm/common/` | Mesh envelope, `AgentBus` (agents' only Kafka access: A2A v1.0 Kafka binding, consume allow-list, deterministic ids), serde, `ConfigSource` |
| `dtm/simulator/` | Device simulator: device pool bounded by `DEVICE_CAP`, risk scenarios, closed loop on `device.policy-commands` |
| `dtm/mesh/` | Ray Serve application: `context-mcp` (materializer + FastMCP tools) and the risk, enrolment, step-up, compliance and revocation agents |
| `schemas/` | Canonical Avro and A2A-derived JSON Schemas, copied into the GitOps repo's `schemas.yaml` |
| `tests/` | Unit tests |

Agents read business data (device and customer context, risk signals) **only** through `context-mcp`. They use Kafka directly only for A2A agent-to-agent traffic.

## Images

`quay.io/osowski/device-trust-mesh-simulator` and `quay.io/osowski/device-trust-mesh-ray` are built by the `device-trust-mesh-build-images.yml` workflow ([#30](https://github.com/osowski/flink-sandbox/issues/30)).

## Tests

```bash
cd device-trust-mesh
python3 -m unittest discover -s tests
```

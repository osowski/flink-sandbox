#!/usr/bin/env python3
"""Time the risk and enrolment prompts on Ollama models and report latency and schema-valid rate.

Usage: python scripts/benchmark_models.py [--endpoint URL] [--models M ...] [--runs N] [--json PATH]

Each model answers the agents' own packaged prompts, schemas and options (the base config's
``temperature 0, seed 42, think false, num_ctx 4096``) for one device per scenario: clean plus the four
injected risk scenarios. Facts come from a context table seeded the way the materializer seeds it, so the
models see what the agents send. A COMPROMISED_OS device is decided by a guardrail before the enrolment
model, so it is only scored by risk. The first call per model loads it and is not timed. Latency is the
provider's wall clock per call; p50/p95 are nearest-rank.
"""
import argparse
import importlib.resources
import json
import platform
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass

from dtm.mesh.agents.enrolment import DECISION_SCHEMA, EnrolmentAgent, decision_facts
from dtm.mesh.agents.reasoning import signal_facts, system_and_user
from dtm.mesh.agents.risk import RISK_SCHEMA, risk_facts
from dtm.mesh.context.table import CUSTOMER_CONTEXT, DEVICE_CONTEXT, RISK_SIGNALS, ContextTable
from dtm.mesh.llm import OK, LLMProvider, LLMRequest, LLMResponse, OllamaProvider

DEFAULT_MODELS = ["qwen3:4b", "qwen3:8b"]
DEFAULT_OPTIONS = {"temperature": 0, "seed": 42, "think": False, "num_ctx": 4096}
DEFAULT_TIMEOUT_MS = 30000
_EVENT_TIME_MS = 1_793_448_000_000  # a fixed timestamp: the facts are identical in every run

_DEVICE = {"customer_id": "cust-001", "context_seq": 3, "trust_status": "PENDING", "platform": "IOS",
           "os_version": "18.1", "rooted": False, "last_country": "US", "last_lat": 37.77, "last_lon": -122.42,
           "last_event_type": "ENROL_REQUESTED", "last_event_time": _EVENT_TIME_MS}
_CUSTOMER = {"device_count": 1, "trusted_device_count": 0, "known_countries": "US", "recent_anomaly_count": 0,
             "account_age_days": 30, "last_country": "US", "last_event_time": _EVENT_TIME_MS}


@dataclass(frozen=True)
class Scenario:
    device: dict
    customer: dict
    signal: tuple[str, str] | None  # (severity, detail) of the scenario's one signal
    risk_score: int  # the risk assessment the enrolment model is given


SCENARIOS = {
    "CLEAN": Scenario({}, {}, None, 8),
    "COMPROMISED_OS": Scenario({"platform": "ANDROID", "os_version": "14", "rooted": True}, {},
                               ("HIGH", "ANDROID 14 rooted"), 90),
    "OUTDATED_OS": Scenario({"os_version": "16.2"}, {}, ("MEDIUM", "IOS 16.2 below minimum 17"), 40),
    "NEW_DEVICE_ESTABLISHED_ACCOUNT": Scenario(
        {}, {"device_count": 3, "trusted_device_count": 2, "account_age_days": 400},
        ("MEDIUM", "account 400 days old, 2 other device(s) allowed"), 45),
    "IMPOSSIBLE_TRAVEL": Scenario({"last_country": "BR", "last_lat": -23.55, "last_lon": -46.63},
                                  {"known_countries": "US,BR", "last_country": "BR"}, ("HIGH", "US->BR 10386 km in 0.5 h"), 80),
}


@dataclass(frozen=True)
class Sample:
    model: str
    prompt: str
    scenario: str
    response: LLMResponse


def prompt(ref: str) -> str:
    return (importlib.resources.files("dtm") / "prompts" / ref).read_text(encoding="utf-8")


def _table(name: str) -> ContextTable:
    s = SCENARIOS[name]
    table = ContextTable(10)
    table.apply(DEVICE_CONTEXT, 0, 0, {"device_id": "dev-0001"},
                {"envelope": {"device_id": "dev-0001"}, **_DEVICE, **s.device}, 0)
    table.apply(CUSTOMER_CONTEXT, 0, 0, {"customer_id": "cust-001"},
                {"envelope": {"device_id": "dev-0001"}, **_CUSTOMER, **s.customer}, 0)
    if s.signal is not None:
        severity, detail = s.signal
        table.apply(RISK_SIGNALS, 0, 0, None,
                    {"envelope": {"device_id": "dev-0001", "event_time": _EVENT_TIME_MS}, "signal_id": f"sig-{name.lower()}",
                     "signal_type": name, "severity": severity, "detail": detail,
                     "source_event_type": "ENROL_REQUESTED"}, 0)
    return table


def _summary(device: dict) -> str:
    return (f"{device['platform']} {device['os_version']}, rooted={str(device['rooted']).lower()}, "
            f"network=WIFI, country={device['last_country']}")


def risk_input(name: str) -> dict:
    """The facts the risk agent gives the model for this scenario's device."""
    table = _table(name)
    device = table.get_device("dev-0001")["device"]
    return risk_facts(_summary(device), device, table.get_customer("cust-001")["customer"],
                      table.list_risk_signals("dev-0001"))


def enrolment_input(name: str) -> dict | None:
    """The facts the enrolment agent gives the model, or None when a guardrail decides first."""
    table = _table(name)
    device = table.get_device("dev-0001")["device"]
    signals = signal_facts(table.list_risk_signals("dev-0001"))
    if EnrolmentAgent._pre_llm(device, signals) is not None:
        return None
    risk = {"score": SCENARIOS[name].risk_score, "rationale": "Representative assessment for the benchmark.",
            "signalIds": [s["signalId"] for s in signals]}
    return decision_facts(device, risk, signals)


def _cases() -> list[tuple[str, str, list[dict], dict]]:
    cases = []
    for name in SCENARIOS:
        cases.append(("risk", name, system_and_user(prompt("risk.v1"), risk_input(name)), RISK_SCHEMA))
    for name in SCENARIOS:
        facts = enrolment_input(name)
        if facts is not None:
            cases.append(("enrolment", name, system_and_user(prompt("enrolment.v1"), facts), DECISION_SCHEMA))
    return cases


def run(provider: LLMProvider, models: list[str], runs: int, options: dict, timeout_ms: int,
        progress=lambda line: None) -> list[Sample]:
    cases = _cases()
    samples = []
    for model in models:
        def request(prompt_name, scenario, messages, schema):
            return LLMRequest(provider="ollama", model=model, messages=messages, schema=schema, options=options,
                              prompt_ref=f"{prompt_name}.v1", input_message_id=scenario, timeout_ms=timeout_ms)
        provider.complete(request(*cases[0]))
        for i in range(runs):
            for prompt_name, scenario, messages, schema in cases:
                samples.append(Sample(model, prompt_name, scenario,
                                      provider.complete(request(prompt_name, scenario, messages, schema))))
            progress(f"{model}: run {i + 1}/{runs}")
    return samples


def percentile(values: list[float], p: int) -> float:
    ordered = sorted(values)
    rank = max(1, -(-p * len(ordered) // 100))
    return ordered[rank - 1]


def _schema_invalid(resp: LLMResponse) -> bool:
    return resp.status != OK and resp.raw is not None


def summarize(samples: list[Sample]) -> list[dict]:
    groups: dict[tuple[str, str], list[Sample]] = {}
    for s in samples:
        groups.setdefault((s.model, s.prompt), []).append(s)
    rows = []
    for (model, prompt_name), group in groups.items():
        latencies = [s.response.latency_ms for s in group]
        valid = sum(s.response.status == OK for s in group)
        invalid = sum(_schema_invalid(s.response) for s in group)
        rows.append({"model": model, "prompt": prompt_name, "calls": len(group),
                     "p50_ms": percentile(latencies, 50), "p95_ms": percentile(latencies, 95),
                     "schema_valid": valid, "schema_invalid": invalid, "failed": len(group) - valid - invalid})
    return rows


def outcomes(samples: list[Sample]) -> list[dict]:
    groups: dict[tuple[str, str], list[Sample]] = {}
    for s in samples:
        groups.setdefault((s.model, s.scenario), []).append(s)
    rows = []
    for (model, scenario), group in groups.items():
        scores = [s.response.content["score"] for s in group if s.prompt == "risk" and s.response.status == OK]
        enrolment = [s for s in group if s.prompt == "enrolment"]
        decisions = Counter(s.response.content["decision"] for s in enrolment if s.response.status == OK)
        if not enrolment:
            decided = "guardrail (no model call)"
        else:
            decided = ", ".join(f"{d}×{n}" for d, n in decisions.most_common()) or "no valid output"
        rows.append({"model": model, "scenario": scenario,
                     "risk_score_p50": percentile(scores, 50) if scores else None, "decisions": decided})
    return rows


def environment() -> dict:
    def sh(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"machine": sh("sysctl", "-n", "hw.model") or platform.machine(),
            "chip": sh("sysctl", "-n", "machdep.cpu.brand_string"),
            "memory_gb": round(int(sh("sysctl", "-n", "hw.memsize") or 0) / 2**30) or None,
            "os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
            "ollama": sh("ollama", "--version").removeprefix("ollama version is ")}


def render(rows: list[dict], outcome_rows: list[dict], env: dict, runs: int, options: dict) -> str:
    lines = [f"Environment: {env['chip'] or env['machine']}, {env['memory_gb']} GB, {env['os']}, "
             f"Ollama {env['ollama']}. Options: {json.dumps(options)}. {runs} runs per scenario.", "",
             "| Model | Prompt | Calls | p50 ms | p95 ms | Schema-valid | Invalid | Failed |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['model']}` | {r['prompt']} | {r['calls']} | {r['p50_ms']} | {r['p95_ms']} | "
                     f"{r['schema_valid'] / r['calls']:.0%} | {r['schema_invalid']} | {r['failed']} |")
    lines += ["", "| Model | Scenario | Risk score p50 | Enrolment model decisions (before guardrails) |", "|---|---|---|---|"]
    for r in outcome_rows:
        lines.append(f"| `{r['model']}` | {r['scenario']} | {r['risk_score_p50']} | {r['decisions']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--json", help="also write the samples and summary to this file")
    args = parser.parse_args(argv)

    samples = run(OllamaProvider(args.endpoint), args.models, args.runs, DEFAULT_OPTIONS, DEFAULT_TIMEOUT_MS,
                  progress=lambda line: print(line, file=sys.stderr))
    rows, outcome_rows, env = summarize(samples), outcomes(samples), environment()
    print(render(rows, outcome_rows, env, args.runs, DEFAULT_OPTIONS))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"environment": env, "options": DEFAULT_OPTIONS, "runs": args.runs, "summary": rows,
                       "outcomes": outcome_rows,
                       "samples": [{**asdict(s), "response": {**asdict(s.response), "raw": None}}
                                   for s in samples]}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())

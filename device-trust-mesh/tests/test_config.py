import copy
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from dtm.common.config import KNOWN_AGENTS, ConfigError, FileConfigSource

FIXTURE = Path(__file__).parent / "fixtures" / "etc-dtm"
PACKAGED_RISK = Path(__file__).parents[1] / "dtm" / "prompts" / "risk.v1"
ENDPOINT = "http://h:11434"


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = Path(self.tmp) / "etc-dtm"
        shutil.copytree(FIXTURE, self.root)

    def src(self, **env):
        env = {"LLM_ENDPOINT": ENDPOINT, **env}
        return FileConfigSource(str(self.root), {k: v for k, v in env.items() if v is not None})

    def edit_agents(self, **edits):
        doc = yaml.safe_load((FIXTURE / "agents.yaml").read_text())
        agents = doc["agents"]
        if "model" in edits:
            doc["llm"]["model"] = edits["model"]
        if "risk_llm" in edits:
            agents["risk"]["llm"] = edits["risk_llm"]
        if "risk_enabled" in edits:
            agents["risk"]["enabled"] = edits["risk_enabled"]
        if "risk_replicas" in edits:
            agents["risk"]["replicas"] = edits["risk_replicas"]
        if "bus" in edits:
            doc["bus"].update(edits["bus"])
        if "context" in edits:
            doc["context"].update(edits["context"])
        if "agents" in edits:
            doc["agents"].update(edits["agents"])
        (self.root / "agents.yaml").write_text(yaml.safe_dump(doc))

    def write_agents_text(self, extra: str) -> None:
        """Write the fixture's agents.yaml with its ``context`` block replaced by ``extra`` (YAML text)."""
        text = (FIXTURE / "agents.yaml").read_text()
        head = text[:text.index("context:")]
        (self.root / "agents.yaml").write_text(head + extra)

    def load_with(self, risk_prompt=None, **edits):
        self.edit_agents(**edits)
        if risk_prompt is not None:
            (self.root / "prompts" / "risk.v1").write_text(risk_prompt)
        return self.src().load()


def enrolment_with(guardrails) -> dict:
    return {"enabled": True, "replicas": 1, "promptRef": "enrolment.v1", "guardrails": guardrails}


MCP_URL = "http://device-trust-mesh-serve-svc.device-trust-mesh.svc.cluster.local:8000/context-mcp/mcp"
UNVERSIONABLE = {
    "unquoted date": f"context:\n  mcpUrl: {MCP_URL}\n  since: 2026-10-31\n",
    "mixed-type keys": f"context:\n  mcpUrl: {MCP_URL}\n  1: a\n  b: c\n",
}
INVALID_CONTEXT = {
    "no context block": "",
    "context not a mapping": "context: [a]\n",
    "missing mcpUrl": "context:\n  waitTimeoutMs: 2000\n",
    "empty mcpUrl": "context:\n  mcpUrl: ''\n",
    "non-string mcpUrl": "context:\n  mcpUrl: 8000\n",
    "zero waitTimeoutMs": f"context:\n  mcpUrl: {MCP_URL}\n  waitTimeoutMs: 0\n",
    "negative waitTimeoutMs": f"context:\n  mcpUrl: {MCP_URL}\n  waitTimeoutMs: -1\n",
    "string waitTimeoutMs": f"context:\n  mcpUrl: {MCP_URL}\n  waitTimeoutMs: '2000'\n",
    "boolean waitTimeoutMs": f"context:\n  mcpUrl: {MCP_URL}\n  waitTimeoutMs: true\n",
    "float waitTimeoutMs": f"context:\n  mcpUrl: {MCP_URL}\n  waitTimeoutMs: 2000.5\n",
}


class LoadTests(TempRoot):
    def test_loads_gitops_agents_yaml(self):
        c = self.src().load()
        self.assertEqual(set(c.agents), set(KNOWN_AGENTS))
        self.assertFalse(c.agents["revocation"].enabled)
        self.assertEqual(c.llm["endpoint"], ENDPOINT)
        self.assertEqual(c.llm["timeoutMs"], 30000)
        self.assertEqual(c.bus.policy_commands, "enabled")
        self.assertEqual(c.context["waitTimeoutMs"], 2000)
        self.assertEqual(c.agents["risk"].prompt_text, (FIXTURE / "prompts/risk.v1").read_text())
        self.assertEqual(c.agents["risk"].prompt_ref, "risk.v1")
        self.assertIsNone(c.agents["step-up"].prompt_ref)
        self.assertIsNone(c.agents["step-up"].prompt_text)
        self.assertEqual(c.agents["step-up"].replicas, 1)

    def test_missing_env_var_is_config_error(self):
        with self.assertRaises(ConfigError):
            self.src(LLM_ENDPOINT=None).load()

    def test_interpolation_only_touches_string_values(self):
        c = self.src().load()
        self.assertIs(type(c.llm["timeoutMs"]), int)
        self.assertEqual(c.llm["options"]["temperature"], 0)

    def test_per_agent_llm_override_merges(self):
        c = self.load_with(risk_llm={"model": "qwen3:8b"})
        self.assertEqual(c.agents["risk"].llm["model"], "qwen3:8b")
        self.assertEqual(c.agents["risk"].llm["options"]["seed"], 42)
        self.assertEqual(c.agents["enrolment"].llm["model"], "qwen3:4b")
        self.assertEqual(c.llm["model"], "qwen3:4b")

    def test_per_agent_options_merge_key_by_key(self):
        c = self.load_with(risk_llm={"options": {"seed": 7}})
        self.assertEqual(c.agents["risk"].llm["options"], {"temperature": 0, "seed": 7, "think": False, "num_ctx": 4096})

    def test_replicas_bounded_by_partitions(self):
        with self.assertRaises(ConfigError):
            self.load_with(risk_replicas=7)
        with self.assertRaises(ConfigError):
            self.load_with(risk_replicas=0)
        self.assertEqual(self.load_with(risk_replicas=6).agents["risk"].replicas, 6)

    def test_unknown_agent_name_is_config_error(self):
        with self.assertRaises(ConfigError):
            self.load_with(agents={"sentinel": {"enabled": True, "replicas": 1}})

    def test_unknown_provider_is_config_error(self):
        with self.assertRaises(ConfigError):
            self.load_with(risk_llm={"provider": "carrier-pigeon"})

    def test_guardrails_threshold_parsed(self):
        c = self.load_with(agents={"enrolment": enrolment_with({"challengeAtRiskScore": 80})})
        self.assertEqual(c.agents["enrolment"].guardrails, {"challengeAtRiskScore": 80})
        self.assertEqual(self.src().load().agents["risk"].guardrails, {})
        self.assertEqual(self.load_with(agents={"enrolment": enrolment_with({"challengeAtRiskScore": 0})})
                         .agents["enrolment"].guardrails, {"challengeAtRiskScore": 0})

    def test_guardrails_rejects_unknown_key_and_out_of_range(self):
        bad = {"unknown key": {"threshold": 80}, "negative": {"challengeAtRiskScore": -1},
               "above range": {"challengeAtRiskScore": 101}, "string": {"challengeAtRiskScore": "80"},
               "boolean": {"challengeAtRiskScore": True}, "float": {"challengeAtRiskScore": 80.5}}
        for what, guardrails in bad.items():
            with self.subTest(what), self.assertRaisesRegex(ConfigError, "guardrails"):
                self.load_with(agents={"enrolment": enrolment_with(guardrails)})
        with self.assertRaisesRegex(ConfigError, "guardrails"):
            self.load_with(agents={"enrolment": enrolment_with([80])})

    def test_guardrails_change_changes_config_version(self):
        low = self.load_with(agents={"enrolment": enrolment_with({"challengeAtRiskScore": 50})})
        high = self.load_with(agents={"enrolment": enrolment_with({"challengeAtRiskScore": 80})})
        self.assertNotEqual(low.agents["enrolment"].config_version, high.agents["enrolment"].config_version)
        self.assertNotEqual(low.agents["enrolment"].config_version,
                            self.src().load().agents["enrolment"].config_version)

    def test_unknown_prompt_ref_raises(self):
        with self.assertRaisesRegex(ConfigError, "nope.v1.*prompts/.*packaged"):
            self.load_with(agents={"risk": {"enabled": True, "replicas": 1, "promptRef": "nope.v1"}})

    def test_prompt_falls_back_to_packaged(self):
        (self.root / "prompts" / "risk.v1").unlink()
        agent = self.src().load().agents["risk"]
        self.assertEqual(agent.prompt_text, PACKAGED_RISK.read_text(encoding="utf-8"))
        self.assertEqual(agent.prompt_ref, "risk.v1")

    def test_mounted_prompt_overrides_packaged(self):
        (self.root / "prompts" / "risk.v1").unlink()
        packaged = self.src().load().agents["risk"]
        (self.root / "prompts" / "risk.v1").write_text("mounted override")
        mounted = self.src().load().agents["risk"]
        self.assertEqual(mounted.prompt_text, "mounted override")
        self.assertNotEqual(mounted.config_version, packaged.config_version)

    def test_packaged_prompt_ref_cannot_leave_the_prompts_package(self):
        for ref in ("../__init__.py", "../prompts/risk.v1", ".."):
            with self.subTest(ref=ref), self.assertRaises(ConfigError):
                self.load_with(agents={"risk": {"enabled": True, "replicas": 1, "promptRef": ref}})

    def test_prompt_ref_cannot_leave_prompts_dir(self):
        (self.root / "secret").write_text("x")
        with self.assertRaises(ConfigError):
            self.load_with(agents={"risk": {"enabled": True, "replicas": 1, "promptRef": "../secret"}})

    def test_invalid_bus_block_is_config_error(self):
        with self.assertRaises(ConfigError):
            self.load_with(bus={"policyCommands": "redirect", "topicPrefix": ""})

    def test_invalid_yaml_is_config_error(self):
        (self.root / "agents.yaml").write_text("llm: [unclosed")
        with self.assertRaises(ConfigError):
            self.src().load()

    def test_unversionable_values_are_config_errors(self):
        for name, text in UNVERSIONABLE.items():
            with self.subTest(name):
                self.write_agents_text(text)
                with self.assertRaises(ConfigError):
                    self.src().load()

    def test_agent_settings_carry_the_context_block(self):
        c = self.src().load()
        for name in KNOWN_AGENTS:
            with self.subTest(name):
                self.assertEqual(c.agents[name].context, {"mcpUrl": MCP_URL, "waitTimeoutMs": 2000})

    def test_wait_timeout_is_optional(self):
        self.write_agents_text(f"context:\n  mcpUrl: {MCP_URL}\n")
        self.assertEqual(self.src().load().agents["risk"].context, {"mcpUrl": MCP_URL})

    def test_invalid_context_is_config_error(self):
        for name, text in INVALID_CONTEXT.items():
            with self.subTest(name):
                self.write_agents_text(text)
                with self.assertRaisesRegex(ConfigError, "context"):
                    self.src().load()

    def test_unknown_agent_key_is_config_error(self):
        with self.assertRaisesRegex(ConfigError, "replica\\b"):
            self.load_with(agents={"risk": {"enabled": True, "replica": 2}})

    def test_unknown_top_level_key_is_config_error(self):
        self.write_agents_text(f"context:\n  mcpUrl: {MCP_URL}\ncontexts:\n  mcpUrl: {MCP_URL}\n")
        with self.assertRaisesRegex(ConfigError, "contexts"):
            self.src().load()

    def test_unknown_bus_key_is_config_error(self):
        with self.assertRaisesRegex(ConfigError, "policyCommand"):
            self.load_with(bus={"policyCommand": "disabled"})

    def test_missing_agents_yaml_is_config_error(self):
        (self.root / "agents.yaml").unlink()
        with self.assertRaises(ConfigError):
            self.src().load()


class ConfigVersionTests(TempRoot):
    def test_config_version_changes_with_model_prompt_and_enabled(self):
        base = self.src().load().agents["risk"].config_version
        self.assertEqual(len(base), 12)
        int(base, 16)
        self.assertNotEqual(self.load_with(model="qwen3:8b").agents["risk"].config_version, base)
        self.assertNotEqual(self.load_with(risk_prompt="new text").agents["risk"].config_version, base)
        self.assertNotEqual(self.load_with(risk_enabled=False).agents["risk"].config_version, base)

    def test_agent_without_prompt_ignores_prompt_edits(self):
        self.edit_agents()
        before = self.src().load().agents["step-up"].config_version
        after = self.load_with(risk_prompt="new text").agents["step-up"].config_version
        self.assertEqual(after, before)

    def test_bus_and_context_blocks_are_part_of_the_version(self):
        base = self.src().load().agents["step-up"].config_version
        self.assertNotEqual(self.load_with(bus={"policyCommands": "disabled"}).agents["step-up"].config_version, base)
        self.assertNotEqual(self.load_with(context={"waitTimeoutMs": 500}).agents["step-up"].config_version, base)

    def test_version_follows_the_interpolated_endpoint(self):
        a = self.src().load().agents["step-up"].config_version
        b = self.src(LLM_ENDPOINT="http://other:11434").load().agents["step-up"].config_version
        self.assertNotEqual(a, b)

    def test_version_is_stable_across_loads(self):
        self.assertEqual(self.src().load().agents["risk"].config_version, self.src().load().agents["risk"].config_version)


class RefreshTests(TempRoot):
    def test_refresh_returns_none_when_unchanged_and_new_config_when_changed(self):
        s = self.src()
        first = s.load()
        self.assertIsNone(s.refresh())
        (self.root / "prompts" / "risk.v1").write_text("changed")
        changed = s.refresh()
        self.assertIsNotNone(changed)
        self.assertEqual(changed.agents["risk"].prompt_text, "changed")
        self.assertNotEqual(changed.digest, first.digest)
        self.assertNotEqual(changed.agents["risk"].config_version, first.agents["risk"].config_version)
        self.assertIsNone(s.refresh())

    def test_refresh_picks_up_agents_yaml_change(self):
        s = self.src()
        s.load()
        self.edit_agents(model="qwen3:8b")
        self.assertEqual(s.refresh().llm["model"], "qwen3:8b")

    def test_refresh_keeps_last_good_on_invalid_yaml(self):
        s = self.src()
        s.load()
        (self.root / "agents.yaml").write_text("llm: [unclosed")
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())

    def test_refresh_keeps_last_good_on_config_error(self):
        s = self.src()
        s.load()
        self.edit_agents(risk_replicas=99)
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())

    def test_refresh_keeps_last_good_when_a_prompt_file_is_missing(self):
        s = self.src()
        s.load()
        self.edit_agents(agents={"risk": {"enabled": True, "replicas": 1, "promptRef": "nope.v1"}})
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())

    def test_bad_read_is_not_recorded_as_seen(self):
        s = self.src()
        s.load()
        good_yaml = (self.root / "agents.yaml").read_text()
        (self.root / "agents.yaml").write_text("llm: [unclosed")
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())
        self.assertIsNone(s.refresh())
        (self.root / "agents.yaml").write_text(good_yaml + "# touched\n")
        self.assertIsNotNone(s.refresh())

    def test_a_rejected_config_is_logged_once_until_it_changes(self):
        s = self.src()
        s.load()
        (self.root / "agents.yaml").write_text("llm: [unclosed")
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())
        with self.assertNoLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())
            self.assertIsNone(s.refresh())
        (self.root / "agents.yaml").write_text("llm: [still unclosed")
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())

    def test_the_same_bad_config_is_logged_again_after_a_good_one(self):
        s = self.src()
        s.load()
        good_yaml = (self.root / "agents.yaml").read_text()
        for _ in range(2):
            (self.root / "agents.yaml").write_text("llm: [unclosed")
            with self.assertLogs("dtm.common.config", logging.ERROR):
                self.assertIsNone(s.refresh())
            (self.root / "agents.yaml").write_text(good_yaml)
            self.assertIsNone(s.refresh())

    def test_an_unreadable_config_is_logged_once_per_error(self):
        s = self.src()
        s.load()
        (self.root / "agents.yaml").unlink()
        with self.assertLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())
        with self.assertNoLogs("dtm.common.config", logging.ERROR):
            self.assertIsNone(s.refresh())

    def test_refresh_keeps_last_good_on_unversionable_values(self):
        for name, text in UNVERSIONABLE.items():
            with self.subTest(name):
                s = self.src()
                good = s.load()
                self.write_agents_text(text)
                with self.assertLogs("dtm.common.config", logging.ERROR):
                    self.assertIsNone(s.refresh())
                (self.root / "agents.yaml").write_text((FIXTURE / "agents.yaml").read_text())
                self.assertIsNone(s.refresh())
                self.assertEqual(s.load().digest, good.digest)

    def test_refresh_without_prior_load_loads(self):
        self.assertIsNotNone(self.src().refresh())

    def test_a_good_config_after_a_bad_one_is_returned(self):
        s = self.src()
        s.load()
        (self.root / "agents.yaml").write_text("llm: [unclosed")
        with self.assertLogs("dtm.common.config", logging.ERROR):
            s.refresh()
        self.edit_agents(model="qwen3:8b")
        self.assertEqual(s.refresh().llm["model"], "qwen3:8b")


def write_volume(directory: Path, files: dict[str, str], generation: str) -> None:
    """Lay out ``directory`` like a mounted ConfigMap: name -> ..data/name, ..data -> ..<generation>."""
    directory.mkdir(parents=True, exist_ok=True)
    gen = directory / f"..{generation}"
    gen.mkdir()
    for name, text in files.items():
        (gen / name).write_text(text)
        link = directory / name
        if not link.is_symlink():
            link.symlink_to(f"..data/{name}")
    tmp = directory / "..data_tmp"
    tmp.symlink_to(gen.name)
    os.replace(tmp, directory / "..data")


class SymlinkSwapTests(TempRoot):
    def build(self, risk_prompt="v1"):
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.agents_text = (FIXTURE / "agents.yaml").read_text()
        write_volume(self.root, {"agents.yaml": self.agents_text}, "2026_10_31_01_00_00.1")
        write_volume(self.root / "prompts", {"risk.v1": risk_prompt, "enrolment.v1": "e", "revocation.v1": "r"},
                     "2026_10_31_01_00_00.2")

    def test_configmap_symlink_swap_is_read(self):
        self.build()
        s = self.src()
        self.assertEqual(s.load().agents["risk"].prompt_text, "v1")
        self.assertIsNone(s.refresh())
        write_volume(self.root / "prompts", {"risk.v1": "v2", "enrolment.v1": "e", "revocation.v1": "r"},
                     "2026_10_31_01_05_00.3")
        c = s.refresh()
        self.assertIsNotNone(c)
        self.assertEqual(c.agents["risk"].prompt_text, "v2")

    def test_agents_yaml_symlink_swap_is_read(self):
        self.build()
        s = self.src()
        s.load()
        write_volume(self.root, {"agents.yaml": self.agents_text.replace("qwen3:4b", "qwen3:8b")},
                     "2026_10_31_01_05_00.4")
        self.assertEqual(s.refresh().llm["model"], "qwen3:8b")

    def test_hidden_generation_directories_are_not_prompts(self):
        self.build()
        c = self.src().load()
        self.assertEqual(c.agents["enrolment"].prompt_text, "e")


if __name__ == "__main__":
    unittest.main()

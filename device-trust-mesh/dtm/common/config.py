"""File-backed mesh configuration: ``agents.yaml`` plus the prompt files it references.

The files come from projected ConfigMap volumes, which Kubernetes updates by swapping a ``..data``
symlink. Every read goes through the resolved path, and a single snapshot of the bytes feeds both the
content digest and the parsed config, so each file is read consistently. A swap between reading
``agents.yaml`` and the prompts can pair files from different generations; that config carries the digest
of what was read, and the next refresh sees the settled files as a new digest.
"""
import copy
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from dtm.common.bus import BusConfig

log = logging.getLogger(__name__)

CONFIG_ROOT = "/etc/dtm"
KNOWN_AGENTS = ("risk", "enrolment", "step-up", "compliance", "revocation")

_PROVIDERS = ("ollama", "openai-compatible", "recorded")
_MIN_REPLICAS = 1
_MAX_REPLICAS = 6
_VERSION_LENGTH = 12
_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(ValueError):
    """Raised when the configuration files are missing, unparseable, or invalid."""


@dataclass(frozen=True)
class AgentSettings:
    name: str
    enabled: bool
    replicas: int
    prompt_ref: str | None
    prompt_text: str | None
    llm: dict
    config_version: str


@dataclass(frozen=True)
class MeshConfig:
    llm: dict
    agents: dict[str, AgentSettings]
    bus: BusConfig
    context: dict
    digest: str


def _interpolate(value, env: Mapping[str, str]):
    """Replace ``${NAME}`` inside string values, recursively; a variable missing from ``env`` is an error."""
    if isinstance(value, str):
        def substitute(match):
            name = match.group(1)
            if name not in env:
                raise ConfigError(f"environment variable {name} is not set")
            return env[name]
        return _VAR.sub(substitute, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, env) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _mapping(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be a mapping")
    return value


def _validate_llm(llm: dict, what: str) -> None:
    if llm.get("provider") not in _PROVIDERS:
        raise ConfigError(f"{what} provider must be one of {', '.join(_PROVIDERS)}; got {llm.get('provider')!r}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class _Snapshot:
    agents_yaml: bytes
    prompts: dict[str, bytes]

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(self.agents_yaml)
        for name in sorted(self.prompts):
            h.update(b"\0" + name.encode() + b"\0")
            h.update(self.prompts[name])
        return h.hexdigest()


class FileConfigSource:
    def __init__(self, root: str = CONFIG_ROOT, env: Mapping[str, str] = os.environ):
        self._root = Path(root)
        self._env = env
        self._digest: str | None = None

    def load(self) -> MeshConfig:
        config = self._build(self._snapshot())
        self._digest = config.digest
        return config

    def refresh(self) -> MeshConfig | None:
        """Return a new config when the files' content changed and is valid; otherwise None.

        An unreadable or invalid change is logged and leaves the caller on its last good config. Its
        digest is not remembered, so the next valid write is picked up.
        """
        try:
            snapshot = self._snapshot()
            if snapshot.digest == self._digest:
                return None
            config = self._build(snapshot)
        except ConfigError as e:
            log.error("config reload rejected, keeping last good config: %s", e)
            return None
        self._digest = config.digest
        return config

    @staticmethod
    def config_version(agent: dict, llm: dict, bus: dict, context: dict, prompt_text: str | None) -> str:
        """First 12 hex digits of the SHA-256 over everything that shapes an agent's behaviour."""
        try:
            canonical = json.dumps({"agent": agent, "llm": llm, "bus": bus, "context": context, "prompt": prompt_text},
                                   sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"configuration contains values that cannot be versioned: {e}") from e
        return hashlib.sha256(canonical.encode()).hexdigest()[:_VERSION_LENGTH]

    def _snapshot(self) -> _Snapshot:
        try:
            agents_yaml = (self._root / "agents.yaml").read_bytes()
            prompts = {}
            prompts_dir = self._root / "prompts"
            if prompts_dir.is_dir():
                for entry in sorted(prompts_dir.iterdir()):
                    if entry.name.startswith("..") or not entry.is_file():
                        continue
                    prompts[entry.name] = entry.read_bytes()
        except OSError as e:
            raise ConfigError(f"cannot read configuration under {self._root}: {e}") from e
        return _Snapshot(agents_yaml, prompts)

    def _build(self, snapshot: _Snapshot) -> MeshConfig:
        try:
            doc = yaml.safe_load(snapshot.agents_yaml)
        except yaml.YAMLError as e:
            raise ConfigError(f"agents.yaml is not valid YAML: {e}") from e
        doc = _interpolate(_mapping(doc, "agents.yaml"), self._env)

        llm = _mapping(doc.get("llm"), "llm")
        _validate_llm(llm, "llm")
        bus_block = _mapping(doc.get("bus", {}), "bus")
        context = _mapping(doc.get("context", {}), "context")
        try:
            bus = BusConfig.from_dict(bus_block)
        except (ValueError, TypeError) as e:
            raise ConfigError(f"invalid bus block: {e}") from e

        agents = {}
        for name, raw in _mapping(doc.get("agents"), "agents").items():
            if name not in KNOWN_AGENTS:
                raise ConfigError(f"unknown agent {name!r}; expected one of {', '.join(KNOWN_AGENTS)}")
            agents[name] = self._agent(name, _mapping(raw, f"agents.{name}"), llm, bus_block, context, snapshot)
        return MeshConfig(llm=llm, agents=agents, bus=bus, context=context, digest=snapshot.digest)

    def _agent(self, name: str, raw: dict, llm: dict, bus_block: dict, context: dict,
               snapshot: _Snapshot) -> AgentSettings:
        enabled = raw.get("enabled", True)
        replicas = raw.get("replicas", 1)
        if not isinstance(enabled, bool):
            raise ConfigError(f"agents.{name}.enabled must be a boolean")
        if not _is_int(replicas) or not _MIN_REPLICAS <= replicas <= _MAX_REPLICAS:
            raise ConfigError(f"agents.{name}.replicas must be between {_MIN_REPLICAS} and {_MAX_REPLICAS}; got {replicas!r}")

        merged = _deep_merge(llm, _mapping(raw.get("llm", {}), f"agents.{name}.llm"))
        _validate_llm(merged, f"agents.{name}.llm")

        prompt_ref = raw.get("promptRef")
        prompt_text = None
        if prompt_ref is not None:
            if prompt_ref not in snapshot.prompts:
                raise ConfigError(f"agents.{name}.promptRef {prompt_ref!r} has no file under prompts/")
            try:
                prompt_text = snapshot.prompts[prompt_ref].decode("utf-8")
            except UnicodeDecodeError as e:
                raise ConfigError(f"prompt {prompt_ref!r} is not valid UTF-8: {e}") from e

        version = self.config_version(raw, merged, bus_block, context, prompt_text)
        return AgentSettings(name, enabled, replicas, prompt_ref, prompt_text, merged, version)

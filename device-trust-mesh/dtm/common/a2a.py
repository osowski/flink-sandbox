"""A2A v1.0 records for the mesh Kafka binding: builders, validation, and wire serialization.

Records are plain dicts in the A2A JSON (protojson, camelCase) form. They are validated against the
vendored JSON Schemas and the a2a-sdk protobuf types, and serialized directly from the dict the caller
built; a protobuf round trip would turn integers into floats.
"""
import copy
import json
from datetime import datetime, timezone

from a2a.types import AgentCard, Message, StreamResponse
from google.protobuf.json_format import ParseDict, ParseError
from jsonschema.exceptions import best_match

from dtm.common.envelope import ENVELOPE_URI, Envelope
from dtm.common.schemas import a2a_validator

KAFKA_BINDING = "urn:device-trust-mesh:kafka:v1"
REPLY_TOPIC = "agent.task-events"
CARDS_TOPIC = "agent.cards"

_MODES = ["application/json", "text/plain"]


class A2AValidationError(ValueError):
    """Raised when a record does not satisfy its topic's schema or the a2a-sdk types."""


def task_topic(agent: str) -> str:
    return f"agent.tasks.{agent}"


def parts(data: dict, text: str) -> list[dict]:
    """Return the two-part body of a request or artifact: the data part, then the text summary."""
    return [{"data": data, "mediaType": "application/json"},
            {"text": text, "mediaType": "text/plain"}]


def _metadata(envelope: Envelope) -> dict:
    return {ENVELOPE_URI: envelope.to_a2a()}


def send_message_request(envelope: Envelope, data: dict, text: str, *, task_id: str | None = None,
                         reference_task_ids: list[str] | None = None) -> dict:
    message = {
        "messageId": envelope.message_id,
        "contextId": envelope.correlation_id,
        "role": "ROLE_USER",
        "extensions": [ENVELOPE_URI],
        "metadata": _metadata(envelope),
        "parts": parts(data, text),
    }
    if task_id is not None:
        message["taskId"] = task_id
    if reference_task_ids is not None:
        message["referenceTaskIds"] = list(reference_task_ids)
    return {"jsonrpc": "2.0", "id": envelope.message_id, "method": "SendMessage",
            "params": {"message": message}}


def _result(rpc_id: str, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def task_event(rpc_id: str, task_id: str, context_id: str, state: str, envelope: Envelope,
               timestamp: str) -> dict:
    return _result(rpc_id, {"task": {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state, "timestamp": timestamp},
        "metadata": _metadata(envelope),
    }})


def status_update(rpc_id: str, task_id: str, context_id: str, state: str, envelope: Envelope,
                  timestamp: str, text: str | None = None) -> dict:
    status = {"state": state, "timestamp": timestamp}
    if text is not None:
        status["message"] = {
            "messageId": envelope.message_id,
            "contextId": context_id,
            "taskId": task_id,
            "role": "ROLE_AGENT",
            "parts": [{"text": text, "mediaType": "text/plain"}],
        }
    return _result(rpc_id, {"statusUpdate": {
        "taskId": task_id,
        "contextId": context_id,
        "status": status,
        "metadata": _metadata(envelope),
    }})


def artifact_update(rpc_id: str, task_id: str, context_id: str, artifact_id: str, name: str,
                    data: dict, text: str, envelope: Envelope) -> dict:
    return _result(rpc_id, {"artifactUpdate": {
        "taskId": task_id,
        "contextId": context_id,
        "artifact": {"artifactId": artifact_id, "name": name, "parts": parts(data, text)},
        "metadata": _metadata(envelope),
    }})


def agent_card(name: str, description: str, skills: list[dict], version: str, bootstrap: str) -> dict:
    return {
        "name": name,
        "description": description,
        "version": version,
        "supportedInterfaces": [{
            "url": f"kafka://{bootstrap}/{task_topic(name)}",
            "protocolBinding": KAFKA_BINDING,
            "protocolVersion": "1.0",
        }],
        "capabilities": {"extensions": [{"uri": ENVELOPE_URI}]},
        "defaultInputModes": list(_MODES),
        "defaultOutputModes": list(_MODES),
        "skills": skills,
    }


def _path(error) -> str:
    return ".".join(str(p) for p in error.absolute_path) or "<root>"


def _sdk_check(topic: str, record: dict) -> None:
    """Parse the record's A2A payload into the matching a2a-sdk type, which rejects unknown members."""
    if topic == CARDS_TOPIC:
        ParseDict(copy.deepcopy(record), AgentCard())
    elif topic == REPLY_TOPIC:
        ParseDict(copy.deepcopy(record["result"]), StreamResponse())
    else:
        ParseDict(copy.deepcopy(record["params"]["message"]), Message())


def validate(topic: str, record: dict) -> None:
    """Validate a record for a logical topic; raise ``A2AValidationError`` on any failure."""
    error = best_match(a2a_validator(topic).iter_errors(record))
    if error is not None:
        raise A2AValidationError(f"{topic}: {_path(error)}: {error.message}")
    try:
        _sdk_check(topic, record)
    except ParseError as e:
        raise A2AValidationError(f"{topic}: a2a-sdk: {e}") from e


def dumps(record: dict) -> bytes:
    """Serialize a record to compact UTF-8 JSON, keeping integers as integers."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def now_iso(epoch_ms: int) -> str:
    """Return an RFC 3339 UTC timestamp with millisecond precision and a ``Z`` suffix."""
    moment = datetime.fromtimestamp(epoch_ms // 1000, timezone.utc).replace(microsecond=epoch_ms % 1000 * 1000)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

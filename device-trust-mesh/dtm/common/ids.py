"""Deterministic identifiers so a redelivered input reproduces the same output ids."""
import uuid

DTM_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:device-trust-mesh")


def _derive(*parts: object) -> str:
    return str(uuid.uuid5(DTM_NAMESPACE, "|".join(str(p) for p in parts)))


def message_id(agent_id: str, causation_id: str, config_version: str, output_index: int) -> str:
    """Id of the ``output_index``-th message an agent emits in response to ``causation_id``."""
    return _derive(agent_id, causation_id, config_version, output_index)


def task_id(agent_id: str, first_input_message_id: str) -> str:
    """Id of the task an agent opens for the first message it receives about it."""
    return _derive(agent_id, first_input_message_id)

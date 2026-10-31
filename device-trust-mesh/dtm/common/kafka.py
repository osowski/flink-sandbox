"""Kafka client settings and endpoints shared by every producer and consumer in the mesh."""
import os

_DEFAULT_BOOTSTRAP = "kafka.kafka.svc.cluster.local:9071"
_DEFAULT_SCHEMA_REGISTRY = "http://schemaregistry.kafka.svc.cluster.local:8081"


def bootstrap_servers() -> str:
    return os.environ.get("KAFKA_BOOTSTRAP_SERVERS", _DEFAULT_BOOTSTRAP)


def schema_registry_url() -> str:
    return os.environ.get("SCHEMA_REGISTRY_URL", _DEFAULT_SCHEMA_REGISTRY)


def producer_config(bootstrap: str) -> dict:
    return {
        "bootstrap.servers": bootstrap,
        "linger.ms": 5,
        "acks": "all",
        "enable.idempotence": True,
        "compression.type": "lz4",
    }


def consumer_config(bootstrap: str, group_id: str, *, auto_offset_reset: str = "latest",
                    enable_auto_commit: bool = False) -> dict:
    """Return consumer settings; every consumer reads only committed records because Flink sinks are transactional."""
    return {
        "bootstrap.servers": bootstrap,
        "group.id": group_id,
        "auto.offset.reset": auto_offset_reset,
        "enable.auto.commit": enable_auto_commit,
        "fetch.min.bytes": 1,
        "fetch.wait.max.ms": 50,
        "isolation.level": "read_committed",
    }

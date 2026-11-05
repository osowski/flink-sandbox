"""Kafka client settings and endpoints shared by every producer and consumer in the mesh."""
import os
from dataclasses import dataclass

_DEFAULT_BOOTSTRAP = "kafka.kafka.svc.cluster.local:9071"
_DEFAULT_SCHEMA_REGISTRY = "http://schemaregistry.kafka.svc.cluster.local:8081"


def bootstrap_servers() -> str:
    return os.environ.get("KAFKA_BOOTSTRAP_SERVERS", _DEFAULT_BOOTSTRAP)


def schema_registry_url() -> str:
    return os.environ.get("SCHEMA_REGISTRY_URL", _DEFAULT_SCHEMA_REGISTRY)


_OAUTH_VARS = (
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_OAUTH_TOKEN_ENDPOINT_URL",
    "KAFKA_OAUTH_CLIENT_ID",
    "KAFKA_OAUTH_CLIENT_SECRET",
)


@dataclass(frozen=True)
class OAuthSettings:
    client_id: str
    client_secret: str
    token_endpoint_url: str
    scope: str | None


def _oauth_env() -> dict[str, str] | None:
    """Return the required OAuth variables, None when none are set, and raise when only some are."""
    values = {name: os.environ.get(name, "") for name in _OAUTH_VARS}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"Kafka OAuth is partially configured; missing {', '.join(missing)}")
    if values["KAFKA_SASL_MECHANISM"] != "OAUTHBEARER":
        raise ValueError(f"KAFKA_SASL_MECHANISM must be OAUTHBEARER, got {values['KAFKA_SASL_MECHANISM']!r}")
    return values


def oauth_settings() -> OAuthSettings | None:
    values = _oauth_env()
    if values is None:
        return None
    return OAuthSettings(
        client_id=values["KAFKA_OAUTH_CLIENT_ID"],
        client_secret=values["KAFKA_OAUTH_CLIENT_SECRET"],
        token_endpoint_url=values["KAFKA_OAUTH_TOKEN_ENDPOINT_URL"],
        scope=os.environ.get("KAFKA_OAUTH_SCOPE") or None,
    )


def security_config() -> dict:
    """Return librdkafka OIDC client settings, or an empty dict when OAuth is not configured."""
    values = _oauth_env()
    if values is None:
        return {}
    settings = oauth_settings()
    config = {
        "security.protocol": values["KAFKA_SECURITY_PROTOCOL"],
        "sasl.mechanisms": values["KAFKA_SASL_MECHANISM"],
        "sasl.oauthbearer.method": "oidc",
        "sasl.oauthbearer.client.id": settings.client_id,
        "sasl.oauthbearer.client.secret": settings.client_secret,
        "sasl.oauthbearer.token.endpoint.url": settings.token_endpoint_url,
    }
    if settings.scope:
        config["sasl.oauthbearer.scope"] = settings.scope
    return config


# The client library requires these Confluent Cloud routing fields; Confluent Platform Schema Registry ignores them.
_SR_LOGICAL_CLUSTER = "confluent-platform"


def schema_registry_conf() -> dict:
    """Return Schema Registry client settings; OAuth reuses the Kafka client and token endpoint when configured."""
    conf = {"url": schema_registry_url()}
    settings = oauth_settings()
    if settings is None:
        return conf
    return {
        **conf,
        "bearer.auth.credentials.source": "OAUTHBEARER",
        "bearer.auth.client.id": settings.client_id,
        "bearer.auth.client.secret": settings.client_secret,
        "bearer.auth.issuer.endpoint.url": settings.token_endpoint_url,
        "bearer.auth.scope": settings.scope or "",
        "bearer.auth.logical.cluster": _SR_LOGICAL_CLUSTER,
    }


def schema_registry_client():
    from confluent_kafka.schema_registry import SchemaRegistryClient

    return SchemaRegistryClient(schema_registry_conf())


def producer_config(bootstrap: str) -> dict:
    return {
        "bootstrap.servers": bootstrap,
        "linger.ms": 5,
        "acks": "all",
        "enable.idempotence": True,
        "compression.type": "lz4",
        **security_config(),
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
        **security_config(),
    }

import pathlib
import unittest
from unittest import mock

import dtm
from dtm.common.kafka import (
    consumer_config,
    oauth_settings,
    producer_config,
    schema_registry_client,
    schema_registry_conf,
    schema_registry_url,
    security_config,
)

OAUTH_ENV = {
    "KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
    "KAFKA_SASL_MECHANISM": "OAUTHBEARER",
    "KAFKA_OAUTH_TOKEN_ENDPOINT_URL": "http://kc:8080/realms/confluent/protocol/openid-connect/token",
    "KAFKA_OAUTH_CLIENT_ID": "sa-dtm-ray",
    "KAFKA_OAUTH_CLIENT_SECRET": "s3cret",
}


def env(**overrides):
    return mock.patch.dict("os.environ", {**OAUTH_ENV, **overrides}, clear=True)


class KafkaSecurityTest(unittest.TestCase):
    def test_anonymous_when_env_unset(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(security_config(), {})
            self.assertIsNone(oauth_settings())

    def test_producer_and_consumer_unchanged_when_anonymous(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            for config in (producer_config("b:9092"), consumer_config("b:9092", "g")):
                self.assertFalse([k for k in config if k.startswith(("sasl.", "security."))])

    def test_oauth_env_yields_librdkafka_oidc_keys(self):
        with env():
            self.assertEqual(security_config(), {
                "security.protocol": "SASL_PLAINTEXT",
                "sasl.mechanisms": "OAUTHBEARER",
                "sasl.oauthbearer.method": "oidc",
                "sasl.oauthbearer.client.id": "sa-dtm-ray",
                "sasl.oauthbearer.client.secret": "s3cret",
                "sasl.oauthbearer.token.endpoint.url": OAUTH_ENV["KAFKA_OAUTH_TOKEN_ENDPOINT_URL"],
            })

    def test_oauth_settings_carry_credentials(self):
        with env(KAFKA_OAUTH_SCOPE="profile email"):
            settings = oauth_settings()
        self.assertEqual(settings.client_id, "sa-dtm-ray")
        self.assertEqual(settings.client_secret, "s3cret")
        self.assertEqual(settings.token_endpoint_url, OAUTH_ENV["KAFKA_OAUTH_TOKEN_ENDPOINT_URL"])
        self.assertEqual(settings.scope, "profile email")

    def test_scope_included_only_when_set(self):
        with env(KAFKA_OAUTH_SCOPE="profile email"):
            self.assertEqual(security_config()["sasl.oauthbearer.scope"], "profile email")
        with env():
            self.assertNotIn("sasl.oauthbearer.scope", security_config())
            self.assertIsNone(oauth_settings().scope)

    def test_producer_and_consumer_carry_oauth(self):
        with env():
            security = security_config()
            producer = producer_config("b:9092")
            consumer = consumer_config("b:9092", "g")
        self.assertLessEqual(security.items(), producer.items())
        self.assertLessEqual(security.items(), consumer.items())
        self.assertEqual(consumer["isolation.level"], "read_committed")

    def test_partial_oauth_env_raises_naming_missing_var(self):
        partial = {k: v for k, v in OAUTH_ENV.items() if k != "KAFKA_OAUTH_CLIENT_SECRET"}
        with mock.patch.dict("os.environ", partial, clear=True):
            with self.assertRaisesRegex(ValueError, "KAFKA_OAUTH_CLIENT_SECRET"):
                security_config()
            with self.assertRaisesRegex(ValueError, "KAFKA_OAUTH_CLIENT_SECRET"):
                oauth_settings()

    def test_unsupported_mechanism_raises(self):
        with env(KAFKA_SASL_MECHANISM="PLAIN"):
            with self.assertRaisesRegex(ValueError, "OAUTHBEARER"):
                security_config()

    def test_non_sasl_protocol_raises(self):
        with env(KAFKA_SECURITY_PROTOCOL="PLAINTEXT"):
            with self.assertRaisesRegex(ValueError, "KAFKA_SECURITY_PROTOCOL.*SASL_PLAINTEXT.*SASL_SSL") as ctx:
                security_config()
            self.assertNotIn(OAUTH_ENV["KAFKA_OAUTH_CLIENT_SECRET"], str(ctx.exception))
            with self.assertRaisesRegex(ValueError, "KAFKA_SECURITY_PROTOCOL"):
                oauth_settings()

    def test_sasl_ssl_protocol_is_accepted(self):
        with env(KAFKA_SECURITY_PROTOCOL="SASL_SSL"):
            self.assertEqual(security_config()["security.protocol"], "SASL_SSL")

    def test_secret_is_kept_out_of_repr(self):
        with env():
            self.assertNotIn(OAUTH_ENV["KAFKA_OAUTH_CLIENT_SECRET"], repr(oauth_settings()))


class SchemaRegistryConfTest(unittest.TestCase):
    def test_anonymous_conf_is_just_the_url(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(schema_registry_conf(), {"url": schema_registry_url()})

    def test_oauth_conf_reuses_the_kafka_client_and_token_endpoint(self):
        with env(KAFKA_OAUTH_SCOPE="profile email"):
            conf = schema_registry_conf()
        self.assertEqual(conf["url"], schema_registry_url())
        self.assertEqual(conf["bearer.auth.credentials.source"], "OAUTHBEARER")
        self.assertEqual(conf["bearer.auth.client.id"], "sa-dtm-ray")
        self.assertEqual(conf["bearer.auth.client.secret"], "s3cret")
        self.assertEqual(conf["bearer.auth.issuer.endpoint.url"], OAUTH_ENV["KAFKA_OAUTH_TOKEN_ENDPOINT_URL"])
        self.assertEqual(conf["bearer.auth.scope"], "profile email")
        self.assertTrue(conf["bearer.auth.logical.cluster"])

    def test_oauth_conf_without_scope_sends_the_empty_scope_the_library_requires(self):
        with env():
            self.assertEqual(schema_registry_conf()["bearer.auth.scope"], "")

    def test_partial_oauth_env_raises(self):
        with mock.patch.dict("os.environ", {"KAFKA_OAUTH_CLIENT_ID": "x"}, clear=True):
            with self.assertRaisesRegex(ValueError, "KAFKA_SECURITY_PROTOCOL"):
                schema_registry_conf()

    def test_client_builds_with_oauth_conf_without_network(self):
        with env():
            client = schema_registry_client()
        self.assertEqual(client._rest_client.base_urls, [schema_registry_url()])

    def test_client_builds_anonymously(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNotNone(schema_registry_client())

    def test_no_inline_client_constructions_remain(self):
        offenders = [str(path) for path in pathlib.Path(dtm.__file__).parent.rglob("*.py")
                     if 'SchemaRegistryClient({"url"' in path.read_text()]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

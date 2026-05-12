import json
import pytest
import boto3
from botocore.exceptions import ClientError
from moto import mock_aws
from credentials import fetch_confluent_credentials


@mock_aws
def test_fetch_returns_kafka_credentials():
    client = boto3.client("secretsmanager", region_name="us-east-1")
    client.create_secret(
        Name="/yt-pipeline/confluent/producer",
        SecretString=json.dumps({
            "kafka_api_key": "test-key",
            "kafka_api_secret": "test-secret",
            "sr_api_key": "sr-test-key",
            "sr_api_secret": "sr-test-secret",
        }),
    )

    creds = fetch_confluent_credentials("/yt-pipeline/confluent/producer", "us-east-1")

    assert creds["kafka_api_key"] == "test-key"
    assert creds["kafka_api_secret"] == "test-secret"
    assert creds["sr_api_key"] == "sr-test-key"
    assert creds["sr_api_secret"] == "sr-test-secret"


@mock_aws
def test_fetch_raises_on_missing_secret():
    with pytest.raises(ClientError):
        fetch_confluent_credentials("/yt-pipeline/confluent/does-not-exist", "us-east-1")

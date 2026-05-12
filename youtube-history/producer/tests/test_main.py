import base64
import json
from unittest.mock import MagicMock, patch, call
import pytest
from main import build_producer_config, produce_events, _resolve_username


def test_resolve_username_cli_wins_over_env():
    assert _resolve_username("@cli", "@env") == "@cli"


def test_resolve_username_env_used_when_no_cli():
    assert _resolve_username(None, "@env") == "@env"


def test_resolve_username_sentinel_when_neither():
    assert _resolve_username(None, None) == "no-username-provided"


def test_resolve_username_strips_whitespace():
    assert _resolve_username("  @osowski  ", None) == "@osowski"


def test_resolve_username_empty_string_becomes_sentinel():
    assert _resolve_username("", "") == "no-username-provided"


FAKE_CREDS = {
    "kafka_api_key": "key123",
    "kafka_api_secret": "secret456",
    "sr_api_key": "sr-key123",
    "sr_api_secret": "sr-secret456",
}
FAKE_CONFIG = {
    "bootstrap_servers": "pkc-xxx.confluent.cloud:9092",
    "schema_registry_url": "https://psrc-xxx.confluent.cloud",
    "input_file": "tests/fixtures/sample_watch_history.html",
}


def test_build_producer_config_sets_sasl_credentials():
    config = build_producer_config("pkc-xxx.confluent.cloud:9092", FAKE_CREDS)
    assert config["sasl.username"] == "key123"
    assert config["sasl.password"] == "secret456"
    assert config["security.protocol"] == "SASL_SSL"
    assert config["sasl.mechanism"] == "PLAIN"
    assert config["enable.idempotence"] is True


def test_produce_events_calls_produce_for_each_valid_entry():
    mock_producer = MagicMock()
    mock_serializer = MagicMock(return_value=b"avro-bytes")

    with patch("main.extract_entries") as mock_extract:
        mock_extract.return_value = [
            {
                "video_id": "dQw4w9WgXcQ",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "title": "Never Gonna Give You Up",
                "channel_name": "Rick Astley",
                "channel_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
                "watched_at": "2024-01-15T10:30:00Z",
            },
            {
                "video_id": "9bZkp7q19f0",
                "url": "https://www.youtube.com/watch?v=9bZkp7q19f0",
                "title": "GANGNAM STYLE",
                "channel_name": "PSY",
                "channel_url": "https://www.youtube.com/channel/UCrDkAvwZum-UTjHmzDI2iIw",
                "watched_at": "2024-02-20T15:45:00Z",
            },
        ]

        produce_events(
            producer=mock_producer,
            avro_serializer=mock_serializer,
            topic="yt.raw.watch.events",
            input_file="fake.html",
            username="@testuser",
            flush_every=500,
        )

    assert mock_producer.produce.call_count == 2
    assert mock_producer.flush.called


def test_produce_events_uses_correct_key():
    mock_producer = MagicMock()
    mock_serializer = MagicMock(return_value=b"avro-bytes")

    with patch("main.extract_entries") as mock_extract:
        mock_extract.return_value = [{
            "video_id": "dQw4w9WgXcQ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "channel_name": "Rick Astley",
            "channel_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
            "watched_at": "2024-01-15T10:30:00Z",
        }]

        produce_events(
            producer=mock_producer,
            avro_serializer=mock_serializer,
            topic="yt.raw.watch.events",
            input_file="fake.html",
            username="@testuser",
            flush_every=500,
        )

    call_kwargs = mock_producer.produce.call_args
    key = call_kwargs.kwargs["key"]
    decoded = base64.b64decode(key).decode()
    assert decoded == "2024-01-15T10:30:00Z-dQw4w9WgXcQ"


def test_produce_events_raises_on_delivery_failure():
    mock_producer = MagicMock()
    mock_serializer = MagicMock(return_value=b"avro-bytes")
    fake_err = MagicMock()

    def fake_produce(**kwargs):
        on_delivery = kwargs.get("on_delivery")
        if on_delivery:
            on_delivery(fake_err, MagicMock())

    mock_producer.produce.side_effect = fake_produce

    with patch("main.extract_entries") as mock_extract:
        mock_extract.return_value = [{
            "video_id": "dQw4w9WgXcQ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "channel_name": "Rick Astley",
            "channel_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
            "watched_at": "2024-01-15T10:30:00Z",
        }]

        with pytest.raises(RuntimeError, match="1 message"):
            produce_events(
                producer=mock_producer,
                avro_serializer=mock_serializer,
                topic="yt.raw.watch.events",
                input_file="fake.html",
                username="@testuser",
                flush_every=500,
            )


def test_produce_events_stamps_username_on_record():
    mock_producer = MagicMock()
    mock_serializer = MagicMock(return_value=b"avro-bytes")

    with patch("main.extract_entries") as mock_extract:
        mock_extract.return_value = [{
            "video_id": "dQw4w9WgXcQ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "channel_name": "Rick Astley",
            "channel_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
            "watched_at": "2024-01-15T10:30:00Z",
        }]

        produce_events(
            producer=mock_producer,
            avro_serializer=mock_serializer,
            topic="yt.raw.watch.events",
            input_file="fake.html",
            username="@testuser",
            flush_every=500,
        )

    record_passed = mock_serializer.call_args_list[0][0][0]
    assert record_passed["username"] == "@testuser"


def test_produce_events_uses_sentinel_when_no_username():
    mock_producer = MagicMock()
    mock_serializer = MagicMock(return_value=b"avro-bytes")

    with patch("main.extract_entries") as mock_extract:
        mock_extract.return_value = [{
            "video_id": "dQw4w9WgXcQ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "channel_name": "Rick Astley",
            "channel_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
            "watched_at": "2024-01-15T10:30:00Z",
        }]

        produce_events(
            producer=mock_producer,
            avro_serializer=mock_serializer,
            topic="yt.raw.watch.events",
            input_file="fake.html",
            username="no-username-provided",
            flush_every=500,
        )

    record_passed = mock_serializer.call_args_list[0][0][0]
    assert record_passed["username"] == "no-username-provided"

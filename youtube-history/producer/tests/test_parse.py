import base64
from pathlib import Path
import pytest
from parse import extract_entries, make_key

FIXTURE = Path(__file__).parent / "fixtures" / "sample_watch_history.html"


def test_extract_returns_only_valid_watch_entries():
    entries = extract_entries(str(FIXTURE))
    assert len(entries) == 3  # dQw4w9WgXcQ, 9bZkp7q19f0, narrowspace1


def test_extract_first_entry_fields():
    entries = extract_entries(str(FIXTURE))
    e = entries[0]
    assert e["video_id"] == "dQw4w9WgXcQ"
    assert e["title"] == "Never Gonna Give You Up"
    assert e["channel_name"] == "Rick Astley"
    assert e["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert e["watched_at"] == "2024-01-15T10:30:00Z"


def test_extract_drops_tv_youtube_entries():
    entries = extract_entries(str(FIXTURE))
    urls = [e["url"] for e in entries]
    assert not any("tv.youtube.com" in url for url in urls)


def test_extract_drops_entries_without_channel():
    entries = extract_entries(str(FIXTURE))
    assert all(e["channel_name"] for e in entries)


def test_make_key_is_deterministic():
    key1 = make_key("2024-01-15T10:30:00Z", "dQw4w9WgXcQ")
    key2 = make_key("2024-01-15T10:30:00Z", "dQw4w9WgXcQ")
    assert key1 == key2


def test_make_key_is_base64():
    key = make_key("2024-01-15T10:30:00Z", "dQw4w9WgXcQ")
    decoded = base64.b64decode(key).decode()
    assert decoded == "2024-01-15T10:30:00Z-dQw4w9WgXcQ"


def test_make_key_differs_for_different_inputs():
    key1 = make_key("2024-01-15T10:30:00Z", "dQw4w9WgXcQ")
    key2 = make_key("2024-01-15T10:30:00Z", "9bZkp7q19f0")
    assert key1 != key2


def test_extract_normalizes_narrow_no_break_space_in_timestamp():
    entries = extract_entries(str(FIXTURE))
    narrow = [e for e in entries if e["video_id"] == "narrowspace1"]
    assert len(narrow) == 1, "entry with U+202F timestamp must be parsed"
    assert "2024-05-10" in narrow[0]["watched_at"]

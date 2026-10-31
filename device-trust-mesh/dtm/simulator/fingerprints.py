"""Device fingerprints and capital-city coordinates for the risk scenarios."""

GEO: dict[str, tuple[float, float]] = {
    "GB": (51.5074, -0.1278),
    "US": (38.9072, -77.0369),
    "DE": (52.5200, 13.4050),
    "FR": (48.8566, 2.3522),
    "CA": (45.4215, -75.6972),
    "AU": (-35.2809, 149.1300),
}

_CURRENT = {"IOS": "18.1", "ANDROID": "15"}
_OUTDATED = {"IOS": "15.7", "ANDROID": "11"}
_KINDS = ("CLEAN", "ROOTED", "OUTDATED")


def fingerprint(platform: str, kind: str) -> dict:
    """Return an Avro ``fingerprint``: ``CLEAN``, ``ROOTED`` (current OS, rooted) or ``OUTDATED``
    (an OS major version below the Flink threshold)."""
    if platform not in _CURRENT:
        raise ValueError(f"unknown platform {platform!r}")
    if kind not in _KINDS:
        raise ValueError(f"unknown fingerprint kind {kind!r}")
    versions = _OUTDATED if kind == "OUTDATED" else _CURRENT
    return {
        "platform": platform,
        "os_version": versions[platform],
        "rooted": kind == "ROOTED",
        "browser_signals": None,
        "network_type": "WIFI",
    }

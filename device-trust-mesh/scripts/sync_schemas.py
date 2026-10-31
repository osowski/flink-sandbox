#!/usr/bin/env python3
"""Vendor the mesh schemas from a GitOps checkout into dtm/schemas/.

Usage: python3 scripts/sync_schemas.py <gitops-checkout> [--check]

Copies <gitops>/workloads/device-trust-mesh/base/schemas/{avro,a2a}/* into
dtm/schemas/ and records the source commit in dtm/schemas/SOURCE. With --check,
nothing is written; the script exits 1 and lists every file that differs.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "dtm" / "schemas"
SUBDIRS = ("avro", "a2a")
SOURCE_RELPATH = Path("workloads/device-trust-mesh/base/schemas")


def source_files(gitops: Path) -> dict[Path, Path]:
    """Map each vendored relative path to its file in the GitOps checkout."""
    root = gitops / SOURCE_RELPATH
    found = {}
    for sub in SUBDIRS:
        for src in sorted((root / sub).glob("*")):
            if src.is_file():
                found[Path(sub) / src.name] = src
    return found


def head_commit(gitops: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(gitops), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def vendored_files() -> set[Path]:
    return {
        p.relative_to(SCHEMA_DIR)
        for sub in SUBDIRS
        for p in (SCHEMA_DIR / sub).glob("*")
        if p.is_file()
    }


def check(sources: dict[Path, Path]) -> list[Path]:
    """Return relative paths that are missing, stale or no longer upstream."""
    differing = []
    for rel, src in sources.items():
        dst = SCHEMA_DIR / rel
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            differing.append(rel)
    differing.extend(vendored_files() - set(sources))
    return sorted(differing)


def sync(gitops: Path, sources: dict[Path, Path]) -> None:
    for sub in SUBDIRS:
        (SCHEMA_DIR / sub).mkdir(parents=True, exist_ok=True)
    for rel in vendored_files() - set(sources):
        (SCHEMA_DIR / rel).unlink()
    for rel, src in sources.items():
        shutil.copyfile(src, SCHEMA_DIR / rel)
    (SCHEMA_DIR / "SOURCE").write_text(head_commit(gitops) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("gitops", type=Path, help="path to a GitOps checkout")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 and list files that differ instead of copying")
    args = parser.parse_args(argv)

    sources = source_files(args.gitops)
    if not sources:
        print(f"no schemas found under {args.gitops / SOURCE_RELPATH}", file=sys.stderr)
        return 2

    if args.check:
        differing = check(sources)
        for rel in differing:
            print(rel)
        return 1 if differing else 0

    sync(args.gitops, sources)
    print(f"synced {len(sources)} files from {head_commit(args.gitops)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

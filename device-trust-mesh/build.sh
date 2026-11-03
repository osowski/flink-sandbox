#!/usr/bin/env bash
# Build all three device-trust-mesh images locally for the host platform.
# Usage: ./build.sh [tag]   (default tag: dev)
set -euo pipefail
cd "$(dirname "$0")"
TAG=${1:-dev}
docker build -f Dockerfile.simulator -t "quay.io/osowski/device-trust-mesh-simulator:$TAG" .
docker build -f Dockerfile.ray -t "quay.io/osowski/device-trust-mesh-ray:$TAG" .
docker build -f Dockerfile.ui -t "quay.io/osowski/device-trust-mesh-ui:$TAG" .

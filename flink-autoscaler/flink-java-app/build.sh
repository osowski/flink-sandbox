#!/bin/bash
set -e

echo "Building Kafka Flink Job..."

# Check if Maven is installed
if ! command -v mvn &> /dev/null; then
    echo "Maven is not installed. Installing via Homebrew..."
    brew install maven
fi

# Build the JAR
mvn clean package

echo ""
echo "Build complete!"
echo "JAR location: target/kafka-flink-job-1.0-SNAPSHOT.jar"

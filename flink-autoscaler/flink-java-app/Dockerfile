#FROM flink:2.2.0
FROM confluentinc/cp-flink:2.1.1-cp1-java11

# Copy the JAR to the Flink image
COPY target/kafka-flink-job-1.0-SNAPSHOT.jar /opt/flink/usrlib/kafka-flink-job.jar

# Ensure /tmp has correct permissions
USER root
RUN chmod 1777 /tmp
USER flink

FROM confluentinc/cp-flink:2.1.1-cp2-java21

USER root
RUN chmod 1777 /tmp
USER flink

COPY yt-enricher/target/yt-enricher-1.0-SNAPSHOT.jar /opt/flink/usrlib/yt-enricher.jar

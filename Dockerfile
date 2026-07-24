FROM eclipse-temurin:25-jre AS java-runtime

FROM python:3.11-trixie

ARG SIGNAL_CLI_VERSION=latest

ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=45 -XX:+ExitOnOutOfMemoryError"

COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    if [ "$SIGNAL_CLI_VERSION" = "latest" ]; then \
        latest_url="$(curl -fsSL -o /dev/null -w "%{url_effective}" https://github.com/AsamK/signal-cli/releases/latest)"; \
        SIGNAL_CLI_VERSION="${latest_url##*/v}"; \
    fi; \
    curl -fsSL \
        "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
        -o /tmp/signal-cli.tar.gz \
    && tar -xzf /tmp/signal-cli.tar.gz -C /opt \
    && ln -sf "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli \
    && rm /tmp/signal-cli.tar.gz
RUN signal-cli --version
    

RUN mkdir /code
WORKDIR /code

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY signal_api.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

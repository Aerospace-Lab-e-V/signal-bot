FROM python:3.11-trixie

ARG SIGNAL_CLI_VERSION=latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    if [ "$SIGNAL_CLI_VERSION" = "latest" ]; then \
        latest_url="$(curl -fsSL -o /dev/null -w "%{url_effective}" https://github.com/AsamK/signal-cli/releases/latest)"; \
        SIGNAL_CLI_VERSION="${latest_url##*/v}"; \
    fi; \
    curl -fsSL \
        "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
        -o /tmp/signal-cli.tar.gz \
    && tar -xzf /tmp/signal-cli.tar.gz -C /opt \
    && ln -sf /opt/signal-cli /usr/local/bin/signal-cli \
    && signal-cli --version \
    && rm /tmp/signal-cli.tar.gz

RUN mkdir /code
WORKDIR /code

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY signal_api.py ./


CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

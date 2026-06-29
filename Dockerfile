FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

# ingress2gateway binary (optional; fallback Python converter is used if absent)
ARG I2GW_VERSION=v1.1.0
ARG TARGETARCH=amd64
RUN case "${TARGETARCH}" in \
        amd64) I2GW_ARCH="x86_64" ;; \
        arm64) I2GW_ARCH="arm64" ;; \
        *) echo "unsupported ingress2gateway arch: ${TARGETARCH}" && exit 1 ;; \
    esac \
    && wget -qO /tmp/i2gw.tar.gz \
        "https://github.com/kubernetes-sigs/ingress2gateway/releases/download/${I2GW_VERSION}/ingress2gateway_Linux_${I2GW_ARCH}.tar.gz" \
    && tar -xzf /tmp/i2gw.tar.gz -C /usr/local/bin ingress2gateway \
    && chmod +x /usr/local/bin/ingress2gateway \
    && rm /tmp/i2gw.tar.gz \
    || echo "ingress2gateway binary unavailable — built-in converter will be used"

WORKDIR /app

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

# MCP stdio transport (default for Claude Desktop / claude-code)
CMD ["python", "src/server.py"]

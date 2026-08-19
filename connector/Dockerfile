# satisfactory connector — read-only client for the FRM mod's JSON API.
FROM python:3.12-slim-bookworm

WORKDIR /app

# Minimal base — this connector only makes outbound HTTP (no SSH, no shell tools).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Non-root — limits blast radius if a tool is abused.
RUN useradd -m -u 1001 mcp
USER mcp

# PORT must match docker-compose.yml and the connector's env.
EXPOSE 8029

# SSE has no plain HTTP /health endpoint (health_check is a TOOL), so verify the
# port is accepting connections instead of curling an endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import os,socket; socket.create_connection(('localhost', int(os.environ.get('PORT','8029'))), timeout=3)" \
    || exit 1

ENTRYPOINT ["python", "server.py"]

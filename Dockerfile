FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install python deps separately so layer caching works during iteration.
COPY indexer/requirements.txt /app/indexer/requirements.txt
COPY ingest/requirements.txt  /app/ingest/requirements.txt
COPY query/requirements.txt   /app/query/requirements.txt
COPY server/requirements.txt  /app/server/requirements.txt
RUN pip install --no-cache-dir \
    -r /app/indexer/requirements.txt \
    -r /app/ingest/requirements.txt \
    -r /app/query/requirements.txt \
    -r /app/server/requirements.txt

COPY indexer /app/indexer
COPY ingest  /app/ingest
COPY query   /app/query
COPY server  /app/server

ENV PYTHONUNBUFFERED=1
ENV INDEX_DB_PATH=/data/index.db
ENV PORT=8080
# NOTE: no `VOLUME ["/data"]` directive — Railway rejects it and manages
# the mount via their own Volumes config (Service → Settings → Volumes,
# mount path /data). Other platforms (fly.io, plain Docker) configure the
# same mount externally too, so the directive isn't load-bearing anywhere.
EXPOSE 8080

# Default: run the FastAPI HTTP server (REST + MCP).
# Override CMD to run the CLI (e.g. `python -m query.ask "..."`) or
# the stdio MCP transport (`python -m server.mcp.stdio`).
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8080}"]

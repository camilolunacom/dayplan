# ZimaBlade is x86-64, but this builds fine on arm64 too.
FROM python:3.13-slim

# uv gives us a fast, reproducible install from the lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    DAYPLAN_DB=/data/dayplan.sqlite \
    DAYPLAN_CONFIG_DIR=/config

WORKDIR /app

# Dependencies first so code edits do not bust the layer cache.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY dayplan ./dayplan
RUN uv sync --frozen --no-dev

# The SQLite file is the only state; mount /data to keep your plan across upgrades.
RUN mkdir -p /data /config
VOLUME ["/data"]

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=4).status == 200 else 1)"

CMD ["dayplan", "serve", "--host", "0.0.0.0", "--port", "8787"]

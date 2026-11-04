# Tag Drifter: one container, one process (web UI + scheduled scans).
#   docker build -t tagdrifter .
#   docker run -p 8080:8080 -v /path/to/music:/music:ro -v tagdrifter-data:/data tagdrifter
#
# Base images are pinned by digest; Dependabot proposes updates (.github/dependabot.yml).

# --- Build: install the locked dependencies and the app into /app/.venv -----------------
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- Runtime -----------------------------------------------------------------------------
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

LABEL org.opencontainers.image.title="Tag Drifter" \
      org.opencontainers.image.description="Monitors a FLAC library for naming and tagging drift" \
      org.opencontainers.image.source="https://github.com/emilystap/tagdrifter" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# The default user; run as another with `user:` in compose (it needs read access to the library).
ARG UID=568
ARG GID=568
RUN groupadd --gid "${GID}" tagdrifter \
    && useradd --uid "${UID}" --gid "${GID}" --no-create-home --shell /usr/sbin/nologin tagdrifter \
    && mkdir -p /data /config \
    && chown "${UID}:${GID}" /data /config

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TAGDRIFTER_LIBRARY=/music \
    TAGDRIFTER_DB=/data/tagdrifter.db \
    TZ=UTC

USER ${UID}:${GID}
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["tagdrifter", "serve", "--host", "0.0.0.0", "--port", "8080"]

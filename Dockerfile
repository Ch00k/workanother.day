FROM ghcr.io/astral-sh/uv:0.11.3-python3.14-trixie-slim

ENV UV_LINK_MODE=copy
WORKDIR /app

# COPY instead of --mount=type=bind so that Docker includes file contents in the
# layer cache key. Bind mounts are not part of the cache key, so changes to
# uv.lock (e.g. a bumped git dependency commit) would not invalidate the layer,
# leaving a stale .venv from a previous build. Found when `manage.py migrate`
# reported "No migrations to apply" despite a new migration in the database
# package -- the container's site-packages had the old package without it.
COPY uv.lock pyproject.toml /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --locked --no-dev

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV PATH=/app/.venv/bin:$PATH

FROM ghcr.io/astral-sh/uv:0.12.5-python3.14-trixie-slim

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

RUN chmod +x /app/run.sh

# The app never needs root, and the volume it writes to is the only thing it must own.
# /app/data is created here so the mount point belongs to the user before the volume is
# attached over it, and the static files are collected at startup into a directory this
# user can write.
RUN useradd --create-home --uid 10001 wad \
    && mkdir -p /app/data /app/staticfiles \
    && chown -R wad:wad /app/data /app/staticfiles

USER wad

CMD ["/app/run.sh"]

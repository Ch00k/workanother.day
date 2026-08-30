FROM ghcr.io/astral-sh/uv:0.12.5-python3.14-trixie-slim

ENV UV_LINK_MODE=copy
WORKDIR /app

# Chromium prints the invoice to PDF, which is what the buyer is sent. The same engine
# already produces that document from the browser's own print command, so there is one
# document printed one way whether it goes out from a screen or by mail.
#
# The fonts are not optional. Nothing here declares a font of its own, so the document asks
# for the default sans-serif stack, and a slim image satisfies none of it; Liberation Sans
# carries the Arial metrics that stack ends in, along with the Polish diacritics.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

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

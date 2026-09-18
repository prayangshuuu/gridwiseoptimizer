FROM python:3.12-slim-bookworm

# Create a non-root user
RUN groupadd -r appuser && useradd -r -m -g appuser appuser

# uv for fast, lockfile-pinned installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies from the lockfile first (better layer caching)
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

# Copy project and finish install
COPY . /app/
RUN uv sync --frozen

# Ensure the entrypoint is executable and dirs exist
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app

# Collect static (no secrets needed; uses the built-in insecure default key)
ENV DJANGO_SETTINGS_MODULE=core.settings
RUN uv run python manage.py collectstatic --noinput

USER appuser

# Documentation only; the platform injects the real PORT at runtime.
EXPOSE 8000

# Secrets are provided via runtime env (-e / --env-file), never baked in.
ENTRYPOINT ["/app/docker-entrypoint.sh"]

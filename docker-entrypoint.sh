#!/bin/sh
# Container entrypoint. Secrets come from the runtime environment only; nothing
# sensitive is baked into the image.
set -e

# Run migrations only when a database is actually configured. /health has no DB
# dependency, so the container still serves it when no DB is present. The
# migrate is best-effort: an unreachable DB must not stop the server booting.
if [ -n "$DATABASE_URL" ]; then
    echo "DATABASE_URL is set; applying migrations..."
    uv run python manage.py migrate --noinput || \
        echo "WARNING: migrate failed; continuing (\/health does not need a DB)."
else
    echo "No DATABASE_URL; skipping migrations."
fi

# Bind to 0.0.0.0 on the platform-provided PORT (default 8000).
exec uv run uvicorn core.asgi:application --host 0.0.0.0 --port "${PORT:-8000}"

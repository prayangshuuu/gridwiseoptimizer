#!/bin/sh
# Container entrypoint. Secrets come from the runtime environment only; nothing
# sensitive is baked into the image.
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec uv run uvicorn core.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT:-8000}"

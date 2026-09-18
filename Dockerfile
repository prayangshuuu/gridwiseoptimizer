FROM python:3.12-slim-bookworm

# Create a non-root user
RUN groupadd -r appuser && useradd -r -m -g appuser appuser

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

# Copy project files
COPY . /app/
RUN uv sync --frozen

RUN mkdir -p /app/staticfiles /app/media && chown -R appuser:appuser /app

# Run collectstatic
ENV DJANGO_SETTINGS_MODULE=core.settings
RUN uv run python manage.py collectstatic --noinput

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uv run uvicorn core.asgi:application --host 0.0.0.0 --port ${PORT:-8000}"]

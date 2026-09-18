#!/bin/sh
set -e
uv run python manage.py migrate
uv run python manage.py seed

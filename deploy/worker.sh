#!/bin/sh
set -e

exec celery -A backend_core worker \
    --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
    --concurrency="${CELERY_CONCURRENCY:-4}"

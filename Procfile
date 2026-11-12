web: gunicorn backend_core.wsgi --workers 3 --threads 4 --timeout 60 --log-file -
worker: celery -A backend_core worker --loglevel=INFO --concurrency=4

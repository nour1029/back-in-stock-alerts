FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data

# SQLite lives here when DATABASE_URL isn't set: a volume keeps it across restarts and re-creates.
VOLUME ["/data"]

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .

USER app
EXPOSE 8000

# Shell form so the migrate step and $PORT expansion work; exec hands PID 1 to gunicorn.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi -c gunicorn.conf.py"]

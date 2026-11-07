"""Gunicorn config. One worker on purpose: the notifier thread lives inside it,
and SQLite prefers a single writer process. Threads handle concurrent requests."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 30
graceful_timeout = 10
accesslog = "-"
errorlog = "-"


def post_worker_init(worker):
    from alerts import notifier

    notifier.start(recover_all=True)


def worker_exit(server, worker):
    from alerts import notifier

    notifier.stop()

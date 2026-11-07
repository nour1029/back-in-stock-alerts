import os
import sys

from django.apps import AppConfig


class AlertsConfig(AppConfig):
    name = "alerts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Under gunicorn the notifier is started from post_worker_init. Under
        # `runserver` start it only in the autoreloader's child process, and
        # never for other management commands (test, migrate, ...).
        if len(sys.argv) > 1 and sys.argv[1] == "runserver" and os.environ.get("RUN_MAIN") == "true":
            from alerts import notifier

            notifier.start(recover_all=True)

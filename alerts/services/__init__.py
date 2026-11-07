from django.utils import timezone


def requeue(queryset) -> int:
    """Put FAILED requests back in the send queue. Returns the number requeued.

    Only FAILED rows move; the notifier re-checks live stock before sending, so a
    retry for an item that has sold out again just goes back to waiting.
    """
    from alerts.models import AlertRequest
    from alerts import notifier

    updated = queryset.filter(status=AlertRequest.Status.FAILED).update(
        status=AlertRequest.Status.WAITING,
        due_at=timezone.now(),
        attempts=0,
        last_error="",
        claimed_at=None,
        updated_at=timezone.now(),
    )
    if updated:
        notifier.wake()
    return updated

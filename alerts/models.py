import uuid

from django.db import models
from django.db.models import Q

OPEN_STATUSES = ("waiting", "sending", "failed")


class StockEvent(models.Model):
    """Durable webhook inbox. `event_id` is unique, so a delivery is recorded at most once."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    class Outcome(models.TextChoices):
        RESTOCKED = "restocked", "Restocked"  # stock API confirmed qty > 0, requests queued
        SOLD_OUT = "sold_out", "Sold out"
        PHANTOM = "phantom", "Phantom"  # webhook said qty > 0, stock API said 0
        NO_CHANGE = "", "—"

    event_id = models.CharField(max_length=128, unique=True)
    sku = models.CharField(max_length=64, db_index=True)
    reported_quantity = models.IntegerField(null=True, blank=True)
    verified_quantity = models.IntegerField(null=True, blank=True)
    deliveries = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    outcome = models.CharField(max_length=16, choices=Outcome, blank=True, default="")
    queued_count = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="event_status_next_idx")]

    def __str__(self) -> str:
        return f"{self.event_id} ({self.sku})"


class AlertRequest(models.Model):
    """A customer's request to be emailed when `sku` is back in stock."""

    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"  # open, not yet sent (may or may not be due)
        SENDING = "sending", "Sending"  # claimed by the worker, send in flight
        NOTIFIED = "notified", "Notified"  # terminal: email accepted by provider
        FAILED = "failed", "Failed"  # gave up after max attempts / permanent error (still "open")

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email = models.EmailField(max_length=254)
    sku = models.CharField(max_length=64, db_index=True)
    product_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.WAITING)
    out_of_stock_seen = models.BooleanField(default=False)
    due_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=128, blank=True)
    triggered_by = models.ForeignKey(
        StockEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests"
    )
    stock_at_signup = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["email", "sku"],
                condition=Q(status__in=OPEN_STATUSES),
                name="one_open_request_per_email_sku",
            )
        ]
        indexes = [
            models.Index(fields=["status", "due_at"], name="req_status_due_idx"),
            models.Index(fields=["sku", "status"], name="req_sku_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.email} → {self.sku} ({self.status})"

    @property
    def idempotency_key(self) -> str:
        return f"back-in-stock/{self.public_id}"

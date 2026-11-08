import threading
from datetime import timedelta
from unittest import mock

import httpx
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from alerts import notifier
from alerts.models import AlertRequest, StockEvent
from alerts.services import requeue, stock
from alerts.tests.fakes import RESEND_URL, STOCK_URL, FakeResend, FakeStockAPI
from alerts.views import admin as admin_views

SECRET = "whsec_test"
W = AlertRequest.Status

TEST_SETTINGS = dict(
    STOCK_API_URL=STOCK_URL,
    STOCK_API_KEY="stock_key",
    RESEND_BASE_URL=RESEND_URL,
    RESEND_API_KEY="re_key",
    WEBHOOK_SECRET=SECRET,
    ADMIN_PASSWORD="admin-pw",
)


class FakesMixin:
    """Swap the HTTP layer for in-process fakes. Any other network call fails the test."""

    def setUp(self):
        super().setUp()
        self.store = FakeStockAPI()
        self.resend = FakeResend()
        settings_override = override_settings(**TEST_SETTINGS)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        patches = [
            mock.patch.object(httpx, "get", self.store.get),
            mock.patch.object(httpx, "post", self.resend.post),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        stock.clear_cache()
        admin_views.reset_throttle()
        self.client = Client()

    # helpers
    def signup(self, email="ada@example.com", sku="KNF-CHF-20", **kwargs):
        return self.client.post(
            "/api/requests", {"email": email, "sku": sku}, content_type="application/json", **kwargs
        )

    def webhook(self, event_id, sku, quantity, secret=SECRET, client=None):
        headers = {} if secret is None else {"HTTP_X_WEBHOOK_SECRET": secret}
        return (client or self.client).post(
            "/webhooks/stock",
            {"event_id": event_id, "type": "stock.updated", "sku": sku, "quantity": quantity,
             "occurred_at": "2026-11-08T00:00:00Z"},
            content_type="application/json",
            **headers,
        )

    def time_passes(self):
        """Make every scheduled retry due now."""
        past = timezone.now() - timedelta(seconds=1)
        AlertRequest.objects.filter(status=W.WAITING, due_at__isnull=False).update(due_at=past)
        StockEvent.objects.filter(next_attempt_at__isnull=False).update(next_attempt_at=past)


class SignupTests(FakesMixin, TestCase):
    def test_creates_request_and_normalises_input(self):
        response = self.signup(email="  Ada@Example.COM ", sku="knf-chf-20")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["status"], "waiting")
        self.assertEqual(body["email"], "ada@example.com")
        self.assertEqual(body["sku"], "KNF-CHF-20")  # catalogue casing
        self.assertIs(body["in_stock"], False)
        self.assertEqual(body["product"]["name"], "Chef's Knife, 20 cm")
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        alert = AlertRequest.objects.get()
        self.assertTrue(alert.out_of_stock_seen)
        self.assertEqual(alert.stock_at_signup, 0)
        self.assertEqual(alert.product_name, "Chef's Knife, 20 cm")

    def test_duplicate_open_request_returns_200_not_created(self):
        first = self.signup()
        second = self.signup(email="ADA@example.com", sku="knf-chf-20")
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(AlertRequest.objects.count(), 1)

    def test_same_email_and_sku_allowed_again_after_notified(self):
        first = self.signup()
        AlertRequest.objects.update(status=W.NOTIFIED, notified_at=timezone.now())
        again = self.signup()
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.json()["id"], first.json()["id"])
        self.assertEqual(AlertRequest.objects.count(), 2)

    def test_failed_request_is_revived_on_resubmit(self):
        self.signup()
        AlertRequest.objects.update(status=W.FAILED, attempts=15, due_at=None)
        again = self.signup()
        self.assertEqual(again.status_code, 200)
        alert = AlertRequest.objects.get()
        self.assertEqual((alert.status, alert.attempts, alert.due_at), (W.WAITING, 0, None))

    def test_custom_sku_keeps_its_case(self):
        response = self.signup(sku="Custom-Sku.1")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["sku"], "Custom-Sku.1")
        self.assertIsNone(response.json()["product"])

    def test_in_stock_signup_is_emailed_right_away(self):
        self.store.stock["KNF-CHF-20"] = 5
        response = self.signup()
        self.assertIs(response.json()["in_stock"], True)
        alert = AlertRequest.objects.get()
        self.assertEqual(alert.stock_at_signup, 5)
        self.assertIsNotNone(alert.due_at)  # queued at sign-up, no restock needed
        notifier.send_due_requests()
        self.assertEqual([e["to"][0] for e in self.resend.delivered], ["ada@example.com"])
        self.assertEqual(AlertRequest.objects.get().status, W.NOTIFIED)

    def test_email_wording_follows_whether_the_item_sold_out(self):
        self.store.stock["KNF-CHF-20"] = 5
        self.signup("now@example.com")  # in stock at sign-up
        notifier.send_due_requests()
        self.store.stock["KNF-CHF-20"] = 0
        self.signup("later@example.com")  # sold out at sign-up, then restocked
        self.store.stock["KNF-CHF-20"] = 5
        notifier.reconcile()
        notifier.send_due_requests()
        subjects = {e["to"][0]: e["subject"] for e in self.resend.delivered}
        self.assertEqual(subjects["now@example.com"], "Chef's Knife, 20 cm is in stock now")
        self.assertEqual(subjects["later@example.com"], "Chef's Knife, 20 cm is back in stock")

    def test_in_stock_duplicate_signup_sends_once(self):
        self.store.stock["KNF-CHF-20"] = 5
        self.signup()
        self.assertEqual(self.signup().status_code, 200)
        notifier.send_due_requests()
        notifier.reconcile()
        notifier.send_due_requests()
        self.assertEqual(len(self.resend.delivered), 1)

    def test_stock_api_down_is_not_fatal(self):
        self.store.down = True
        response = self.signup(sku="Custom-1")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["in_stock"])
        alert = AlertRequest.objects.get()
        self.assertIsNone(alert.stock_at_signup)
        self.assertTrue(alert.out_of_stock_seen)

    def test_validation_errors(self):
        cases = [
            (self.client.post("/api/requests", "{not json", content_type="application/json"), "invalid_json"),
            (self.client.post("/api/requests", "[1]", content_type="application/json"), "invalid_json"),
            (self.signup(email="nope"), "invalid_email"),
            (self.signup(sku="bad sku/x"), "invalid_sku"),
            (self.signup(sku=""), "invalid_sku"),
        ]
        for response, code in cases:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], code)
            self.assertTrue(response.json()["message"])

    def test_form_encoded_fallback_and_preflight(self):
        response = self.client.post("/api/requests", {"email": "f@example.com", "sku": "MUG-STN-SAGE"})
        self.assertEqual(response.status_code, 201)
        preflight = self.client.options("/api/requests")
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight["Access-Control-Allow-Origin"], "*")
        self.assertEqual(self.client.get("/api/requests").status_code, 405)


class WebhookTests(FakesMixin, TestCase):
    def test_wrong_or_missing_secret_is_rejected(self):
        self.assertEqual(self.webhook("e1", "KNF-CHF-20", 5, secret="wrong").status_code, 401)
        self.assertEqual(self.webhook("e1", "KNF-CHF-20", 5, secret=None).status_code, 401)
        self.assertFalse(StockEvent.objects.exists())

    def test_unset_secret_fails_closed(self):
        with override_settings(WEBHOOK_SECRET=""):
            self.assertEqual(self.webhook("e1", "KNF-CHF-20", 5, secret="").status_code, 503)

    def test_bad_payloads(self):
        post = lambda body: self.client.post(  # noqa: E731
            "/webhooks/stock", body, content_type="application/json", HTTP_X_WEBHOOK_SECRET=SECRET
        )
        self.assertEqual(post("nope").status_code, 400)
        self.assertEqual(post({"sku": "KNF-CHF-20"}).status_code, 400)
        self.assertEqual(post({"event_id": "e1", "sku": "../x"}).status_code, 400)
        self.assertEqual(post({"event_id": "e1", "sku": "KNF-CHF-20", "quantity": "lots"}).status_code, 200)
        self.assertIsNone(StockEvent.objects.get().reported_quantity)

    def test_ack_is_immediate_and_work_is_deferred(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 4
        response = self.webhook("e1", "KNF-CHF-20", 4)
        self.assertEqual(response.json(), {"received": True})
        self.assertEqual(StockEvent.objects.get().status, StockEvent.Status.PENDING)
        self.assertEqual(self.resend.requests, [])  # nothing sent inside the request

    def test_duplicate_event_is_processed_once(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 4
        self.assertEqual(self.webhook("e1", "KNF-CHF-20", 4).json(), {"received": True})
        self.assertEqual(self.webhook("e1", "KNF-CHF-20", 4).json(), {"received": True, "duplicate": True})
        notifier.run_once()
        self.assertEqual(self.webhook("e1", "KNF-CHF-20", 4).json()["duplicate"], True)
        notifier.run_once()
        event = StockEvent.objects.get()
        self.assertEqual((event.deliveries, event.outcome, event.queued_count), (3, "restocked", 1))
        self.assertEqual(len(self.resend.delivered), 1)
        self.assertEqual(len(self.resend.requests), 1)

    def test_phantom_restock_sends_nothing(self):
        self.signup()
        self.webhook("e1", "KNF-CHF-20", 12)  # webhook claims 12, stock API says 0
        notifier.run_once()
        event = StockEvent.objects.get()
        self.assertEqual((event.outcome, event.verified_quantity), ("phantom", 0))
        alert = AlertRequest.objects.get()
        self.assertEqual((alert.status, alert.due_at), (W.WAITING, None))
        self.assertEqual(self.resend.requests, [])

    def test_stock_api_failure_keeps_event_pending_for_retry(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 3
        self.webhook("e1", "KNF-CHF-20", 3)
        self.store.down = True
        notifier.process_pending_events()
        event = StockEvent.objects.get()
        self.assertEqual((event.status, event.attempts), (StockEvent.Status.PENDING, 1))
        self.assertIsNotNone(event.next_attempt_at)
        self.store.down = False
        self.time_passes()
        notifier.run_once()
        self.assertEqual(len(self.resend.delivered), 1)


class SendingTests(FakesMixin, TestCase):
    def restock(self, sku="KNF-CHF-20", quantity=6, event_id="e1"):
        self.store.stock[sku] = quantity
        self.webhook(event_id, sku, quantity)
        notifier.run_once()

    def test_restock_notifies_once_with_verified_stock(self):
        self.signup("a@example.com")
        self.signup("b@example.com")
        self.signup("c@example.com", sku="MUG-STN-SAGE")
        self.restock()
        sent = sorted(e["to"][0] for e in self.resend.delivered)
        self.assertEqual(sent, ["a@example.com", "b@example.com"])
        email = self.resend.delivered[0]
        self.assertEqual(email["subject"], "Chef's Knife, 20 cm is back in stock")
        self.assertTrue(email["html"] and email["text"] and email["from"])
        alert = AlertRequest.objects.get(email="a@example.com")
        self.assertEqual(alert.status, W.NOTIFIED)
        self.assertIsNotNone(alert.notified_at)
        self.assertEqual(alert.triggered_by.event_id, "e1")
        self.assertEqual(email["key"], alert.idempotency_key)
        # Later events don't notify the same people again.
        self.restock(event_id="e2")
        self.assertEqual(len(self.resend.requests), 2)

    def test_provider_500_is_retried_with_same_key_then_notified_once(self):
        self.signup()
        self.resend.fail_next = 2
        self.restock()
        alert = AlertRequest.objects.get()
        self.assertEqual((alert.status, alert.attempts), (W.WAITING, 1))
        self.assertIn("HTTP 500", alert.last_error)
        self.time_passes()
        notifier.run_once()
        self.time_passes()
        notifier.run_once()
        alert.refresh_from_db()
        self.assertEqual(alert.status, W.NOTIFIED)
        self.assertEqual(alert.attempts, 3)
        keys = {r["key"] for r in self.resend.requests}
        self.assertEqual(keys, {alert.idempotency_key})
        self.assertEqual(len(self.resend.requests), 3)
        self.assertEqual(len(self.resend.delivered), 1)
        # Nothing further happens.
        self.time_passes()
        notifier.run_once()
        self.assertEqual(len(self.resend.requests), 3)

    def test_lost_response_is_deduplicated_by_idempotency_key(self):
        self.signup()
        self.resend.timeout_after_accept = 1  # provider accepted, we never heard back
        self.restock()
        self.assertEqual(AlertRequest.objects.get().status, W.WAITING)
        self.time_passes()
        notifier.run_once()
        self.assertEqual(AlertRequest.objects.get().status, W.NOTIFIED)
        self.assertEqual(len(self.resend.delivered), 1)
        self.assertEqual(self.resend.deduped, 1)

    def test_permanent_error_marks_failed_and_requeue_retries(self):
        self.signup()
        self.resend.fail_next, self.resend.status_for_failures = 1, 422
        self.restock()
        alert = AlertRequest.objects.get()
        self.assertEqual(alert.status, W.FAILED)
        self.assertEqual(requeue(AlertRequest.objects.exclude(status=W.FAILED)), 0)
        self.assertEqual(requeue(AlertRequest.objects.all()), 1)
        notifier.run_once()
        alert.refresh_from_db()
        self.assertEqual(alert.status, W.NOTIFIED)
        self.assertEqual(len(self.resend.delivered), 1)

    def test_gives_up_after_max_attempts(self):
        self.signup()
        self.resend.fail_next = 100
        self.restock()
        for _ in range(notifier.MAX_SEND_ATTEMPTS):
            self.time_passes()
            notifier.run_once()
        alert = AlertRequest.objects.get()
        self.assertEqual((alert.status, alert.attempts), (W.FAILED, notifier.MAX_SEND_ATTEMPTS))

    def test_sold_out_again_before_send_unqueues(self):
        self.signup()
        self.resend.fail_next = 1
        self.restock()  # first attempt fails, retry scheduled
        self.store.stock["KNF-CHF-20"] = 0  # sells out before the retry
        self.time_passes()
        notifier.send_due_requests()
        alert = AlertRequest.objects.get()
        self.assertEqual((alert.status, alert.due_at), (W.WAITING, None))
        self.assertEqual(len(self.resend.delivered), 0)
        self.restock(event_id="e2")  # comes back: now they hear, once
        self.assertEqual(len(self.resend.delivered), 1)

    def test_stock_recheck_failure_never_sends_blind(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 2
        self.webhook("e1", "KNF-CHF-20", 2)
        notifier.process_pending_events()
        self.store.down = True
        notifier.send_due_requests()
        alert = AlertRequest.objects.get()
        self.assertEqual(alert.status, W.WAITING)
        self.assertGreater(alert.due_at, timezone.now())
        self.assertEqual(self.resend.requests, [])

    def test_sell_out_webhook_clears_queue(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 2
        self.webhook("e1", "KNF-CHF-20", 2)
        self.store.stock["KNF-CHF-20"] = 0
        self.webhook("e2", "KNF-CHF-20", 0)
        notifier.run_once()
        outcomes = dict(StockEvent.objects.values_list("event_id", "outcome"))
        # By the time e1 is verified the stock API says 0, so its "2" was a phantom.
        self.assertEqual(outcomes, {"e1": "phantom", "e2": "sold_out"})
        self.assertEqual(self.resend.requests, [])


class RecoveryTests(FakesMixin, TestCase):
    def test_stale_sending_claim_is_recovered_and_sent_once(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 3
        # Simulate a crash mid-send: claimed long ago, provider already accepted it.
        alert = AlertRequest.objects.get()
        self.resend.accepted[alert.idempotency_key] = {"id": "msg_0", "to": [alert.email], "key": "x"}
        AlertRequest.objects.update(status=W.SENDING, claimed_at=timezone.now() - timedelta(minutes=5),
                                    due_at=timezone.now())
        self.assertEqual(notifier.recover_stale_claims(), 1)
        notifier.send_due_requests()
        alert.refresh_from_db()
        self.assertEqual(alert.status, W.NOTIFIED)
        self.assertEqual(alert.provider_message_id, "msg_0")
        self.assertEqual(len(self.resend.delivered), 1)
        self.assertEqual(self.resend.deduped, 1)

    def test_fresh_claim_left_alone_except_on_startup(self):
        self.signup()
        AlertRequest.objects.update(status=W.SENDING, claimed_at=timezone.now())
        self.assertEqual(notifier.recover_stale_claims(), 0)
        self.assertEqual(notifier.recover_stale_claims(recover_all=True), 1)
        self.assertEqual(AlertRequest.objects.get().status, W.WAITING)

    def test_reconciliation_emails_every_waiting_request_with_stock(self):
        self.signup("missed@example.com", sku="MUG-STN-SAGE")  # sold out at sign-up
        self.store.stock["KNF-CHF-20"] = 5
        self.signup("instock@example.com")  # in stock at sign-up...
        AlertRequest.objects.filter(email="instock@example.com").update(due_at=None)  # ...but that send was lost
        self.store.stock["MUG-STN-SAGE"] = 7  # restock whose webhook never arrives
        notifier.reconcile()
        notifier.send_due_requests()
        self.assertEqual(
            sorted(e["to"][0] for e in self.resend.delivered), ["instock@example.com", "missed@example.com"]
        )
        notifier.reconcile()  # running again sends nothing new
        notifier.send_due_requests()
        self.assertEqual(len(self.resend.delivered), 2)

    def test_reconciliation_leaves_sold_out_requests_waiting(self):
        self.signup()
        notifier.reconcile()
        notifier.send_due_requests()
        self.assertEqual(self.resend.delivered, [])
        self.assertIsNone(AlertRequest.objects.get().due_at)

    def test_reconcile_repairs_sku_casing_from_catalogue_outage(self):
        self.store.down = True
        self.signup("ada@example.com", sku="knf-chf-20")
        self.assertEqual(AlertRequest.objects.get().sku, "knf-chf-20")
        self.store.down = False
        stock.clear_cache()
        self.signup("ada@example.com", sku="KNF-CHF-20")  # second spelling while recovering
        self.assertEqual(AlertRequest.objects.count(), 2)
        self.store.stock["KNF-CHF-20"] = 4
        notifier.reconcile()
        notifier.send_due_requests()
        self.assertEqual(AlertRequest.objects.filter(status__in=[W.WAITING, W.NOTIFIED]).count(), 1)
        self.assertEqual(len(self.resend.delivered), 1)


class ConcurrencyTests(FakesMixin, TransactionTestCase):
    def test_simultaneous_duplicate_deliveries(self):
        self.signup()
        self.store.stock["KNF-CHF-20"] = 4
        barrier = threading.Barrier(4)
        results = []

        def deliver():
            try:
                barrier.wait()
                results.append(self.webhook("evt_same", "KNF-CHF-20", 4, client=Client()).json())
            finally:
                connection.close()

        threads = [threading.Thread(target=deliver) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(1 for r in results if r.get("duplicate")), 3)
        notifier.run_once()
        event = StockEvent.objects.get()
        self.assertEqual(event.deliveries, 4)
        self.assertEqual(len(self.resend.requests), 1)

    def test_simultaneous_duplicate_signups(self):
        barrier = threading.Barrier(4)
        codes = []

        def submit():
            try:
                barrier.wait()
                response = Client().post(
                    "/api/requests", {"email": "race@example.com", "sku": "KNF-CHF-20"},
                    content_type="application/json",
                )
                codes.append(response.status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(codes), [200, 200, 200, 201])
        self.assertEqual(AlertRequest.objects.count(), 1)


class PageAndAdminTests(FakesMixin, TestCase):
    def test_embed_is_frameable(self):
        response = self.client.get("/embed?sku=knf-chf-20&accent=FF8800")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Frame-Options", response)
        self.assertEqual(response["Content-Security-Policy"], "frame-ancestors *")
        self.assertEqual(response.context["sku"], "KNF-CHF-20")
        self.assertEqual(response.context["accent"], "#ff8800")
        self.assertEqual(response.context["product"]["name"], "Chef's Knife, 20 cm")
        self.assertIsNone(self.client.get("/embed?accent=red").context["accent"])

    def test_home_and_health(self):
        self.assertEqual(len(self.client.get("/").context["products"]), 3)
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})

    def test_admin_requires_password(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertIn("no-store", response["Cache-Control"])
        bad = self.client.post("/admin/login", {"password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        self.assertTrue(bad.context["error"])
        good = self.client.post("/admin/login", {"password": "admin-pw", "next": "/admin?status=failed"})
        self.assertEqual((good.status_code, good["Location"]), (302, "/admin?status=failed"))
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/admin/export.csv").status_code, 200)

    def test_admin_refuses_login_without_password_configured(self):
        with override_settings(ADMIN_PASSWORD=""):
            self.assertEqual(self.client.post("/admin/login", {"password": ""}).status_code, 401)

    def test_login_throttle(self):
        for _ in range(admin_views.THROTTLE_MAX_FAILURES):
            self.client.post("/admin/login", {"password": "wrong"})
        self.assertEqual(self.client.post("/admin/login", {"password": "admin-pw"}).status_code, 429)

    def test_dashboard_counts_match_filters(self):
        self.client.post("/admin/login", {"password": "admin-pw"})
        self.signup("a@example.com")
        self.signup("b@example.com")
        AlertRequest.objects.filter(email="b@example.com").update(due_at=timezone.now())
        response = self.client.get("/admin?status=queued")
        self.assertEqual(response.context["counts"]["waiting"], 1)
        self.assertEqual(response.context["counts"]["queued"], 1)
        self.assertEqual([r.email for r in response.context["page_obj"]], ["b@example.com"])
        self.assertEqual(response.context["page_obj"][0].product["name"], "Chef's Knife, 20 cm")

    def test_email_preview_requires_admin(self):
        response = self.client.get("/admin/email-preview")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.context["next"], "/admin/email-preview")

    def test_email_preview_renders_real_email_without_sending(self):
        self.client.post("/admin/login", {"password": "admin-pw"})
        response = self.client.get("/admin/email-preview?sku=knf-chf-20&qty=3")
        self.assertEqual(response.status_code, 200)
        payload = response.context["payload"]
        self.assertEqual(payload["subject"], "Chef's Knife, 20 cm is back in stock")
        self.assertIn("Only 3 left", payload["html"])
        self.assertIn("Only 3 left", payload["text"])
        self.assertEqual(self.resend.requests, [])
        # The whole email must reach the iframe: escaped once inside srcdoc="…", never cut at its first quote.
        self.assertContains(response, 'srcdoc="&lt;!doctype html&gt;')
        self.assertContains(response, "Only 3 left")
        in_stock = self.client.get("/admin/email-preview?sku=knf-chf-20&signed_up=in_stock")
        self.assertEqual(in_stock.context["payload"]["subject"], "Chef's Knife, 20 cm is in stock now")

    def test_admin_retry(self):
        self.client.post("/admin/login", {"password": "admin-pw"})
        self.signup()
        alert = AlertRequest.objects.get()
        AlertRequest.objects.update(status=W.FAILED)
        response = self.client.post(f"/admin/requests/{alert.public_id}/retry", {"next": "/admin?status=failed"})
        self.assertEqual(response["Location"], "/admin?status=failed")
        alert.refresh_from_db()
        self.assertEqual(alert.status, W.WAITING)
        self.assertIsNotNone(alert.due_at)

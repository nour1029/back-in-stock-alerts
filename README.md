# Back-in-Stock alerts

Customers leave their email on a sold-out product and get **one** email when it really comes back.
Django 5.2 + SQLite (WAL) + a background notifier thread, packaged as a single Docker container.

| Route | What it is |
|---|---|
| `GET /embed?sku=…&accent=#rrggbb` | Email + SKU form, made to sit in an iframe on any store (`frame-ancestors *`) |
| `POST /api/requests` | `{ "email", "sku" }` (JSON or form). `201` new, `200` already on the list, `400` with `error` + `message` |
| `POST /webhooks/stock` | Stock webhooks, authenticated by `X-Webhook-Secret` |
| `GET /admin` | Password-protected dashboard: every request, its status, when it was notified, recent stock events, CSV export |
| `GET /` | Demo storefront that embeds the form |
| `GET /healthz` | `{"ok": true}` |

## Environment

Required:

| Variable | |
|---|---|
| `STOCK_API_URL` | `GET {url}/{sku}` → `{quantity, in_stock, …}`, `GET {url}` → catalogue |
| `STOCK_API_KEY` | Bearer token for the stock API |
| `RESEND_API_KEY` | Bearer token for the email API |
| `RESEND_BASE_URL` | Emails are POSTed to `{RESEND_BASE_URL}/emails` in Resend's format |
| `WEBHOOK_SECRET` | Expected `X-Webhook-Secret`. If unset, webhooks are refused (503) |

Optional:

| Variable | Default |
|---|---|
| `ADMIN_PASSWORD` | **Defaults to `WEBHOOK_SECRET`.** If neither is set, every admin login is refused |
| `PORT` | `8000` |
| `STORE_NAME` | `Common Goods` |
| `EMAIL_FROM` | `"{STORE_NAME} <alerts@example.com>"` |
| `PRODUCT_URL_TEMPLATE` | `https://example.com/products/{sku}` (the "Shop now" link in the email) |
| `DATA_DIR` | `./data` locally, `/data` in Docker (holds the SQLite database) |
| `ALLOWED_HOSTS` | `*` |
| `CSRF_TRUSTED_ORIGINS` | ngrok and Railway domains (comma-separated `https://…` origins) |
| `SECRET_KEY` | Derived from the secrets above when unset |

## Run it

With Docker:

```sh
docker build -t back-in-stock .
docker run --env-file .env -p 8000:8000 -v bis-data:/data back-in-stock
```

The container runs migrations and then serves on `$PORT`. The `-v bis-data:/data` volume keeps the
database across container restarts and re-creations. Without it, data survives `docker restart` but not
`docker rm`.

Locally (Python 3.12):

```sh
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python manage.py migrate
PORT=8000 .venv/bin/gunicorn config.wsgi -c gunicorn.conf.py   # or: manage.py runserver
.venv/bin/python manage.py test alerts
```

A `.env` file in the project root is loaded for local development. Real environment variables take precedence.

## How duplicate and missed emails are prevented

Every guarantee lives in the database, so it holds across threads, retries and restarts.

**No duplicates**

- **Unique event inbox.** `StockEvent.event_id` is unique. The webhook inserts the event, and a second
  delivery (a retry, or the same event sent twice at the same moment) hits the constraint. That delivery
  is only counted (`deliveries + 1`) and acknowledged with `{"duplicate": true}`. It never runs the work twice.
- **One open request per person and product.** A partial unique index on `(email, sku)` covers the open
  statuses (`waiting`, `sending`, `failed`). Signing up again returns the existing request with `200`.
  Once someone has been notified they can sign up again for a future restock. Emails are lower-cased, and
  SKUs are matched to the catalogue's casing, so `knf-chf-20` and `KNF-CHF-20` count as the same product.
- **Atomic claim.** A request is sent only after a conditional `UPDATE … SET status='sending' WHERE
  status='waiting'` affects exactly one row. Nothing else can claim it at the same time.
- **Idempotency key.** Every send carries `Idempotency-Key: back-in-stock/<request uuid>`, and retries reuse
  it. If the provider accepted an email but we never saw the reply (a timeout, or a crash mid-send), the
  retry is deduplicated by the provider instead of reaching the customer twice. (Resend keeps keys for 24 h.)

**No false alarms**

- **The webhook is only a hint.** Its `quantity` is stored but never trusted. The worker asks the stock API.
  A webhook that says "12 in stock" while the API says 0 is recorded as a *phantom* and sends nothing.
- **Re-check before sending.** Stock is checked again just before each send, because retries and backoff
  leave time for an item to sell out. If it has sold out, the request goes back to waiting for the next
  restock. If the stock API can't be reached, the send is postponed. It never sends without a fresh confirmation.
- **Signed up while in stock.** These customers aren't emailed immediately. They hear about it only after
  the item has been seen out of stock and has come back.

**No missed emails**

- **Ack fast, work durably.** The webhook stores the event and answers `200` in milliseconds. A background
  worker does the slow part (stock API, email). A crash after the ack loses nothing, because the event is
  already in the database as `pending`.
- **Retries with backoff.** A 5xx, 429, 409, 401/403 or network error schedules a retry after 3 s, 10 s,
  30 s, 1 m, 2 m, 5 m, 10 m, then every 30 m. The request is marked `failed` after 15 attempts. A 400/422
  is permanent and marked `failed` at once. A failed request can be retried from the admin ("Retry now"),
  or it revives when the customer signs up again. If the stock API fails while an event is being
  processed, that event is retried the same way.
- **Stale-claim recovery.** A request left in `sending` (the process died mid-send) goes back in the queue:
  immediately on startup, or after 2 minutes otherwise. This is safe because of the idempotency key.
- **Reconciliation.** Every 60 s (and once at startup) the worker compares the open requests with the live
  catalogue, plus per-SKU lookups for custom SKUs. This catches restocks whose webhooks were lost or arrived
  while the app was down.
- **Persistence.** Requests, events, attempts and schedules all live in SQLite (WAL mode, `/data` volume).
  After a restart the worker carries on from exactly where it stopped.

Gunicorn runs one worker with 8 threads. That keeps a single SQLite writer process and a single notifier
thread, while the threads serve requests in parallel. Write transactions use `BEGIN IMMEDIATE` with a 20 s
busy timeout, so concurrent writers queue instead of failing.

## Layout

```
config/            settings, urls, wsgi
alerts/models.py   AlertRequest, StockEvent
alerts/views/      api.py (signup + webhook), pages.py (embed, home), admin.py
alerts/services/   stock.py (stock API + catalogue cache), email.py (render + send), requeue()
alerts/notifier.py background worker
alerts/tests/      flow tests with fake stock and Resend APIs
```

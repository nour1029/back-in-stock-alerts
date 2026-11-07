from django.urls import path

from alerts.views import admin, api, pages

urlpatterns = [
    path("", pages.home, name="home"),
    path("embed", pages.embed, name="embed"),
    path("healthz", pages.healthz, name="healthz"),
    path("api/requests", api.create_request, name="create_request"),
    path("webhooks/stock", api.stock_webhook, name="stock_webhook"),
    path("admin", admin.dashboard, name="admin"),
    path("admin/", admin.dashboard),
    path("admin/login", admin.login, name="admin_login"),
    path("admin/logout", admin.logout, name="admin_logout"),
    path("admin/requests/<uuid:public_id>/retry", admin.retry, name="admin_retry"),
    path("admin/export.csv", admin.export_csv, name="admin_export"),
]

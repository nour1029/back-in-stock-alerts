"""Never cache anything under /admin, including responses produced before a view runs."""

from django.utils.cache import add_never_cache_headers


class AdminNeverCacheMiddleware:
    """Same headers as @never_cache (Cache-Control: max-age=0, no-cache, no-store,
    must-revalidate, private), applied to every /admin response.

    The admin views are decorated too; this also covers responses the views never see,
    such as CSRF rejections (403) from CsrfViewMiddleware and 404s under /admin/.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path == "/admin" or request.path.startswith("/admin/"):
            add_never_cache_headers(response)
        return response

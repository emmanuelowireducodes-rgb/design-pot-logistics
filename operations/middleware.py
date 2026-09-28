import secrets


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(24)
        response = self.get_response(request)
        # allauth's POST login redirects the form navigation to Google OAuth.
        form_action = "'self'"
        if request.path_info in {"/login/", "/accounts/google/login/"}:
            form_action += " https://accounts.google.com"
        response["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            f"form-action {form_action}"
        )
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.user.is_authenticated:
            response["Cache-Control"] = "private, no-store"
        return response

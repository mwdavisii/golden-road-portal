"""Security headers on every response. No inline script or style anywhere, so the CSP needs no 'unsafe-inline'."""

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
HEADERS = [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
           (b"x-frame-options", b"DENY"), (b"strict-transport-security", b"max-age=31536000")]
NAMES = {name for name, _ in HEADERS} | {b"cache-control"}


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        static = scope["path"].startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in NAMES]
                message = {**message, "headers": headers + HEADERS + ([] if static else [(b"cache-control", b"no-store")])}
            await send(message)

        await self.app(scope, receive, send_with_headers)

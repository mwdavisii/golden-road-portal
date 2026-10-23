"""The portal's cookies. __Host- names make the browser insist on Secure, Path=/ and no Domain."""

SESSION_COOKIE = "__Host-portal-session"
LOGIN_COOKIE = "__Host-portal-login"
LINK_COOKIE = "__Host-portal-link"


def set_cookie(response, name: str, value: str, max_age_seconds: float) -> None:
    response.set_cookie(name, value, max_age=int(max_age_seconds), path="/", secure=True, httponly=True, samesite="lax")


def clear_cookie(response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")

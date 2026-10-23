"""Linking a GitHub login (source github-oauth, P11): the person authorizes the read App once. The user token proves the
login, is revoked straight away, and is never stored or logged."""

import re
from urllib.parse import urlencode

import httpx

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


class LinkError(Exception):
    pass


def authorize_url(settings, state: str) -> str:
    query = {"client_id": settings.github_oauth_client_id, "redirect_uri": settings.base_url + "/link/github/callback", "state": state,
             "allow_signup": "false"}
    return f"{settings.github_web}/login/oauth/authorize?{urlencode(query)}"


def identify(http: httpx.Client, settings, code: str) -> tuple[str, int]:
    token = None
    try:
        response = http.post(f"{settings.github_web}/login/oauth/access_token", headers={"Accept": "application/json"}, timeout=10,
                             data={"client_id": settings.github_oauth_client_id, "client_secret": settings.github_oauth_client_secret,
                                   "code": code, "redirect_uri": settings.base_url + "/link/github/callback"})
        token = response.json().get("access_token") if response.status_code == 200 else None
        if not isinstance(token, str) or not token:
            raise LinkError("GitHub did not issue a token")
        user = http.get(f"{settings.github_api}/user", timeout=10,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        doc = user.json() if user.status_code == 200 else {}
        login, uid = doc.get("login"), doc.get("id")
        if not isinstance(login, str) or not LOGIN.fullmatch(login) or type(uid) is not int:
            raise LinkError("GitHub did not say who signed in")
        return login, uid
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise LinkError("GitHub can't be reached") from exc
    finally:
        if isinstance(token, str) and token:
            try:
                http.request("DELETE", f"{settings.github_api}/applications/{settings.github_oauth_client_id}/token", timeout=10,
                             auth=(settings.github_oauth_client_id, settings.github_oauth_client_secret), json={"access_token": token})
            except httpx.HTTPError:
                pass

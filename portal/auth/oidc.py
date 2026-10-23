"""OIDC authorization code flow with PKCE (P15). The provider is configuration: Authentik in the PoC, Entra ID later."""

import base64
import dataclasses as dc
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
import jwt


class OIDCError(Exception):
    """Sign-in failed. The message is ours, never the provider's text."""


@dc.dataclass(frozen=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def _https(value) -> bool:
    return isinstance(value, str) and value.startswith("https://")


def discover(http: httpx.Client, issuer: str) -> Provider:
    try:
        response = http.get(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10)
        doc = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("the sign-in provider's discovery document can't be read") from exc
    if not isinstance(doc, dict) or doc.get("issuer") != issuer:
        raise OIDCError("the sign-in provider's discovery document names another issuer")
    fields = [doc.get(k) for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")]
    if not all(_https(f) for f in fields):
        raise OIDCError("the sign-in provider's endpoints must be https")
    return Provider(issuer, *fields)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(provider, client_id, redirect_uri, scopes, state, nonce, challenge) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scopes, "state": state,
             "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    return provider.authorization_endpoint + ("&" if "?" in provider.authorization_endpoint else "?") + urlencode(query)


def exchange(http, provider, client_id, client_secret, code, redirect_uri, verifier) -> str:
    try:
        response = http.post(provider.token_endpoint, auth=(client_id, client_secret), timeout=10,
                             data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier})
        token = response.json().get("id_token") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise OIDCError("the sign-in provider refused the code") from exc
    if not isinstance(token, str) or not token:
        raise OIDCError("the sign-in provider refused the code")
    return token


def verify_id_token(http, provider, token, *, client_id, nonce, algorithms) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise OIDCError("the id token is malformed") from exc
    alg = header.get("alg")
    if alg not in algorithms:
        raise OIDCError("the id token's algorithm is not allowed")
    try:
        keys = jwt.PyJWKSet.from_dict(http.get(provider.jwks_uri, timeout=10).json())
        key = next(k for k in keys.keys if k.key_id == header.get("kid"))
        claims = jwt.decode(token, key.key, algorithms=[alg], audience=client_id, issuer=provider.issuer, leeway=60,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except (StopIteration, jwt.InvalidTokenError, jwt.PyJWKSetError, httpx.HTTPError, ValueError) as exc:
        raise OIDCError("the id token did not verify") from exc
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
        raise OIDCError("the id token's nonce doesn't match")
    aud = claims["aud"]
    # azp, when the provider sends it, names the client the token was issued to: it must be us. With several audiences it is required.
    if ("azp" in claims or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != client_id:
        raise OIDCError("the id token was issued to another client")
    return claims


def groups_from(claims: dict, claim: str) -> tuple[str, ...]:
    """Groups from the id token. Anything but a list of strings counts as no groups, which grants no role (fail toward less)."""
    value = claims.get(claim)
    return tuple(g for g in value if isinstance(g, str)) if isinstance(value, list) else ()


def display_name(claims: dict) -> str:
    for key in ("name", "preferred_username", "email", "sub"):
        if isinstance(claims.get(key), str) and claims[key].strip():
            return claims[key].strip()[:200]
    return "unknown"

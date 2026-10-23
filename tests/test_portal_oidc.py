import base64
import hashlib

import httpx
import pytest
from portal_fakes import ISSUER, FakeIdP, new_rsa_key, transport

from portal.auth import oidc


@pytest.fixture
def idp():
    return FakeIdP(ISSUER)


@pytest.fixture
def http(idp):
    return httpx.Client(transport=transport(idp))


def test_discovery_must_name_the_configured_issuer(idp, http):
    provider = oidc.discover(http, ISSUER)
    assert provider.token_endpoint.startswith("https://sso.example.test/")
    with pytest.raises(oidc.OIDCError):
        oidc.discover(http, ISSUER.replace("portal", "other"))


def verify(idp, http, token, nonce="n-1", algorithms=frozenset({"RS256"})):
    return oidc.verify_id_token(http, oidc.discover(http, ISSUER), token, client_id="portal-client", nonce=nonce, algorithms=algorithms)


def test_a_good_id_token_gives_its_claims(idp, http):
    claims = verify(idp, http, idp.id_token({"sub": "s-1", "nonce": "n-1", "groups": ["g"]}))
    assert claims["sub"] == "s-1" and oidc.groups_from(claims, "groups") == ("g",)


@pytest.mark.parametrize("make", [
    lambda idp: idp.id_token({"sub": "s", "nonce": "other"}),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "aud": "someone-else"}),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "iss": "https://evil.example/"}),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "exp": 1}),
    lambda idp: idp.id_token({"nonce": "n-1"}),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1"}, alg="HS256", key=b"oidc-secret-for-tests"),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1"}, alg="none", key=None),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1"}, key=new_rsa_key()),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1"}, kid="unknown"),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "aud": ["portal-client", "other"], "azp": "other"}),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "azp": "other"}),  # aud is ours alone, but the token says another client asked for it
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "azp": ""}),
    lambda idp: idp.id_token({"sub": "s", "nonce": "n-1", "azp": 5}),
    lambda idp: "not.a.token",
])
def test_bad_id_tokens_are_refused(idp, http, make):
    with pytest.raises(oidc.OIDCError):
        verify(idp, http, make(idp))


def test_groups_that_are_not_a_list_of_strings_give_no_groups():
    assert oidc.groups_from({"groups": "admins"}, "groups") == ()
    assert oidc.groups_from({"groups": ["a", 1, None]}, "groups") == ("a",)
    assert oidc.groups_from({}, "groups") == ()


def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = oidc.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_the_authorization_url_carries_state_nonce_and_an_s256_challenge(idp, http):
    from urllib.parse import parse_qs, urlsplit
    provider = oidc.discover(http, ISSUER)
    url = oidc.authorize_url(provider, "portal-client", "https://portal.example.test/auth/callback", "openid profile email", "st", "no", "ch")
    q = parse_qs(urlsplit(url).query)
    assert (q["state"], q["nonce"], q["code_challenge"], q["code_challenge_method"], q["response_type"]) == (["st"], ["no"], ["ch"], ["S256"], ["code"])


def test_an_azp_that_is_our_client_is_fine(idp, http):
    assert verify(idp, http, idp.id_token({"sub": "s", "nonce": "n-1", "azp": "portal-client"}))["sub"] == "s"
    assert verify(idp, http, idp.id_token({"sub": "s", "nonce": "n-1", "aud": ["portal-client", "other"], "azp": "portal-client"}))["sub"] == "s"

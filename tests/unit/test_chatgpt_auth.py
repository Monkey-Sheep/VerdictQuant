"""Security boundaries for the direct ChatGPT OAuth public client."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pa_agent.chatgpt import oauth
from pa_agent.chatgpt.client import ChatGPTClient, ChatGPTError
from pa_agent.security.secret_store import MemorySecretStore


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_jwks(key):
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kid": "test-key",
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": oauth.b64encode(numbers.n.to_bytes(256, "big")),
                "e": oauth.b64encode(numbers.e.to_bytes(3, "big")),
            }
        ]
    }


def sign_claims(key, changes=None, header=None):
    claims = {
        "iss": oauth.ISSUER,
        "sub": "subject-1",
        "aud": "oaiapp_test",
        "exp": time.time() + 3600,
        "iat": time.time(),
        "nonce": "fresh-nonce",
        "email": "same@example.test",
    }
    claims.update(changes or {})
    encoded_header = oauth.b64encode(
        json.dumps(header or {"alg": "RS256", "kid": "test-key"}).encode()
    )
    encoded_claims = oauth.b64encode(json.dumps(claims).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode() + "." + oauth.b64encode(signature)


def test_valid_signature_and_identity(signing_key):
    token = sign_claims(signing_key)
    result = oauth.validate_id_token(token, make_jwks(signing_key), "oaiapp_test", "fresh-nonce")
    assert result["sub"] == "subject-1"


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://evil.example"},
        {"aud": "different-app"},
        {"sub": ""},
        {"nonce": "other-nonce"},
        {"exp": time.time() - 10},
        {"iat": time.time() + 3600},
        {"iat": True},
        {"exp": float("nan")},
        {"nbf": time.time() + 3600},
        {"aud": ["oaiapp_test", "second-app"]},
        {"azp": "different-app"},
        {"at_hash": "invalid"},
    ],
)
def test_id_token_rejects_wrong_binding(signing_key, change):
    with pytest.raises(ChatGPTError, match="身份校验失败"):
        oauth.validate_id_token(
            sign_claims(signing_key, change), make_jwks(signing_key), "oaiapp_test", "fresh-nonce"
        )


def test_rejects_tampering_and_algorithm_substitution(signing_key):
    token = sign_claims(signing_key)
    head, body, sig = token.split(".")
    claims = json.loads(oauth._b64decode(body))
    claims["sub"] = "attacker"
    tampered = head + "." + oauth.b64encode(json.dumps(claims).encode()) + "." + sig
    for bad in (
        tampered,
        sign_claims(signing_key, header={"alg": "HS256", "kid": "test-key"}),
        sign_claims(
            signing_key,
            header={"alg": "RS256", "kid": "test-key", "jku": "https://evil.example/keys"},
        ),
    ):
        with pytest.raises(ChatGPTError):
            oauth.validate_id_token(bad, make_jwks(signing_key), "oaiapp_test", "fresh-nonce")


def test_authorized_party_for_multiple_audiences(signing_key):
    token = sign_claims(signing_key, {"aud": ["oaiapp_test", "second-app"], "azp": "oaiapp_test"})
    assert (
        oauth.validate_id_token(token, make_jwks(signing_key), "oaiapp_test", "fresh-nonce")["sub"]
        == "subject-1"
    )


@pytest.mark.parametrize(
    "query,client",
    [
        ("code=abc&state=wrong&client_id=oaiapp_test", oauth.DYNAMIC_CLIENT),
        ("code=abc&state=fresh&state=fresh&client_id=oaiapp_test", oauth.DYNAMIC_CLIENT),
        ("code=abc&state=fresh", oauth.DYNAMIC_CLIENT),
        ("code=abc&state=fresh&client_id=dynamic_agent_client", oauth.DYNAMIC_CLIENT),
        ("code=abc&state=fresh&client_id=oaiapp_other", "oaiapp_test"),
        ("code=abc&error=access_denied&state=fresh", "oaiapp_test"),
    ],
)
def test_callback_rejects_injection(query, client):
    with pytest.raises(ChatGPTError):
        oauth.validate_callback("/auth/callback?" + query, "fresh", client)


def test_callback_returning_client_and_denied_state():
    assert oauth.validate_callback(
        "/auth/callback?code=abc&state=fresh", "fresh", "oaiapp_test"
    ) == {"code": "abc", "client_id": "oaiapp_test"}
    with pytest.raises(ChatGPTError) as exc:
        oauth.validate_callback(
            "/auth/callback?error=access_denied&state=fresh", "fresh", "oaiapp_test"
        )
    assert exc.value.code == "access_denied"
    with pytest.raises(ChatGPTError) as exc:
        oauth.validate_callback(
            "/auth/callback?error=access_denied&state=wrong", "fresh", "oaiapp_test"
        )
    assert exc.value.code == "invalid_callback"


def test_oauth_fresh_pkce_loopback_and_registration_hints():
    first = oauth.OAuthAttempt("urn:uuid:stable-host")
    second = oauth.OAuthAttempt(
        "urn:uuid:stable-host",
        {"client_id": "oaiapp_test", "email": "same@example.test"},
        "private-id-token",
    )
    try:
        query = parse_qs(urlsplit(first.authorization_url).query)
        assert first.server.server_address[0] == "127.0.0.1"
        assert urlsplit(first.redirect_uri).path == "/auth/callback"
        assert query["agent_name_hint"] == ["VerdictQuant"]
        assert query["code_challenge"] == [
            oauth.b64encode(hashlib.sha256(first.verifier.encode()).digest())
        ]
        assert (
            first.state != second.state
            and first.nonce != second.nonce
            and first.verifier != second.verifier
        )
        returning = parse_qs(urlsplit(second.authorization_url).query)
        assert "agent_name_hint" not in returning
        assert returning["id_token_hint"] == ["private-id-token"]
        assert returning["client_id"] == ["oaiapp_test"]
    finally:
        first.close()
        second.close()


def test_actual_loopback_callback_completes_and_cancel_closes(monkeypatch):
    attempt = oauth.OAuthAttempt("host")
    threads = []

    def browser(_url, new):
        def deliver():
            url = (
                attempt.redirect_uri
                + "?"
                + urlencode(
                    {"state": attempt.state, "code": "test-code", "client_id": "oaiapp_test"}
                )
            )
            with urllib.request.urlopen(url, timeout=3) as response:
                assert response.status == 200

        thread = threading.Thread(target=deliver)
        thread.start()
        threads.append(thread)
        return True

    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    assert attempt.authorize(timeout=3)["client_id"] == "oaiapp_test"
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert attempt.server.socket.fileno() == -1
    assert attempt.authorization_url == ""
    cancelled = oauth.OAuthAttempt("host")
    with pytest.raises(ChatGPTError) as exc:
        cancelled.authorize(cancelled=lambda: True)
    assert exc.value.code == "cancelled" and cancelled.server.socket.fileno() == -1


def test_status_constructs_no_files_and_never_networks(tmp_path):
    class ForbiddenTransport:
        def request(self, *args, **kwargs):
            raise AssertionError("Status must not use network")

    root = tmp_path / "new-root"
    client = ChatGPTClient(root, MemorySecretStore(), ForbiddenTransport())
    assert client.status() == {
        "connected": False,
        "sharing": False,
        "profile_id": None,
        "label": "未连接 ChatGPT",
        "models": [],
    }
    assert client.list_accounts() == []
    assert not root.exists()

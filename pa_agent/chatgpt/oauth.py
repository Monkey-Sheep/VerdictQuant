"""Direct public-client OAuth for Sign in with ChatGPT.

No credentials are read from Codex, environment API keys, or browser storage.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
REVOKE_URL = ISSUER + "/api/accounts/oauth/revoke"
RESOURCE = "https://api.openai.com/v1"
MODELS_URL = RESOURCE + "/models"
RESPONSES_URL = RESOURCE + "/responses"
DYNAMIC_CLIENT = "dynamic_agent_client"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
SHARING_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
ALLOWED_URLS = {TOKEN_URL, DISCOVERY_URL, JWKS_URL, REVOKE_URL, MODELS_URL, RESPONSES_URL}
MAX_BODY = 2 * 1024 * 1024


class ChatGPTError(RuntimeError):
    """Safe user-facing error; never includes server messages or credentials."""

    def __init__(self, message: str, code: str = "chatgpt_error") -> None:
        super().__init__(message)
        self.code = code


def is_cancelled(cancelled=None) -> bool:
    if cancelled is None:
        return False
    return bool(cancelled() if callable(cancelled) else cancelled.is_set())


def check_cancelled(cancelled=None) -> None:
    if is_cancelled(cancelled):
        raise ChatGPTError("操作已取消。", "cancelled")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_json(data: bytes | str):
    return json.loads(data, object_pairs_hook=_unique_object)


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def validate_id_token(
    token: str,
    jwks: dict,
    client_id: str,
    nonce: str | None,
    *,
    now: float | None = None,
    require_nonce: bool = True,
    access_token: str | None = None,
) -> dict:
    """Validate signature and OIDC claims before returning any identity claims."""
    now = time.time() if now is None else now
    try:
        if not isinstance(token, str) or len(token) > 65536:
            raise ValueError("Invalid ID token")
        encoded_header, encoded_claims, encoded_sig = token.split(".")
        header = decode_json(_b64decode(encoded_header))
        claims = decode_json(_b64decode(encoded_claims))
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise ValueError("Invalid token JSON")
        if (
            header.get("alg") != "RS256"
            or header.get("crit")
            or any(field in header for field in ("jku", "x5u", "jwk"))
        ):
            raise ValueError("Invalid token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ValueError("Missing key identifier")
        keys = [
            key for key in jwks.get("keys", []) if isinstance(key, dict) and key.get("kid") == kid
        ]
        if len(keys) != 1:
            raise ValueError("Ambiguous or missing key")
        key = keys[0]
        if (
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
        ):
            raise ValueError("Wrong key type")
        if "key_ops" in key and "verify" not in key["key_ops"]:
            raise ValueError("Wrong key operation")
        modulus = int.from_bytes(_b64decode(key["n"]), "big")
        exponent = int.from_bytes(_b64decode(key["e"]), "big")
        if not 2048 <= modulus.bit_length() <= 8192:
            raise ValueError("Invalid RSA key size")
        rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(
            _b64decode(encoded_sig),
            f"{encoded_header}.{encoded_claims}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        if claims.get("iss") != ISSUER:
            raise ValueError("Wrong issuer")
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else audience
        if (
            not isinstance(audience, list)
            or not audience
            or not all(isinstance(v, str) for v in audience)
            or client_id not in audience
        ):
            raise ValueError("Wrong audience")
        if (len(audience) > 1 or "azp" in claims) and claims.get("azp") != client_id:
            raise ValueError("Wrong authorized party")
        for name in ("exp", "iat"):
            if (
                isinstance(claims.get(name), bool)
                or not isinstance(claims.get(name), (int, float))
                or not math.isfinite(claims[name])
            ):
                raise ValueError("Invalid token time")
        if claims["exp"] <= now or claims["iat"] > now + 60 or claims["exp"] <= claims["iat"]:
            raise ValueError("Expired or future token")
        if "nbf" in claims:
            nbf = claims["nbf"]
            if (
                isinstance(nbf, bool)
                or not isinstance(nbf, (int, float))
                or not math.isfinite(nbf)
                or nbf > now + 60
            ):
                raise ValueError("Not yet valid")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ValueError("Missing subject")
        token_nonce = claims.get("nonce")
        if (require_nonce or token_nonce is not None) and (
            not isinstance(nonce, str)
            or not isinstance(token_nonce, str)
            or not hmac.compare_digest(token_nonce, nonce)
        ):
            raise ValueError("Wrong nonce")
        if "at_hash" in claims:
            expected_hash = b64encode(
                hashlib.sha256((access_token or "").encode("ascii")).digest()[:16]
            )
            if not isinstance(claims["at_hash"], str) or not hmac.compare_digest(
                expected_hash, claims["at_hash"]
            ):
                raise ValueError("Wrong access-token hash")
        return claims
    except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature, UnicodeError):
        raise ChatGPTError("ChatGPT 登录身份校验失败，请重新登录。", "invalid_identity") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    """Small injectable HTTPS transport. Redirects never receive bearer tokens."""

    def request(self, method, url, *, headers, body=None, timeout=10):
        if url not in ALLOWED_URLS:
            raise ChatGPTError("ChatGPT 请求地址不受支持。", "invalid_endpoint")
        opener = urllib.request.build_opener(_NoRedirect())
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as response:
            return response


def request(transport, method, url, *, headers=None, body=None, cancelled=None):
    check_cancelled(cancelled)
    if url not in ALLOWED_URLS:
        raise ChatGPTError("ChatGPT 请求地址不受支持。", "invalid_endpoint")
    try:
        response = transport.request(method, url, headers=headers or {}, body=body, timeout=10)
    except (OSError, TimeoutError, urllib.error.URLError):
        raise ChatGPTError("无法连接 ChatGPT，请检查网络后重试。", "network_error") from None
    if is_cancelled(cancelled):
        response.close()
        check_cancelled(cancelled)
    if 300 <= response.status < 400:
        response.close()
        raise ChatGPTError("ChatGPT 返回了不允许的跳转。", "redirect_rejected")
    return response


def response_error(code: str = "", status: int = 0) -> ChatGPTError:
    if code in {
        "subscription_sharing_usage_limit_exceeded",
        "subscription_sharing_usage_unavailable",
    }:
        return ChatGPTError(
            "ChatGPT 套餐额度暂不可用，请在 ChatGPT 设置中查看用量与应用权限。", "usage_unavailable"
        )
    if (
        code in {"invalid_grant", "invalid_token", "token_expired", "invalid_api_key"}
        or status == 401
    ):
        return ChatGPTError("ChatGPT 会话已失效，请重新登录。", "reauth_required")
    if status == 403 or code in {"insufficient_scope", "subscription_sharing_not_enabled"}:
        return ChatGPTError("此 ChatGPT 账户尚未允许应用使用套餐，请重新授权。", "sharing_required")
    if status == 429:
        return ChatGPTError("ChatGPT 当前请求较多或额度受限，请稍后再试。", "rate_limited")
    return ChatGPTError("ChatGPT 请求未成功，请稍后重试。", "request_failed")


def read_json(response, cancelled=None) -> dict:
    try:
        data = response.read(MAX_BODY + 1)
        check_cancelled(cancelled)
        if len(data) > MAX_BODY:
            raise ValueError("Oversized body")
        result = decode_json(data)
        if not isinstance(result, dict):
            raise ValueError("Invalid JSON object")
        if response.status != 200:
            error = result.get("error", {})
            code = error.get("code", "") if isinstance(error, dict) else error
            raise response_error(code if isinstance(code, str) else "", response.status)
        return result
    except (ValueError, UnicodeError):
        if response.status != 200:
            raise response_error(status=response.status) from None
        raise ChatGPTError("ChatGPT 返回了无法识别的数据。", "invalid_response") from None
    except (OSError, TimeoutError):
        raise ChatGPTError("ChatGPT 网络连接中断，请重试。", "network_error") from None
    finally:
        response.close()


def form_request(transport, url, data, cancelled=None):
    return request(
        transport,
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=urlencode(data).encode("ascii"),
        cancelled=cancelled,
    )


def validate_callback(path: str, state: str, pending_client: str) -> dict:
    try:
        parts = urlsplit(path)
        if (
            parts.scheme
            or parts.netloc
            or parts.path != "/auth/callback"
            or parts.fragment
            or len(path) > 16384
        ):
            raise ValueError("Wrong callback path")
        pairs = parse_qsl(
            parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16
        )
        params = _unique_object(pairs)
        if not hmac.compare_digest(params.get("state", ""), state):
            raise ValueError("Wrong OAuth state")
        if "error" in params:
            if "code" in params:
                raise ValueError("Mixed callback result")
            raise ChatGPTError("ChatGPT 登录未获授权。", "access_denied")
        code = params.get("code", "")
        if not code or len(code) > 4096:
            raise ValueError("Missing authorization code")
        issued = params.get("client_id", pending_client)
        if issued == DYNAMIC_CLIENT or not re.fullmatch(r"[A-Za-z0-9._~-]{1,200}", issued):
            raise ValueError("Missing issued client")
        if pending_client != DYNAMIC_CLIENT and issued != pending_client:
            raise ValueError("Different registered client")
        return {"code": code, "client_id": issued}
    except (ValueError, TypeError, UnicodeError):
        raise ChatGPTError("登录回调校验失败，请重新登录。", "invalid_callback") from None


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(1)
        return connection, address


class OAuthAttempt:
    """One fresh authorization attempt with a loopback-only listener."""

    def __init__(self, host_id: str, registration: dict | None = None, id_token_hint: str = ""):
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.client_id = (registration or {}).get("client_id", DYNAMIC_CLIENT)
        self.result = None
        attempt = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                expected_host = f"127.0.0.1:{attempt.server.server_port}"
                if (
                    self.client_address[0] != "127.0.0.1"
                    or self.headers.get("Host") != expected_host
                    or attempt.result is not None
                ):
                    self.send_error(400)
                    return
                try:
                    result = validate_callback(self.path, attempt.state, attempt.client_id)
                except ChatGPTError as exc:
                    if exc.code != "access_denied":
                        self.send_error(400, "Invalid callback")
                        return
                    result = exc
                attempt.result = result
                body = "<!doctype html><meta charset=utf-8><title>VerdictQuant</title><p>登录流程已返回 VerdictQuant，可以关闭此窗口。</p>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with suppress(OSError):
                    self.wfile.write(body)

        try:
            self.server = _LoopbackServer(("127.0.0.1", 0), Handler)
        except OSError:
            raise ChatGPTError(
                "无法启动本机登录回调，请关闭占用程序后重试。", "callback_unavailable"
            ) from None
        self.server.timeout = 0.2
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
        params = {
            "client_id": self.client_id,
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()),
        }
        if self.client_id == DYNAMIC_CLIENT:
            params["agent_name_hint"] = "VerdictQuant"
        else:
            if id_token_hint:
                params["id_token_hint"] = id_token_hint
            if (registration or {}).get("email"):
                params["login_hint"] = registration["email"]
        self.authorization_url = AUTHORIZE_URL + "?" + urlencode(params)

    def authorize(self, cancelled=None, progress=None, timeout=180) -> dict:
        try:
            check_cancelled(cancelled)
            if progress:
                progress("请在浏览器中完成 ChatGPT 登录和授权。")
            if not webbrowser.open(self.authorization_url, new=1):
                raise ChatGPTError("无法打开浏览器，请检查系统默认浏览器。", "browser_unavailable")
            deadline = time.monotonic() + min(timeout, 180)
            while self.result is None:
                check_cancelled(cancelled)
                if time.monotonic() >= deadline:
                    raise ChatGPTError("ChatGPT 登录等待超时，请重新登录。", "login_timeout")
                self.server.handle_request()
            if isinstance(self.result, ChatGPTError):
                raise self.result
            check_cancelled(cancelled)
            return self.result
        finally:
            self.server.server_close()
            self.authorization_url = ""

    def close(self):
        self.server.server_close()
        self.authorization_url = ""

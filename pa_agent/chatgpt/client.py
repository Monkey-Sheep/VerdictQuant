"""Tool-free Responses client authenticated only by this app's own OAuth flow."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from pathlib import Path

from pa_agent.chatgpt import oauth
from pa_agent.chatgpt.oauth import ChatGPTError
from pa_agent.chatgpt.storage import SessionStore

__all__ = ["ChatGPTClient", "ChatGPTError"]


def _public(account: dict | None) -> dict:
    account = account or {}
    connected = bool(account.get("credential_ref"))
    return {
        "profile_id": account.get("profile_id"),
        "label": account.get("label", "未连接 ChatGPT"),
        "connected": connected,
        "sharing": connected and oauth.SHARING_SCOPES.issubset(account.get("scopes", [])),
    }


def _token_response(payload, old=None):
    """Do not infer grants from callback scope or unverified access-token claims."""
    old = old or {}
    if not isinstance(payload, dict):
        raise ChatGPTError("ChatGPT 登录凭据不完整，请重新登录。", "invalid_token_response")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    lifetime = payload.get("expires_in")
    token_type = payload.get("token_type", "")
    if (
        not isinstance(access, str)
        or not access
        or len(access) > 65536
        or not isinstance(refresh, str)
        or not refresh
        or len(refresh) > 65536
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or isinstance(lifetime, bool)
        or not isinstance(lifetime, (float, int))
        or not math.isfinite(lifetime)
        or not 0 < lifetime <= 86400
        or any(ord(char) <= 32 or ord(char) >= 127 for char in access + refresh)
        or not (access + refresh).isascii()
    ):
        raise ChatGPTError("ChatGPT 登录凭据不完整，请重新登录。", "invalid_token_response")
    scope = payload.get("scope")
    if scope is None and old:
        # RFC 6749 permits omitted scope on refresh when unchanged.
        scopes = old["scopes"]
    elif isinstance(scope, str):
        scopes = list(dict.fromkeys(scope.split()))
    else:
        scopes = []
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": payload.get("id_token") or old.get("id_token", ""),
        "token_type": "Bearer",
        "scopes": scopes,
        "expires_at": time.time() + lifetime,
        "earliest_refresh_at": payload.get("earliest_refresh_at"),
        "nonce": old.get("nonce", ""),
    }


def _model_catalog(payload):
    models = payload.get("models")
    if not isinstance(models, list):
        raise ChatGPTError("ChatGPT 模型目录格式不受支持，请稍后重试。", "invalid_catalog")
    result, seen = [], set()
    for row in models:
        if not isinstance(row, dict) or row.get("visibility") != "list":
            continue
        slug, name = row.get("slug"), row.get("display_name")
        if (
            not isinstance(slug, str)
            or not slug
            or len(slug) > 200
            or not isinstance(name, str)
            or not name
            or slug in seen
        ):
            continue
        seen.add(slug)
        # These capability fields are optional. Their absence is NOT evidence
        # for any effort; in that case omit reasoning and let the server choose.
        raw = row.get(
            "supported_reasoning_levels",
            row.get("supported_reasoning_efforts", row.get("reasoning_efforts", [])),
        )
        efforts = []
        if isinstance(raw, list):
            for entry in raw:
                value = entry.get("effort") if isinstance(entry, dict) else entry
                if (
                    isinstance(value, str)
                    and value
                    in oauth.RESPONSE_REASONING_EFFORTS
                    and value not in efforts
                ):
                    efforts.append(value)
        result.append({"slug": slug, "display_name": name, "reasoning_efforts": efforts})
    return result


def _event_error(event):
    response = event.get("response", {})
    error = response.get("error", {}) if isinstance(response, dict) else {}
    code = error.get("code", "") if isinstance(error, dict) else ""
    if not code:
        error = event.get("error", {})
        code = (error.get("code", "") if isinstance(error, dict) else "") or event.get("code", "")
    return oauth.response_error(code if isinstance(code, str) else "")


def _completed_text(response):
    output = response.get("output", [])
    if not isinstance(output, list):
        raise ChatGPTError("ChatGPT 完成结果格式异常。", "invalid_response")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise ChatGPTError("ChatGPT 完成结果格式异常。", "invalid_response")
        if item.get("type") == "message":
            if item.get("role") != "assistant" or item.get("status", "completed") != "completed":
                raise ChatGPTError("ChatGPT 返回了未完成消息。", "incomplete_response")
            content = item.get("content", [])
            if not isinstance(content, list):
                raise ChatGPTError("ChatGPT 完成结果格式异常。", "invalid_response")
            for part in content:
                if not isinstance(part, dict):
                    raise ChatGPTError("ChatGPT 完成结果格式异常。", "invalid_response")
                if part.get("type") == "refusal":
                    raise ChatGPTError("ChatGPT 未能完成此请求，请调整问题。", "refused")
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        elif item.get("type") != "reasoning":
            # No executable tool response is ever accepted by this client.
            raise ChatGPTError("ChatGPT 返回了当前模式不支持的内容。", "unsupported_output")
    return "".join(texts)


def _consume_stream(response, *, model, cancelled=None, on_delta=None):
    """Read SSE through completed; partial, failed and interrupted text is unusable."""
    fields, size, total = [], 0, 0
    finished_items = {}
    deadline = time.monotonic() + 300
    try:
        while True:
            oauth.check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise ChatGPTError("ChatGPT 响应超时，请重试。", "response_timeout")
            line = response.readline(oauth.MAX_BODY + 1)
            oauth.check_cancelled(cancelled)
            if not line:
                raise ChatGPTError(
                    "ChatGPT 响应在完成前中断，本次结果未采用。", "incomplete_response"
                )
            size += len(line)
            total += len(line)
            if size > oauth.MAX_BODY or total > 32 * 1024 * 1024:
                raise ChatGPTError("ChatGPT 响应超出本机处理限制。", "response_too_large")
            decoded = line.decode("utf-8").rstrip("\r\n")
            if decoded:
                if decoded.startswith("data:"):
                    fields.append(decoded[5:].lstrip(" "))
                continue
            size = 0
            if not fields:
                continue
            payload = "\n".join(fields)
            fields = []
            if payload == "[DONE]":
                raise ChatGPTError(
                    "ChatGPT 响应缺少完成确认，本次结果未采用。", "incomplete_response"
                )
            event = oauth.decode_json(payload)
            if not isinstance(event, dict):
                raise ValueError("Invalid SSE event")
            event_type = event.get("type")
            if event_type in {
                "error",
                "response.failed",
                "response.error",
                "response.incomplete",
                "response.cancelled",
            }:
                if event_type == "response.incomplete":
                    raise ChatGPTError(
                        "ChatGPT 未完成本次回答，本次结果未采用。", "incomplete_response"
                    )
                raise _event_error(event)
            if event_type in {"response.output_item.added", "response.output_item.done"}:
                item = event.get("item")
                index = event.get("output_index")
                if (not isinstance(item, dict) or isinstance(index, bool)
                        or not isinstance(index, int) or not 0 <= index <= 10000):
                    raise ValueError("Invalid output item")
                if item.get("type") not in {"message", "reasoning"}:
                    raise ChatGPTError("ChatGPT 返回了当前模式不支持的内容。", "unsupported_output")
                if event_type == "response.output_item.done":
                    if index in finished_items and finished_items[index] != item:
                        raise ValueError("Conflicting completed output item")
                    # Validate completed messages now, but publish only after
                    # response.completed confirms the whole request succeeded.
                    _completed_text({"output": [item]})
                    finished_items[index] = item
            if event_type in {"response.refusal.delta", "response.refusal.done"}:
                raise ChatGPTError("ChatGPT 未能完成此请求，请调整问题。", "refused")
            if event_type in {"response.content_part.added", "response.content_part.done"}:
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "refusal":
                    raise ChatGPTError("ChatGPT 未能完成此请求，请调整问题。", "refused")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if not isinstance(delta, str):
                    raise ValueError("Invalid delta")
                if on_delta:
                    on_delta(delta)
            elif event_type == "response.completed":
                completed = event.get("response")
                if (
                    not isinstance(completed, dict)
                    or completed.get("status") != "completed"
                    or completed.get("error")
                    or completed.get("incomplete_details")
                ):
                    raise ChatGPTError(
                        "ChatGPT 未提供有效完成结果，本次结果未采用。", "incomplete_response"
                    )
                text = _completed_text(completed)
                if not completed.get("output") and finished_items:
                    # SIWC streams can omit output from the terminal snapshot;
                    # output_item.done carries each full, completed message.
                    # Never promote output_text.delta drafts as final output.
                    text = _completed_text({"output": [finished_items[index]
                                                      for index in sorted(finished_items)]})
                if not text.strip():
                    raise ChatGPTError("ChatGPT 未返回可用文本。", "empty_response")
                usage = completed.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
                # Return known aggregate counters, never copy opaque server data.
                usage = {
                    key: value
                    for key, value in usage.items()
                    if key in {"input_tokens", "output_tokens", "total_tokens"}
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                }
                return {"text": text, "usage": usage, "model": model}
    except (UnicodeError, ValueError, TypeError):
        raise ChatGPTError("ChatGPT 响应格式异常，本次结果未采用。", "invalid_response") from None
    except (OSError, TimeoutError):
        raise ChatGPTError(
            "ChatGPT 连接在回答完成前中断，本次结果未采用。", "network_error"
        ) from None
    finally:
        response.close()


class ChatGPTClient:
    def __init__(self, data_root: Path | None = None, secret_store=None, transport=None):
        self._store = SessionStore(data_root, secret_store)
        self._transport = transport if transport is not None else oauth.HTTPTransport()
        self._epoch = 0
        self._new_account = False
        self._pending_client_id = ""
        self._signin_lock = threading.Lock()

    def status(self) -> dict:
        active, accounts = self._store.snapshot()
        account = next((row for row in accounts if row["profile_id"] == active), None)
        result = _public(account)
        result["models"] = [
            {**model, "reasoning_efforts": oauth.compatible_reasoning_efforts(model.get("reasoning_efforts"))}
            for model in (account or {}).get("models", []) if isinstance(model, dict)
        ]
        return result

    def list_accounts(self) -> list[dict]:
        return [_public(row) for row in self._store.snapshot()[1]]

    def select_account(self, profile_id: str) -> dict:
        if not profile_id:
            return self.begin_new_account()
        with self._store.locked() as db:
            if not self._store.account(db, profile_id):
                raise ChatGPTError("未找到此 ChatGPT 账户。", "account_missing")
            self._store.set_setting(db, "active", profile_id)
        self._epoch += 1
        self._new_account = False
        self._pending_client_id = ""
        return self.status()

    def begin_new_account(self) -> dict:
        """Stage a new registration; existing accounts and credentials stay intact."""
        self._new_account = True
        self._pending_client_id = ""
        return self.status()

    def _operation_cancelled(self, profile_id, epoch, cancelled):
        def check():
            if oauth.is_cancelled(cancelled) or self._epoch != epoch:
                return True
            active, accounts = self._store.snapshot()
            return active != profile_id or not any(
                row["profile_id"] == profile_id and row.get("credential_ref") for row in accounts
            )

        return check

    def _json(self, url, *, token="", cancelled=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        return oauth.read_json(
            oauth.request(self._transport, "GET", url, headers=headers, cancelled=cancelled),
            cancelled,
        )

    def _identity(self, tokens, client_id, nonce, cancelled, *, refresh=False):
        jwks = self._json(oauth.JWKS_URL, cancelled=cancelled)
        return oauth.validate_id_token(
            tokens["id_token"],
            jwks,
            client_id,
            nonce,
            require_nonce=not refresh,
            access_token=tokens["access_token"],
        )

    def sign_in(self, cancelled=None, progress=None, expected_profile_id=None) -> dict:
        if not self._signin_lock.acquire(blocking=False):
            raise ChatGPTError("已有 ChatGPT 登录正在进行。", "login_pending")
        attempt = None
        try:
            with self._store.locked(cancelled) as db:
                active = self._store.setting(db, "active")
                if expected_profile_id is not None and active != expected_profile_id:
                    raise ChatGPTError(
                        "ChatGPT 账户已在其他窗口改变，请重新选择。", "account_changed"
                    )
                starting_account = self._store.account(db, active)
                starting_ref = (starting_account or {}).get("credential_ref", "")
                account = None if self._new_account else starting_account
                host_id = self._store.setting(db, "host_id")
                credentials = self._store.credentials(account) if account else {}
            epoch = self._epoch

            def login_cancelled():
                return oauth.is_cancelled(cancelled) or self._epoch != epoch

            registration = account or (
                {"client_id": self._pending_client_id} if self._pending_client_id else None
            )
            attempt = oauth.OAuthAttempt(host_id, registration, credentials.get("id_token", ""))
            callback = attempt.authorize(login_cancelled, progress)
            if not account:
                self._pending_client_id = callback["client_id"]
            payload = oauth.read_json(
                oauth.form_request(
                    self._transport,
                    oauth.TOKEN_URL,
                    {
                        "grant_type": "authorization_code",
                        "client_id": callback["client_id"],
                        "code": callback["code"],
                        "code_verifier": attempt.verifier,
                        "redirect_uri": attempt.redirect_uri,
                        "resource": oauth.RESOURCE,
                    },
                    login_cancelled,
                ),
                login_cancelled,
            )
            tokens = _token_response(payload)
            tokens["nonce"] = attempt.nonce
            identity = self._identity(tokens, callback["client_id"], attempt.nonce, login_cancelled)
            if account and (
                identity["sub"] != account["subject"] or identity["iss"] != account["issuer"]
            ):
                raise ChatGPTError("登录账户与所选注册不一致，原会话已保留。", "account_mismatch")
            profile_id = hashlib.sha256(
                (identity["iss"] + "\0" + identity["sub"] + "\0" + callback["client_id"]).encode()
            ).hexdigest()[:32]
            oauth.check_cancelled(login_cancelled)
            with self._store.locked(login_cancelled) as db:
                current_account = self._store.account(db, active)
                if (
                    self._store.setting(db, "active") != active
                    or (current_account or {}).get("credential_ref", "") != starting_ref
                ):
                    raise ChatGPTError("账户状态已在其他窗口改变，请重新登录。", "cancelled")
                saved = self._store.account(db, profile_id) or {}
                email = identity.get("email", "")
                if (
                    not isinstance(email, str)
                    or len(email) > 320
                    or any(ord(char) < 32 for char in email)
                ):
                    email = ""
                label = saved.get("label") or f"{email or 'ChatGPT 账户'} · {profile_id[:8]}"
                saved.update(
                    {
                        "profile_id": profile_id,
                        "issuer": identity["iss"],
                        "subject": identity["sub"],
                        "client_id": callback["client_id"],
                        "email": email,
                        "label": label,
                        "scopes": tokens["scopes"],
                        "expires_at": tokens["expires_at"],
                        "models": [],
                    }
                )
                previous = self._store.replace_credentials(saved, tokens)
                self._store.put(db, saved)
                self._store.set_setting(db, "active", profile_id)
            self._store.delete_secret(previous)
            self._new_account = False
            self._pending_client_id = ""
            self._epoch += 1
            return self.status()
        finally:
            if attempt is not None:
                attempt.close()
            self._signin_lock.release()

    def _session(self, cancelled=None, expected_profile_id=None):
        previous = ""
        reauth = False
        with self._store.locked(cancelled) as db:
            active = self._store.setting(db, "active")
            if expected_profile_id is not None and active != expected_profile_id:
                raise ChatGPTError(
                    "ChatGPT 账户已在其他窗口改变，请重新打开对话。", "account_changed"
                )
            account = self._store.account(db, active)
            if not account or not account.get("credential_ref"):
                raise ChatGPTError("请先登录 ChatGPT。", "not_connected")
            tokens = self._store.credentials(account)
            if not tokens.get("access_token"):
                raise ChatGPTError("ChatGPT 凭据不可用，请重新登录。", "reauth_required")
            if not oauth.SHARING_SCOPES.issubset(tokens.get("scopes", [])):
                raise ChatGPTError(
                    "此账户已登录，但尚未授权使用 ChatGPT 套餐。", "sharing_required"
                )
            if tokens.get("expires_at", 0) <= time.time() + 60:
                try:
                    payload = oauth.read_json(
                        oauth.form_request(
                            self._transport,
                            oauth.TOKEN_URL,
                            {
                                "grant_type": "refresh_token",
                                "client_id": account["client_id"],
                                "refresh_token": tokens["refresh_token"],
                                "resource": oauth.RESOURCE,
                            },
                            cancelled,
                        ),
                        cancelled,
                    )
                except ChatGPTError as exc:
                    if exc.code != "reauth_required":
                        raise
                    previous = account.pop("credential_ref", "")
                    account["scopes"] = []
                    account["models"] = []
                    self._store.put(db, account)
                    reauth = True
                if not reauth:
                    updated = _token_response(payload, tokens)
                    if payload.get("id_token"):
                        identity = self._identity(
                            updated,
                            account["client_id"],
                            tokens.get("nonce"),
                            cancelled,
                            refresh=True,
                        )
                        if (
                            identity["sub"] != account["subject"]
                            or identity["iss"] != account["issuer"]
                        ):
                            raise ChatGPTError(
                                "ChatGPT 续期身份不一致，请重新登录。", "account_mismatch"
                            )
                    previous = self._store.replace_credentials(account, updated)
                    account.update(scopes=updated["scopes"], expires_at=updated["expires_at"])
                    self._store.put(db, account)
                    tokens = updated
        self._store.delete_secret(previous)
        if reauth:
            raise ChatGPTError("ChatGPT 会话已失效，请重新登录。", "reauth_required")
        if not oauth.SHARING_SCOPES.issubset(tokens.get("scopes", [])):
            raise ChatGPTError("ChatGPT 套餐使用权限已改变，请重新授权。", "sharing_required")
        return account, tokens

    def list_models(self, cancelled=None, expected_profile_id=None) -> list[dict]:
        epoch = self._epoch
        account, tokens = self._session(cancelled, expected_profile_id)
        stopped = self._operation_cancelled(account["profile_id"], epoch, cancelled)
        payload = self._json(oauth.MODELS_URL, token=tokens["access_token"], cancelled=stopped)
        models = _model_catalog(payload)
        with self._store.locked(stopped) as db:
            latest = self._store.account(db, account["profile_id"])
            oauth.check_cancelled(stopped)
            latest["models"] = models
            self._store.put(db, latest)
        return models

    def chat(
        self,
        messages: list[dict],
        model: str,
        reasoning_effort: str,
        cancelled=None,
        on_delta=None,
        instructions=None,
        output_schema=None,
        expected_profile_id=None,
    ) -> dict:
        if not isinstance(messages, list) or not messages:
            raise ChatGPTError("请提供对话内容。", "invalid_input")
        inputs = []
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in {"user", "assistant"}
                or not isinstance(message.get("content"), str)
            ):
                raise ChatGPTError("对话内容格式不受支持。", "invalid_input")
            inputs.append({"role": message["role"], "content": message["content"]})
        if sum(len(row["content"]) for row in inputs) > 2_000_000:
            raise ChatGPTError("对话内容过长，请精简后重试。", "invalid_input")
        epoch = self._epoch
        account, tokens = self._session(cancelled, expected_profile_id)
        stopped = self._operation_cancelled(account["profile_id"], epoch, cancelled)
        models = account.get("models", []) or self.list_models(
            stopped, expected_profile_id=account["profile_id"]
        )
        selected = next((row for row in models if row["slug"] == model), None)
        if not selected:
            raise ChatGPTError(
                "所选模型不在当前 ChatGPT 账户目录中，请刷新模型列表。", "model_unavailable"
            )
        if reasoning_effort and reasoning_effort not in oauth.compatible_reasoning_efforts(selected["reasoning_efforts"]):
            raise ChatGPTError(
                "所选推理档位不受当前接口及模型支持，请刷新模型并重新选择。", "effort_unavailable"
            )
        payload = {"model": model, "input": inputs, "store": False, "stream": True}
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if instructions is not None:
            if not isinstance(instructions, str):
                raise ChatGPTError("研究指令格式不受支持。", "invalid_input")
            payload["instructions"] = instructions
        if output_schema is not None:
            if not isinstance(output_schema, dict):
                raise ChatGPTError("研究结果格式定义无效。", "invalid_input")
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "verdictquant_research",
                    "strict": True,
                    "schema": output_schema,
                }
            }
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError):
            raise ChatGPTError("对话请求内容格式无效。", "invalid_input") from None
        response = oauth.request(
            self._transport,
            "POST",
            oauth.RESPONSES_URL,
            headers={
                "Authorization": "Bearer " + tokens["access_token"],
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            body=body,
            cancelled=stopped,
        )
        if response.status != 200:
            oauth.read_json(response, stopped)
        return _consume_stream(response, model=model, cancelled=stopped, on_delta=on_delta)

    def sign_out(self, cancelled=None, expected_profile_id=None) -> dict:
        self._epoch += 1
        revoked = False
        previous = ""
        with self._store.locked() as db:
            active = self._store.setting(db, "active")
            if expected_profile_id is not None and active != expected_profile_id:
                raise ChatGPTError("ChatGPT 账户已在其他窗口改变，请重新选择。", "account_changed")
            account = self._store.account(db, active)
            if account:
                try:
                    tokens = self._store.credentials(account)
                    refresh = tokens.get("refresh_token")
                    if refresh:
                        # Discovery is read at sign-out, but a poisoned discovery
                        # document cannot redirect this secret to another URL.
                        discovery = self._json(oauth.DISCOVERY_URL, cancelled=cancelled)
                        if (
                            discovery.get("issuer") != oauth.ISSUER
                            or discovery.get("revocation_endpoint") != oauth.REVOKE_URL
                        ):
                            raise ChatGPTError("ChatGPT 注销地址校验失败。", "invalid_discovery")
                        for retry in range(3):
                            try:
                                response = oauth.form_request(
                                    self._transport,
                                    oauth.REVOKE_URL,
                                    {
                                        "token": refresh,
                                        "token_type_hint": "refresh_token",
                                        "client_id": account["client_id"],
                                    },
                                    cancelled,
                                )
                                status = response.status
                                response.close()
                                if status == 200:
                                    revoked = True
                                    break
                                if status < 500:
                                    break
                            except ChatGPTError as exc:
                                if exc.code != "network_error":
                                    break
                            deadline = time.monotonic() + 0.25 * (2**retry)
                            while time.monotonic() < deadline:
                                oauth.check_cancelled(cancelled)
                                time.sleep(0.05)
                except ChatGPTError:
                    pass  # Local sign-out still finishes, with revoked=False.
                previous = account.pop("credential_ref", "")
                account.update(scopes=[], models=[], expires_at=0)
                self._store.put(db, account)
        self._store.delete_secret(previous)
        return {**self.status(), "revoked": revoked, "connected": False, "sharing": False}

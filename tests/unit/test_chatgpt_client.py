"""Offline session, rotation, account isolation and terminal SSE regression tests."""

from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import pytest

from pa_agent.chatgpt import oauth
from pa_agent.chatgpt.client import ChatGPTClient, ChatGPTError, _consume_stream
from pa_agent.security.secret_store import (
    MemorySecretStore,
    ResilientSecretStore,
    SecretStoreUnavailable,
)
from tests.unit.test_chatgpt_auth import make_jwks, sign_claims
from tests.unit.test_chatgpt_auth import signing_key as _signing_key_fixture

signing_key = _signing_key_fixture


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(json.dumps(body).encode() if isinstance(body, dict) else body)
        self.status = status


class FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        with self.lock:
            self.calls.append((method, url, kwargs))
            if not self.responses:
                raise AssertionError("Unexpected network request")
            value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def seed(
    client,
    profile="profile-1",
    *,
    expires=3600,
    sharing=True,
    client_id="oaiapp_test",
    subject="subject-1",
):
    store = client._store
    tokens = {
        "access_token": "synthetic-access-" + profile,
        "refresh_token": "synthetic-refresh-" + profile,
        "id_token": "synthetic-id-" + profile,
        "expires_at": time.time() + expires,
        "scopes": oauth.SCOPES.split() if sharing else ["openid"],
        "nonce": "fresh-nonce",
    }
    account = {
        "profile_id": profile,
        "issuer": oauth.ISSUER,
        "subject": subject,
        "client_id": client_id,
        "email": "same@example.test",
        "label": "same@example.test · " + profile,
        "scopes": tokens["scopes"],
        "expires_at": tokens["expires_at"],
        "models": [
            {
                "slug": "test-model",
                "display_name": "Test Model",
                "reasoning_efforts": ["low", "max"],
            }
        ],
    }
    with store.locked() as db:
        store.replace_credentials(account, tokens)
        store.put(db, account)
        store.set_setting(db, "active", profile)
    return account, tokens


def token_payload(**changes):
    result = {
        "access_token": "synthetic-rotated-access",
        "refresh_token": "synthetic-rotated-refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": oauth.SCOPES,
    }
    result.update(changes)
    return result


def completed(text="final answer", **changes):
    result = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }
    result.update(changes)
    return {"type": "response.completed", "response": result}


def sse(*events):
    return FakeResponse(
        b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    )


def test_stream_success_uses_completed_text_and_closes():
    response = sse(
        {"type": "response.output_text.delta", "delta": "draft "}, completed("confirmed final")
    )
    deltas = []
    result = _consume_stream(response, model="test-model", on_delta=deltas.append)
    assert result == {
        "text": "confirmed final",
        "model": "test-model",
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }
    assert deltas == ["draft "] and response.closed


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "secret-server-message",
                }
            },
        },
        {"type": "error", "code": "subscription_sharing_usage_unavailable"},
        {"type": "response.incomplete", "response": {"status": "incomplete"}},
        {"type": "response.completed", "response": {"status": "failed"}},
        completed(output=[{"type": "function_call", "name": "run_shell", "arguments": "never"}]),
        completed(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "private"}],
                }
            ]
        ),
    ],
)
def test_failed_or_tool_stream_never_returns_partial(event):
    response = sse({"type": "response.output_text.delta", "delta": "must not adopt"}, event)
    with pytest.raises(ChatGPTError) as exc:
        _consume_stream(response, model="test-model")
    assert "secret-server-message" not in str(exc.value)
    assert response.closed


def test_eof_done_malformed_and_cancellation_are_not_completion():
    for response in (
        sse({"type": "response.output_text.delta", "delta": "partial"}),
        FakeResponse(b"data: [DONE]\n\n"),
        FakeResponse(b"data: not-json\n\n"),
    ):
        with pytest.raises(ChatGPTError):
            _consume_stream(response, model="test-model")
        assert response.closed
    response = sse(completed())
    with pytest.raises(ChatGPTError) as exc:
        _consume_stream(response, model="test-model", cancelled=lambda: True)
    assert exc.value.code == "cancelled" and response.closed


def test_request_contract_no_tools_history_ids_or_api_fallback(tmp_path):
    transport = FakeTransport([sse(completed())])
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    assert (
        client.chat(
            [{"role": "user", "content": "hello"}],
            "test-model",
            "max",
            instructions="research only",
            output_schema=schema,
        )["text"]
        == "final answer"
    )
    method, url, kwargs = transport.calls[0]
    payload = json.loads(kwargs["body"])
    assert method == "POST" and url == oauth.RESPONSES_URL
    assert kwargs["headers"]["Authorization"] == "Bearer synthetic-access-profile-1"
    assert payload == {
        "model": "test-model",
        "input": [{"role": "user", "content": "hello"}],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "max"},
        "instructions": "research only",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "verdictquant_research",
                "strict": True,
                "schema": schema,
            }
        },
    }
    for invalid in ("unknown-model",):
        with pytest.raises(ChatGPTError) as exc:
            client.chat([{"role": "user", "content": "hello"}], invalid, "")
        assert exc.value.code == "model_unavailable"
    with pytest.raises(ChatGPTError) as exc:
        client.chat([{"role": "user", "content": "hello"}], "test-model", "unsupported")
    assert exc.value.code == "effort_unavailable"
    assert len(transport.calls) == 1


def test_catalog_only_visible_preserves_order_no_effort_guess(tmp_path):
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "models": [
                        {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
                        {"slug": "b", "display_name": "B", "visibility": "list"},
                        {
                            "slug": "a",
                            "display_name": "A",
                            "visibility": "list",
                            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}, {"effort": "ultra"}],
                        },
                    ]
                }
            )
        ]
    )
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client)
    assert client.list_models() == [
        {"slug": "b", "display_name": "B", "reasoning_efforts": []},
        {"slug": "a", "display_name": "A", "reasoning_efforts": ["low", "max"]},
    ]
    assert client.status()["models"][0]["slug"] == "b"
    assert "synthetic" not in json.dumps(client.status())


def test_saved_catalog_filters_endpoint_incompatible_effort_without_writing(tmp_path):
    transport = FakeTransport()
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    account, _ = seed(client)
    account['models'][0]['reasoning_efforts'] = ['low', 'max', 'ultra']
    with client._store.locked() as db:
        client._store.put(db, account)
    assert client.status()['models'][0]['reasoning_efforts'] == ['low', 'max']
    assert client._store.snapshot()[1][0]['models'][0]['reasoning_efforts'] == ['low', 'max', 'ultra']
    with pytest.raises(ChatGPTError) as error:
        client.chat([{'role': 'user', 'content': 'hello'}], 'test-model', 'ultra')
    assert error.value.code == 'effort_unavailable'
    assert not transport.calls


def test_effort_http_error_is_specific_and_does_not_echo_server_text():
    response = FakeResponse({'error': {'code': 'invalid_value', 'type': 'invalid_request_error',
                                      'param': 'reasoning.effort', 'message': 'private-server-text synthetic-access-token'}}, status=400)
    with pytest.raises(ChatGPTError) as error:
        oauth.read_json(response)
    assert error.value.code == 'effort_unavailable'
    assert '推理档位' in str(error.value)
    assert 'private-server-text' not in str(error.value)
    assert 'synthetic-access-token' not in str(error.value)
    assert response.closed


def output_done(text='finished message', index=0, **changes):
    item = completed(text)['response']['output'][0]
    item.update(changes)
    return {'type': 'response.output_item.done', 'output_index': index, 'item': item}


def test_terminal_without_output_uses_completed_message_not_delta_draft():
    response = sse({'type': 'response.output_text.delta', 'delta': 'draft'},
                   output_done('confirmed final'), completed(output=[]))
    deltas = []
    result = _consume_stream(response, model='test-model', on_delta=deltas.append)
    assert result['text'] == 'confirmed final'
    assert result['usage'] == {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}
    assert deltas == ['draft'] and response.closed


def test_completed_messages_without_successful_terminal_are_never_adopted():
    for terminal in ({'type': 'response.failed'}, {'type': 'response.incomplete'}, None):
        events = [output_done('must discard')]
        if terminal:
            events.append(terminal)
        response = sse(*events)
        with pytest.raises(ChatGPTError):
            _consume_stream(response, model='test-model')
        assert response.closed


def test_delta_drafts_alone_cannot_fill_empty_completed_output():
    response = sse({'type': 'response.output_text.delta', 'delta': 'only draft'}, completed(output=[]))
    with pytest.raises(ChatGPTError) as error:
        _consume_stream(response, model='test-model')
    assert error.value.code == 'empty_response'


def test_completed_message_items_keep_output_order_and_terminal_text_wins():
    assert _consume_stream(sse(output_done('second', 2), output_done('first', 1), completed(output=[])),
                           model='test-model')['text'] == 'firstsecond'
    assert _consume_stream(sse(output_done('item snapshot'), completed('terminal snapshot')),
                           model='test-model')['text'] == 'terminal snapshot'


@pytest.mark.parametrize('event', [
    {'type': 'response.output_item.added', 'output_index': 0, 'item': {'type': 'function_call'}},
    output_done('refused', content=[{'type': 'refusal', 'refusal': 'private'}]),
    {'type': 'response.refusal.delta', 'delta': 'private'},
    {'type': 'response.content_part.done', 'part': {'type': 'refusal', 'refusal': 'private'}},
    output_done('incomplete', status='in_progress'),
    output_done('invalid index', index=True),
])
def test_thin_terminal_does_not_hide_tools_refusals_or_invalid_items(event):
    response = sse(event, completed(output=[]))
    with pytest.raises(ChatGPTError):
        _consume_stream(response, model='test-model')
    assert response.closed


def test_missing_plan_scope_preserves_identity_without_network(tmp_path):
    transport = FakeTransport()
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client, sharing=False)
    assert client.status()["connected"] and not client.status()["sharing"]
    with pytest.raises(ChatGPTError) as exc:
        client.list_models()
    assert exc.value.code == "sharing_required" and not transport.calls


def test_rotating_refresh_serialized_across_independent_clients(tmp_path):
    secrets = MemorySecretStore()
    transport = FakeTransport([FakeResponse(token_payload())])
    first = ChatGPTClient(tmp_path, secrets, transport)
    old, _ = seed(first, expires=-1)
    second = ChatGPTClient(tmp_path, secrets, transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda client: client._session(), [first, second]))
    assert len(transport.calls) == 1
    assert all(tokens["refresh_token"] == "synthetic-rotated-refresh" for _, tokens in sessions)
    assert (
        sessions[0][0]["credential_ref"]
        == sessions[1][0]["credential_ref"]
        != old["credential_ref"]
    )
    assert not secrets.get(old["credential_ref"])
    form = parse_qs(transport.calls[0][2]["body"].decode())
    assert form["grant_type"] == ["refresh_token"] and form["client_id"] == ["oaiapp_test"]
    assert "scope" not in form


def test_new_credential_generation_cannot_read_stale_primary(tmp_path):
    class FailingPrimary(MemorySecretStore):
        fail = False

        def set(self, name, value):
            if self.fail:
                raise SecretStoreUnavailable("synthetic storage failure")
            super().set(name, value)

    primary, fallback = FailingPrimary(), MemorySecretStore()
    secrets = ResilientSecretStore(primary, fallback)
    client = ChatGPTClient(tmp_path, secrets, FakeTransport([FakeResponse(token_payload())]))
    seed(client, expires=-1)
    primary.fail = True
    client._session()
    restarted = ChatGPTClient(tmp_path, secrets, FakeTransport())
    assert restarted._session()[1]["refresh_token"] == "synthetic-rotated-refresh"


def test_invalid_grant_disables_session_and_preserves_registration(tmp_path):
    secrets = MemorySecretStore()
    client = ChatGPTClient(
        tmp_path,
        secrets,
        FakeTransport(
            [FakeResponse({"error": "invalid_grant", "error_description": "private-token"}, 400)]
        ),
    )
    seed(client, expires=-1)
    with pytest.raises(ChatGPTError) as exc:
        client._session()
    assert exc.value.code == "reauth_required"
    assert "private-token" not in str(exc.value)
    assert not client.status()["connected"]
    assert client.status()["profile_id"] == "profile-1"
    assert not secrets.values


def test_signout_only_selected_session_and_keeps_mapping(tmp_path):
    transport = FakeTransport(
        [
            FakeResponse({"issuer": oauth.ISSUER, "revocation_endpoint": oauth.REVOKE_URL}),
            FakeResponse(),
        ]
    )
    secrets = MemorySecretStore()
    client = ChatGPTClient(tmp_path, secrets, transport)
    seed(client, "profile-1")
    seed(client, "profile-2", client_id="oaiapp_other")
    result = client.sign_out()
    assert result["revoked"] and not result["connected"] and result["profile_id"] == "profile-2"
    accounts = {row["profile_id"]: row for row in client.list_accounts()}
    assert accounts["profile-1"]["connected"] and not accounts["profile-2"]["connected"]
    form = parse_qs(transport.calls[-1][2]["body"].decode())
    assert form == {
        "token": ["synthetic-refresh-profile-2"],
        "token_type_hint": ["refresh_token"],
        "client_id": ["oaiapp_other"],
    }
    assert client.select_account("profile-1")["connected"]


def test_signout_failure_clears_local_tokens_without_claiming_revoke(tmp_path):
    secrets = MemorySecretStore()
    client = ChatGPTClient(tmp_path, secrets, FakeTransport([OSError("url with private data")]))
    seed(client)
    result = client.sign_out()
    assert not result["revoked"] and not result["connected"] and not secrets.values


def test_redirect_and_poisoned_discovery_never_receive_secrets(tmp_path):
    transport = FakeTransport([FakeResponse(status=302)])
    with pytest.raises(ChatGPTError) as exc:
        oauth.request(
            transport, "GET", oauth.MODELS_URL, headers={"Authorization": "Bearer synthetic"}
        )
    assert exc.value.code == "redirect_rejected" and len(transport.calls) == 1
    with pytest.raises(ChatGPTError):
        oauth.request(transport, "POST", "https://evil.example", body=b"token=synthetic")
    assert len(transport.calls) == 1
    transport = FakeTransport(
        [
            FakeResponse(
                {"issuer": oauth.ISSUER, "revocation_endpoint": "https://evil.example/revoke"}
            )
        ]
    )
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client)
    assert not client.sign_out()["revoked"] and len(transport.calls) == 1


def fake_attempt(monkeypatch, *, issued="oaiapp_test", during=None):
    attempts = []

    class Attempt:
        nonce = "fresh-nonce"
        verifier = "fresh-verifier"
        redirect_uri = "http://127.0.0.1:14555/auth/callback"

        def __init__(self, host, account=None, hint=""):
            self.registration = account
            self.host = host
            self.hint = hint
            attempts.append(self)

        def authorize(self, cancelled=None, progress=None):
            if during:
                during()
            return {"code": "synthetic-code", "client_id": issued}

        def close(self):
            pass

    monkeypatch.setattr(oauth, "OAuthAttempt", Attempt)
    return attempts


def test_signin_validates_identity_before_replacing_tokens(tmp_path, monkeypatch, signing_key):
    secrets = MemorySecretStore()
    transport = FakeTransport(
        [
            FakeResponse(
                token_payload(id_token=sign_claims(signing_key, {"sub": "other-subject"}))
            ),
            FakeResponse(make_jwks(signing_key)),
        ]
    )
    client = ChatGPTClient(tmp_path, secrets, transport)
    seed(client)
    before = dict(secrets.values)
    attempts = fake_attempt(monkeypatch)
    with pytest.raises(ChatGPTError) as exc:
        client.sign_in()
    assert exc.value.code == "account_mismatch" and secrets.values == before
    assert attempts[0].registration["client_id"] == "oaiapp_test"
    assert attempts[0].hint == "synthetic-id-profile-1"


def test_new_registration_same_email_is_separate_and_stable_host(
    tmp_path, monkeypatch, signing_key
):
    transport = FakeTransport(
        [
            FakeResponse(token_payload(id_token=sign_claims(signing_key, {"aud": "oaiapp_new"}))),
            FakeResponse(make_jwks(signing_key)),
        ]
    )
    secrets = MemorySecretStore()
    client = ChatGPTClient(tmp_path, secrets, transport)
    seed(client)
    before = client.status()
    client.begin_new_account()
    assert client.status() == before
    attempts = fake_attempt(monkeypatch, issued="oaiapp_new")
    result = client.sign_in()
    assert result["connected"] and result["profile_id"] != "profile-1"
    assert len(client.list_accounts()) == 2 and attempts[0].registration is None
    with client._store.locked() as db:
        assert client._store.setting(db, "host_id") == attempts[0].host
    body = parse_qs(transport.calls[0][2]["body"].decode())
    assert body["client_id"] == ["oaiapp_new"]
    assert body["redirect_uri"] == [attempts[0].redirect_uri]
    assert body["code_verifier"] == [attempts[0].verifier]
    assert "synthetic" not in client._store.db_path.read_bytes().decode("latin1")


def test_other_instance_account_change_cannot_be_overwritten(tmp_path, monkeypatch, signing_key):
    secrets = MemorySecretStore()
    client = ChatGPTClient(
        tmp_path,
        secrets,
        FakeTransport(
            [
                FakeResponse(token_payload(id_token=sign_claims(signing_key))),
                FakeResponse(make_jwks(signing_key)),
            ]
        ),
    )
    seed(client, "profile-2")
    seed(client, "profile-1")
    other = ChatGPTClient(tmp_path, secrets, FakeTransport())
    fake_attempt(monkeypatch, during=lambda: other.select_account("profile-2"))
    with pytest.raises(ChatGPTError) as exc:
        client.sign_in()
    assert exc.value.code == "cancelled" and other.status()["profile_id"] == "profile-2"


def test_account_switch_cancels_existing_stream(tmp_path):
    client = ChatGPTClient(tmp_path, MemorySecretStore(), FakeTransport())
    seed(client, "profile-1")
    seed(client, "profile-2")
    client.select_account("profile-1")
    cancelled = client._operation_cancelled("profile-1", client._epoch, None)
    client.select_account("profile-2")
    response = sse(completed())
    with pytest.raises(ChatGPTError) as exc:
        _consume_stream(response, model="test-model", cancelled=cancelled)
    assert exc.value.code == "cancelled" and response.closed


def test_expected_profile_rejects_before_refresh_or_inference(tmp_path):
    transport = FakeTransport()
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client, "different-profile", expires=-1)
    for operation in (
        lambda: client.list_models(expected_profile_id="conversation-owner"),
        lambda: client.chat(
            [{"role": "user", "content": "private history"}],
            "test-model",
            "max",
            expected_profile_id="conversation-owner",
        ),
    ):
        with pytest.raises(ChatGPTError) as exc:
            operation()
        assert exc.value.code == "account_changed"
    assert not transport.calls


def test_malformed_optional_catalog_efforts_are_not_inferred(tmp_path):
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "models": [
                        {
                            "slug": "visible",
                            "display_name": "Visible",
                            "visibility": "list",
                            "supported_reasoning_levels": [
                                {"effort": []},
                                {"effort": {}},
                                None,
                                ["max"],
                                {"effort": "max"},
                            ],
                        }
                    ]
                }
            )
        ]
    )
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client)
    assert client.list_models()[0]["reasoning_efforts"] == ["max"]


def test_signin_with_identity_only_keeps_account(tmp_path, monkeypatch, signing_key):
    transport = FakeTransport(
        [
            FakeResponse(token_payload(scope="openid email", id_token=sign_claims(signing_key))),
            FakeResponse(make_jwks(signing_key)),
        ]
    )
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    fake_attempt(monkeypatch)
    result = client.sign_in()
    assert result["connected"] and not result["sharing"]
    with pytest.raises(ChatGPTError) as exc:
        client.list_models()
    assert exc.value.code == "sharing_required" and len(transport.calls) == 2


def test_invalid_code_retry_reuses_pending_issued_client(tmp_path, monkeypatch, signing_key):
    transport = FakeTransport(
        [
            FakeResponse({"error": "invalid_grant"}, 400),
            FakeResponse(token_payload(id_token=sign_claims(signing_key))),
            FakeResponse(make_jwks(signing_key)),
        ]
    )
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    attempts = fake_attempt(monkeypatch)
    with pytest.raises(ChatGPTError) as exc:
        client.sign_in()
    assert exc.value.code == "reauth_required"
    assert not client.status()["connected"]
    assert client.sign_in()["connected"]
    assert attempts[0].registration is None
    assert attempts[1].registration == {"client_id": "oaiapp_test"}
    assert attempts[0].host == attempts[1].host


class _SyntheticFileStore:
    """Cross-process fixture containing only hardcoded synthetic test tokens."""

    def __init__(self, root):
        from pathlib import Path

        self.root = Path(root) / "synthetic-test-secrets"
        self.root.mkdir(exist_ok=True)

    def _path(self, name):
        import hashlib

        return self.root / hashlib.sha256(name.encode()).hexdigest()

    def get(self, name):
        path = self._path(name)
        return path.read_text() if path.exists() else ""

    def set(self, name, value):
        self._path(name).write_text(value)

    def delete(self, name):
        self._path(name).unlink(missing_ok=True)


def _process_refresh(root, ready, result):
    from pathlib import Path

    class Transport:
        def request(self, method, url, **kwargs):
            assert method == "POST" and url == oauth.TOKEN_URL
            with (Path(root) / "refresh-count").open("a") as output:
                output.write("refresh\n")
            time.sleep(0.15)
            return FakeResponse(token_payload())

    client = ChatGPTClient(root, _SyntheticFileStore(root), Transport())
    ready.wait(10)
    try:
        result.put(client._session()[1]["refresh_token"])
    except (ChatGPTError, OSError, AssertionError) as exc:
        result.put(type(exc).__name__)


def test_refresh_rotation_serialized_across_processes(tmp_path):
    import multiprocessing

    client = ChatGPTClient(tmp_path, _SyntheticFileStore(tmp_path), FakeTransport())
    seed(client, expires=-1)
    context = multiprocessing.get_context("spawn")
    ready, result = context.Event(), context.Queue()
    children = [
        context.Process(target=_process_refresh, args=(str(tmp_path), ready, result))
        for _ in range(2)
    ]
    try:
        for child in children:
            child.start()
        ready.set()
        assert [result.get(timeout=15) for _ in children] == ["synthetic-rotated-refresh"] * 2
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        assert (tmp_path / "refresh-count").read_text().splitlines() == ["refresh"]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        result.close()
        result.join_thread()


def test_credential_cleanup_is_serialized_with_new_generation(tmp_path):
    class GuardedStore(MemorySecretStore):
        def __init__(self):
            super().__init__()
            self.guard = threading.Lock()

        def set(self, name, value):
            assert self.guard.acquire(blocking=False), "Concurrent credential file mutation"
            try:
                time.sleep(0.02)
                super().set(name, value)
            finally:
                self.guard.release()

        def delete(self, name):
            assert self.guard.acquire(blocking=False), "Concurrent credential file mutation"
            try:
                time.sleep(0.02)
                super().delete(name)
            finally:
                self.guard.release()

    secrets = GuardedStore()
    first = ChatGPTClient(tmp_path, secrets, FakeTransport([FakeResponse(token_payload())]))
    seed(first, expires=-1)
    secrets.set("obsolete-generation", "synthetic")
    second = ChatGPTClient(tmp_path, secrets, FakeTransport())
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup = pool.submit(second._store.delete_secret, "obsolete-generation")
        refresh = pool.submit(first._session)
        cleanup.result(timeout=5)
        assert refresh.result(timeout=5)[1]["refresh_token"] == "synthetic-rotated-refresh"


def test_status_closes_every_read_connection(tmp_path, monkeypatch):
    from pa_agent.chatgpt import storage

    client = ChatGPTClient(tmp_path, MemorySecretStore(), FakeTransport())
    seed(client)
    original = storage.sqlite3.connect
    connections = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.connection = original(*args, **kwargs)
            self.closed = False
            connections.append(self)

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def close(self):
            self.closed = True
            self.connection.close()

    monkeypatch.setattr(storage.sqlite3, "connect", Connection)
    for _ in range(10):
        assert client.status()["connected"]
    assert len(connections) == 10 and all(connection.closed for connection in connections)


def test_expected_profile_prevents_wrong_account_login_or_revocation(tmp_path, monkeypatch):
    transport = FakeTransport()
    client = ChatGPTClient(tmp_path, MemorySecretStore(), transport)
    seed(client, "current-account")
    attempts = fake_attempt(monkeypatch)
    for operation in (
        lambda: client.sign_in(expected_profile_id="different-account"),
        lambda: client.sign_out(expected_profile_id="different-account"),
    ):
        with pytest.raises(ChatGPTError) as exc:
            operation()
        assert exc.value.code == "account_changed"
        assert client.status()["connected"]
    assert not attempts and not transport.calls

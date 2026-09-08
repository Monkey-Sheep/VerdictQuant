"""Offline subprocess doubles only: never invoke or authenticate a real model."""
import json
import subprocess
import threading
from concurrent.futures import CancelledError
from pathlib import Path

import pytest

from pa_agent.installment.ai import AnalysisError
from pa_agent.installment.codex_provider import (
    DISABLED, CodexResearch, build_command, safe_environment,
)


PACKETS = [{"symbol": "NVDA", "price": {"close": 123.45}, "sources": []}]
COMPLETE = {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 4}}


class FakeProcess:
    def __init__(self, command, options, *, events=None, output=None, exit_code=0,
                 stderr="", wait_once=False, cancel_event=None):
        self.command, self.options = command, options
        self.events = [COMPLETE] if events is None else events
        self.output = {"assessments": [{"symbol": "NVDA"}]} if output is None else output
        self.exit_code, self.stderr = exit_code, stderr
        self.wait_once, self.cancel_event = wait_once, cancel_event
        self.calls, self.inputs, self.killed = 0, [], False
        self.returncode = None

    def communicate(self, input=None, timeout=None):
        self.calls += 1
        self.inputs.append(input)
        if self.calls == 1 and self.wait_once:
            if self.cancel_event:
                self.cancel_event.set()
            raise subprocess.TimeoutExpired("offline-fixture", timeout)
        self.returncode = -9 if self.killed else self.exit_code
        target = Path(self.command[self.command.index("-o") + 1])
        target.write_text(json.dumps(self.output), encoding="utf-8")
        return "\n".join(json.dumps(e) for e in self.events), self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    def poll(self):
        return self.returncode


def launch(monkeypatch, **settings):
    records = []

    def popen(command, **options):
        proc = FakeProcess(command, options, **settings)
        records.append(proc)
        return proc

    monkeypatch.setattr("pa_agent.installment.codex_provider.subprocess.Popen", popen)
    return CodexResearch(executable=Path("C:/offline-fixture/codex.exe")), records


def test_command_is_argument_list_and_enforces_subscription_isolation(tmp_path):
    command = build_command(Path("C:/path with spaces/codex.exe"), tmp_path,
                            "gpt-5.3-codex-spark", "high")
    assert isinstance(command, list)
    assert command[-1] == "-"
    assert "--ignore-user-config" in command
    assert "--strict-config" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in command
    assert 'web_search="disabled"' in command
    assert "project_doc_max_bytes=0" in command
    disabled = {command[i + 1] for i, v in enumerate(command) if v == "--disable"}
    assert set(DISABLED) == disabled
    assert {"plugins", "apps", "hooks", "memories", "shell_tool", "unified_exec",
            "browser_use", "browser_use_external", "code_mode_host"} <= disabled
    assert not any("dangerously" in arg for arg in command)


@pytest.mark.parametrize("model,effort", [
    ("gpt-x; echo bad", "high"), ("--oss", "high"),
    ("gpt-5.3-codex-spark", "high;echo bad"),
])
def test_model_and_effort_cannot_inject_command_arguments(tmp_path, model, effort):
    with pytest.raises(AnalysisError):
        build_command(Path("codex.exe"), tmp_path, model, effort)


def test_environment_drops_api_keys_but_keeps_official_login_context(monkeypatch):
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "DEEPSEEK_API_KEY",
                "AZURE_OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "UNRELATED_PRIVATE_SETTING"):
        monkeypatch.setenv(key, "private-fixture-do-not-forward")
    monkeypatch.setenv("CODEX_HOME", "C:/offline-fixture/codex-home")
    environment = safe_environment()
    assert "private-fixture-do-not-forward" not in environment.values()
    assert environment["CODEX_HOME"] == "C:/offline-fixture/codex-home"


def test_only_stdin_carries_public_packet_and_process_never_uses_shell(monkeypatch):
    analyst, records = launch(monkeypatch)
    result = analyst.analyze(PACKETS)
    process = records[0]
    assert process.options.get("shell", False) is False
    assert "123.45" not in " ".join(process.command)
    assert '"close": 123.45' in process.inputs[0]
    assert process.options["stdin"] is subprocess.PIPE
    assert process.options["stdout"] is subprocess.PIPE
    assert process.options["stderr"] is subprocess.PIPE
    assert result["provider"]["usage"] == {"input_tokens": 8, "output_tokens": 4}
    assert not process.options["cwd"].exists()  # Ephemeral directory cleaned.


def test_timeout_poll_resends_neither_prompt_nor_private_material(monkeypatch):
    analyst, records = launch(monkeypatch, wait_once=True)
    analyst.analyze(PACKETS)
    assert records[0].inputs[0]
    assert records[0].inputs[1] is None


@pytest.mark.parametrize("item_type", ["command_execution", "mcp_tool_call", "web_search", "file_change"])
def test_extra_tool_activity_rejects_even_complete_output(monkeypatch, item_type):
    analyst, _ = launch(monkeypatch, events=[
        {"type": "item.completed", "item": {"type": item_type}}, COMPLETE])
    with pytest.raises(AnalysisError):
        analyst.analyze(PACKETS)


@pytest.mark.parametrize("events", [[], [{"type": "turn.failed"}],
                                     [{"type": "error"}],
                                     [{"type": "item.completed", "item": {"type": "error"}}, COMPLETE]])
def test_incomplete_or_failed_turn_is_not_adopted(monkeypatch, events):
    analyst, _ = launch(monkeypatch, events=events)
    with pytest.raises(AnalysisError):
        analyst.analyze(PACKETS)


@pytest.mark.parametrize("rows", [[], [{"symbol": "TSLA"}],
                                  [{"symbol": "NVDA"}, {"symbol": "NVDA"}]])
def test_missing_foreign_or_duplicate_symbol_rejected(monkeypatch, rows):
    analyst, _ = launch(monkeypatch, output={"assessments": rows})
    with pytest.raises(AnalysisError):
        analyst.analyze(PACKETS)


@pytest.mark.parametrize("diagnostic", ["model not supported", "usage limit", "authentication failed", "unexpected failure"])
def test_nonzero_exit_never_exposes_raw_diagnostics(monkeypatch, diagnostic):
    secret = "SYNTHETIC_PRIVATE_DIAGNOSTIC"
    analyst, _ = launch(monkeypatch, exit_code=1, stderr=diagnostic + secret)
    with pytest.raises(AnalysisError) as captured:
        analyst.analyze(PACKETS)
    assert secret not in str(captured.value)


def test_cancel_kills_process_and_cannot_adopt_written_result(monkeypatch):
    stop = threading.Event()
    analyst, records = launch(monkeypatch, wait_once=True, cancel_event=stop)
    with pytest.raises(CancelledError):
        analyst.analyze(PACKETS, cancelled=stop)
    assert records[0].killed
    assert not records[0].options["cwd"].exists()


def test_timeout_kills_process_without_exposing_stderr(monkeypatch):
    analyst, records = launch(monkeypatch, wait_once=True, stderr="SYNTHETIC_PRIVATE_DIAGNOSTIC")
    times = iter([0, 0, 601])
    monkeypatch.setattr("pa_agent.installment.codex_provider.time.monotonic", lambda: next(times))
    with pytest.raises(AnalysisError) as captured:
        analyst.analyze(PACKETS)
    assert records[0].killed
    assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in str(captured.value)


def test_pre_cancel_never_launches_subprocess(monkeypatch):
    analyst, records = launch(monkeypatch)
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        analyst.analyze(PACKETS, cancelled=stop)
    assert records == []

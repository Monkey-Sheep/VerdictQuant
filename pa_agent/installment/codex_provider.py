"""Official local Codex client, using its own login and subscription entitlements.

This does not extract OAuth tokens or turn subscription credits into API keys.
Only public evidence is sent on stdin. Every run is tool-restricted, read-only,
ephemeral and independent of the user's project/configuration instructions.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import CancelledError
from pathlib import Path

from .ai import AnalysisError, PROMPT_VERSION, SYSTEM

DISABLED = ("shell_tool", "unified_exec", "code_mode_host", "plugins", "apps", "hooks", "memories", "skill_search",
            "multi_agent", "browser_use", "browser_use_external", "computer_use", "workspace_dependencies", "goals",
            "view_image", "image_generation", "sleep_tool", "unbounded_connection_retries")


def output_schema(symbols):
    text = {"type": "string"}
    texts = {"type": "array", "items": text}
    nullable_number = {"type": ["number", "null"]}
    props = {"symbol": {"type": "string", "enum": list(symbols)},
             "business_state": {"type": "string", "enum": ["intact", "uncertain", "impaired"]},
             "valuation_method": {"type": "string", "enum": ["pe_ttm", "normalized_pe", "ps_ttm", "ev_sales", "unknown"]},
             "fair_multiple_low": nullable_number, "fair_multiple_high": nullable_number,
             "normalized_profit_fraction": nullable_number,
             "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
             "summary": text, "valuation_explanation": text,
             "reasons": texts, "risks": texts, "assumptions": texts, "invalidators": texts,
             "business_evidence": texts, "valuation_evidence": texts, "negative_evidence": texts,
             "review_in_days": {"type": "integer"}}
    return {"type": "object", "properties": {"assessments": {"type": "array", "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}},
        "required": ["assessments"], "additionalProperties": False}


def find_codex():
    candidates = []
    command = shutil.which("codex")
    if command and Path(command).suffix.lower() == ".exe":
        candidates.append(Path(command))
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    if local.is_dir():
        candidates.extend(sorted(local.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    raise AnalysisError("没有找到官方 Codex 本机程序。请安装或打开 Codex 并登录；也可选择已有 API 配置。")


def build_command(executable: Path, directory: Path, model: str, effort: str):
    if not re.fullmatch(r"gpt-[a-zA-Z0-9._-]{1,70}", model) or effort not in {"low", "medium", "high", "xhigh"}:
        raise AnalysisError("Codex 模型或推理设置无效。")
    command = [str(executable), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--strict-config",
               "--sandbox", "read-only"]
    for name in DISABLED:
        command.extend(["--disable", name])
    command.extend(["-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0", "-c", 'forced_login_method="chatgpt"',
                    "-c", f'model_reasoning_effort="{effort}"', "-m", model, "-C", str(directory),
                    "--output-schema", str(directory / "schema.json"), "-o", str(directory / "result.json"), "--json", "-"])
    return command


def safe_environment():
    # Keep the OS/official auth context and network routing. No API key is
    # inherited, so a subscription problem cannot silently bill an API account.
    allowed = {"USERPROFILE", "LOCALAPPDATA", "APPDATA", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH",
               "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME"}
    return {k: v for k, v in os.environ.items() if k.upper() in allowed}


class CodexResearch:
    def __init__(self, model="gpt-5.3-codex-spark", reasoning_effort="high", timeout_seconds=240, executable=None):
        self.model, self.reasoning_effort = model, reasoning_effort
        self.timeout_seconds = min(max(timeout_seconds, 10), 600)
        self.executable = executable

    def analyze(self, packets, cancelled=None, progress=None):
        if cancelled is not None and cancelled.is_set():
            raise CancelledError()
        executable = self.executable or find_codex()
        public = json.dumps({"public_evidence": packets}, ensure_ascii=False, allow_nan=False)
        if len(public.encode()) > 300000:
            raise AnalysisError("公开资料超出单次分析上限，未调用 Codex。")
        # The temp working root contains only schema/output. No private plan,
        # holdings, settings, API key or source checkout is copied into it.
        with tempfile.TemporaryDirectory(prefix="verdictquant-public-research-") as tmp:
            directory = Path(tmp).resolve()
            schema = output_schema([x["symbol"] for x in packets])
            (directory / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = build_command(executable, directory, self.model, self.reasoning_effort)
            prompt = SYSTEM + "\n请直接分析以下公开资料并输出最终JSON，不调用任何工具，也不要读取任何文件。\n" + public
            if progress:
                progress(f"正在使用本机 Codex · {self.model}，消耗已登录账号额度…")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=directory, env=safe_environment(), text=True, encoding="utf-8", errors="replace",
                                        creationflags=flags)
            except OSError:
                raise AnalysisError("无法启动官方 Codex 程序，请检查安装或在模型设置切换服务。") from None
            started, pending_input = time.monotonic(), prompt
            try:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        proc.kill()
                        proc.communicate()
                        raise CancelledError()
                    if time.monotonic() - started > self.timeout_seconds:
                        proc.kill()
                        proc.communicate()
                        raise AnalysisError("Codex 本次分析超时，未采用不完整结果；可稍后手动重试。")
                    try:
                        stdout, stderr = proc.communicate(input=pending_input, timeout=.25)
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            # Never display or persist raw CLI stderr/transport errors. They
            # can include local installation paths and host diagnostic context.
            if proc.returncode != 0:
                text = (stdout + stderr).lower()
                if "not supported" in text and "model" in text:
                    raise AnalysisError("当前 Codex 登录不支持这个模型，请在模型设置选择账号可用型号。")
                if "usage limit" in text or "rate limit" in text or "quota" in text:
                    raise AnalysisError("当前 Codex 账号额度或速率受限，本次未生成新判断。")
                if "login" in text or "authentication" in text or "unauthorized" in text:
                    raise AnalysisError("请先在官方 Codex 中使用 ChatGPT 登录，再回软件手动更新。")
                raise AnalysisError("Codex 本次调用失败，旧记录已保留；请检查客户端版本与登录状态。")
            usage, completed = {}, False
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                item = event.get("item") or {}
                if item.get("type") not in {None, "agent_message", "reasoning", "error"}:
                    raise AnalysisError("本次 Codex 出现分析以外的工具活动，结果未被采用。")
                if kind == "turn.completed":
                    usage = {k: v for k, v in event.get("usage", {}).items() if k.endswith("tokens") and isinstance(v, int)}
                    completed = True
                if kind in {"turn.failed", "error"} or item.get("type") == "error":
                    raise AnalysisError("Codex 本次报告了错误，未采用结果。")
            output = directory / "result.json"
            if not completed or not output.is_file() or output.stat().st_size > 180000:
                raise AnalysisError("Codex 未返回完整结构化分析。")
            try:
                result = json.loads(output.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                rows = result["assessments"]
                expected = {p["symbol"] for p in packets}
                if not isinstance(rows, list) or len(rows) != len(expected) or {x.get("symbol") for x in rows} != expected:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                raise AnalysisError("Codex 输出的股票范围或 JSON 格式未通过校验。") from None
            return {"judgments": {x["symbol"]: x for x in rows}, "provider": {"model": self.model, "kind": "codex_cli", "status": "completed",
                    "usage": usage, "prompt_version": PROMPT_VERSION, "public_data_only": True,
                    "cost": "使用本机 Codex 的 ChatGPT 登录额度；不是 API Key 额度，不保证免费或无限。"}}

"""Public research through the app's own Sign in with ChatGPT session."""
from __future__ import annotations

import json
from concurrent.futures import CancelledError

from jsonschema import ValidationError, validate

from .ai import AnalysisError, PROMPT_VERSION, SYSTEM
from .codex_provider import output_schema


class ChatGPTResearch:
    def __init__(self, model="", reasoning_effort="", client=None, *, profile_id=""):
        self.model, self.reasoning_effort = model, reasoning_effort
        self.client = client
        self.profile_id = profile_id

    def analyze(self, packets, cancelled=None, progress=None):
        if cancelled is not None and cancelled.is_set():
            raise CancelledError()
        if not self.model or not self.profile_id:
            raise AnalysisError("请先在 AI 助手中登录 ChatGPT，选择模型并点击“用于股票研究”。")
        if self.client is None:
            from pa_agent.chatgpt.client import ChatGPTClient
            self.client = ChatGPTClient()
        if progress:
            progress(f"正在使用 ChatGPT · {self.model} 分析公开资料，消耗订阅额度…")
        schema = output_schema([row["symbol"] for row in packets])
        try:
            result = self.client.chat(
                [{"role": "user", "content": json.dumps({"public_evidence": packets}, ensure_ascii=False, allow_nan=False)}],
                model=self.model, reasoning_effort=self.reasoning_effort,
                cancelled=cancelled.is_set if cancelled else None,
                instructions=SYSTEM, output_schema=schema, expected_profile_id=self.profile_id,
            )
        except CancelledError:
            raise
        except Exception as exc:
            from pa_agent.chatgpt.client import ChatGPTError
            if isinstance(exc, ChatGPTError) and exc.code == "cancelled":
                raise CancelledError() from None
            message = str(exc) if isinstance(exc, ChatGPTError) else "ChatGPT 请求未完成，请在 AI 助手中检查登录、权限与模型。"
            raise AnalysisError(message) from None
        try:
            parsed = json.loads(result["text"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            validate(parsed, schema)
            rows = parsed["assessments"]
            expected = {row["symbol"] for row in packets}
            if len(rows) != len(expected) or {row["symbol"] for row in rows} != expected:
                raise ValueError()
        except (ValueError, KeyError, TypeError, ValidationError):
            raise AnalysisError("模型结构未通过校验，未采用本次投资判断。") from None
        return {"judgments": {row["symbol"]: row for row in rows},
                "provider": {"model": result.get("model", self.model), "kind": "chatgpt_plan", "status": "completed",
                             "usage": result.get("usage", {}), "prompt_version": PROMPT_VERSION,
                             "public_data_only": True, "cost": "消耗已授权的 ChatGPT 订阅额度；显示本次实际用量。"}}

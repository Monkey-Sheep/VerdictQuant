"""App-owned ChatGPT research routing and private-plan isolation."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError
from pathlib import Path

from pa_agent.installment.ai import AnalysisError
from pa_agent.installment.chatgpt_provider import ChatGPTResearch
from pa_agent.installment.service import DEFAULT_ENGINE, InstallmentService, validate_engine
from pa_agent.chatgpt.client import ChatGPTError

PROFILE_ID = "a" * 32


def assessment(symbol="NVDA"):
    return {"symbol": symbol, "business_state": "uncertain", "valuation_method": "unknown",
            "fair_multiple_low": None, "fair_multiple_high": None, "normalized_profit_fraction": None,
            "confidence": "low", "summary": "证据不足", "valuation_explanation": "待核验",
            "reasons": [], "risks": [], "assumptions": [], "invalidators": [],
            "business_evidence": [], "valuation_evidence": [], "negative_evidence": [], "review_in_days": 1}


class Client:
    calls = 0
    result = None
    def chat(self, messages, **kwargs):
        self.calls += 1
        self.messages, self.kwargs = messages, kwargs
        return self.result or {"text": json.dumps({"assessments": [assessment()]}),
                               "usage": {"input_tokens": 12, "output_tokens": 5}, "model": "gpt-6.1-sol"}


class ChatGPTResearchTests(unittest.TestCase):
    def test_missing_model_never_calls_client(self):
        client = Client()
        with self.assertRaisesRegex(AnalysisError, "AI 助手"):
            ChatGPTResearch(client=client).analyze([{"symbol": "NVDA"}])
        self.assertEqual(client.calls, 0)

    def test_completed_result_preserves_public_data_provenance_and_reported_usage(self):
        client = Client()
        result = ChatGPTResearch("gpt-6.1-sol", "ultra", client, profile_id=PROFILE_ID).analyze([{"symbol": "NVDA", "sources": []}])
        self.assertEqual(result["provider"]["kind"], "chatgpt_plan")
        self.assertEqual(result["provider"]["usage"], {"input_tokens": 12, "output_tokens": 5})
        self.assertTrue(result["provider"]["public_data_only"])
        self.assertEqual(client.kwargs["reasoning_effort"], "ultra")
        self.assertIn("output_schema", client.kwargs)
        self.assertEqual(client.kwargs["expected_profile_id"], PROFILE_ID)
        self.assertEqual(json.loads(client.messages[0]["content"]), {"public_evidence": [{"symbol": "NVDA", "sources": []}]})

    def test_invalid_duplicate_and_nonfinite_results_are_not_adopted(self):
        for payload in ({"assessments": [assessment(), assessment()]},
                        {"assessments": [assessment("COIN")]},
                        {"assessments": [{**assessment(), "confidence": "madeup"}]},
                        {"assessments": [{**assessment(), "fair_multiple_low": float("nan")}]},
                        {"assessments": [{"symbol": "NVDA"}]}):
            with self.subTest(payload=payload):
                client = Client()
                client.result = {"text": json.dumps(payload)}
                with self.assertRaisesRegex(AnalysisError, "校验"):
                    ChatGPTResearch("gpt-6.1-sol", "", client, profile_id=PROFILE_ID).analyze([{"symbol": "NVDA"}])

    def test_cancel_does_not_start_request(self):
        stop = threading.Event()
        stop.set()
        client = Client()
        with self.assertRaises(CancelledError):
            ChatGPTResearch("gpt-6.1-sol", "", client, profile_id=PROFILE_ID).analyze([{"symbol": "NVDA"}], stop)
        self.assertEqual(client.calls, 0)

    def test_client_cancel_is_propagated_and_unknown_errors_are_sanitized(self):
        from unittest.mock import Mock
        client = Mock()
        client.chat.side_effect = ChatGPTError("操作已取消。", "cancelled")
        with self.assertRaises(CancelledError):
            ChatGPTResearch("gpt-6.1-sol", "", client, profile_id=PROFILE_ID).analyze([{"symbol": "NVDA"}])
        client.chat.side_effect = RuntimeError("opaque-session-secret")
        with self.assertRaises(AnalysisError) as caught:
            ChatGPTResearch("gpt-6.1-sol", "", client, profile_id=PROFILE_ID).analyze([{"symbol": "NVDA"}])
        self.assertNotIn("opaque-session-secret", str(caught.exception))

    def test_old_codex_config_changes_effective_route_without_modifying_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "engine.json"
            original = json.dumps({"kind": "codex_cli", "model": "gpt-5.3-codex-spark", "reasoning_effort": "high"}).encode()
            path.write_bytes(original)
            service = InstallmentService(root)
            self.assertEqual(service.load_engine(), DEFAULT_ENGINE)
            self.assertEqual(path.read_bytes(), original)
            saved = service.save_engine({"kind": "chatgpt_plan", "model": "gpt-6.1-sol", "reasoning_effort": "ultra", "profile_id": PROFILE_ID})
            self.assertEqual(service.load_engine(), saved)

    def test_new_engine_shape_is_strict(self):
        for value in ([], {"kind": "chatgpt_plan", "model": "bad/model"},
                      {"kind": "chatgpt_plan", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
                      {"kind": "chatgpt_plan", "model": [], "reasoning_effort": "high"},
                      {"kind": "chatgpt_plan", "model": "gpt-6.1-sol", "reasoning_effort": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_engine(value)


if __name__ == "__main__":
    unittest.main()

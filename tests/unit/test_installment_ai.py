"""No live requests: constrain model transport, output and disclosure behavior."""
import json
import threading
import unittest
from concurrent.futures import CancelledError
from types import SimpleNamespace
from unittest.mock import patch

from pa_agent.installment.ai import AnalysisError, DeepSeekResearch, public_packet


class APIError(Exception):
    status_code = 401


class Stream:
    def __init__(self, events): self.events = events
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def __iter__(self): return iter(self.events)


def event(content=None, reason=None, thinking=None):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=content, reasoning_content=thinking), finish_reason=reason)])


class ModelTransportTests(unittest.TestCase):
    def setUp(self):
        self.provider = SimpleNamespace(model="deepseek-v4-flash", api_key="private-test-key", base_url="https://api.deepseek.com",
                                        thinking=True, reasoning_effort="high")
        self.ai = DeepSeekResearch(lambda: self.provider)
        self.packets = [{"symbol": "NVDA", "price": {"close": 100}, "sources": []}]
        self.events = [event(thinking="private internal reasoning not stored"),
                       event(json.dumps({"assessments": [{"symbol": "NVDA", "summary": "short public summary"}]}), "stop")]
        self.requests = []
        outer = self
        class Client:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
                outer.options = kwargs
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                return Stream(outer.events)
            def __enter__(self): return self
            def __exit__(self, *args): return False
        self.sdk = SimpleNamespace(OpenAI=Client, APIConnectionError=type("ConnectionError", (Exception,), {}),
                                   APIStatusError=APIError, APITimeoutError=type("TimeoutError", (Exception,), {}))

    def run_ai(self, cancelled=None):
        with patch.dict("sys.modules", {"openai": self.sdk}):
            return self.ai.analyze(self.packets, cancelled)

    def test_transport_bounded_and_reasoning_is_not_persisted(self):
        result = self.run_ai()
        self.assertEqual(self.options["max_retries"], 0)
        self.assertEqual(self.requests[0]["response_format"], {"type": "json_object"})
        self.assertLessEqual(self.requests[0]["max_tokens"], 20000)
        self.assertNotIn("private-test-key", json.dumps(result))
        self.assertNotIn("private internal reasoning", json.dumps(result))

    def test_truncated_output_is_rejected_without_repair_or_retry(self):
        self.events[-1].choices[0].finish_reason = "length"
        with self.assertRaises(AnalysisError): self.run_ai()
        self.assertEqual(len(self.requests), 1)

    def test_missing_or_duplicate_symbol_is_rejected(self):
        self.events = [event('{"assessments":[{"symbol":"TSLA"}]}', "stop")]
        with self.assertRaises(AnalysisError): self.run_ai()
        self.events = [event('{"assessments":[{"symbol":"NVDA"},{"symbol":"NVDA"}]}', "stop")]
        with self.assertRaises(AnalysisError): self.run_ai()

    def test_non_finite_json_is_rejected(self):
        self.events = [event('{"assessments":[{"symbol":"NVDA","value":NaN}]}', "stop")]
        with self.assertRaises(AnalysisError): self.run_ai()

    def test_pre_cancel_does_not_call_provider(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CancelledError): self.run_ai(stop)
        self.assertEqual(self.requests, [])

    def test_api_error_body_cannot_escape(self):
        class Failed:
            def __init__(self, **kwargs): raise APIError("Bearer private-test-key private account body")
        self.sdk.OpenAI = Failed
        with self.assertRaises(AnalysisError) as found: self.run_ai()
        self.assertNotIn("private-test-key", str(found.exception))
        self.assertNotIn("private account", str(found.exception))

    def test_public_packet_excludes_local_financial_inputs(self):
        public = {"identity": {"status": "verified"}, "financials": {}, "sources": [],
                  "positions_usd": {"NVDA": 99999}, "plan": {"private_budget": 12345},
                  "executions": ["private user record"], "local_path": "C:/private", "api_key": "not-for-model"}
        packet = public_packet("NVDA", {"close": 100}, public)
        serialized = json.dumps(packet)
        for forbidden in ("99999", "12345", "private user record", "C:/private", "not-for-model"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__": unittest.main()

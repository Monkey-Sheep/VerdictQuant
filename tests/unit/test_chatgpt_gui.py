"""Qt behavior checks for the opt-in ChatGPT workbench page."""
from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from pa_agent.chatgpt.oauth import ChatGPTError
from pa_agent.gui.chatgpt_assistant import ChatGPTAssistantWidget
from pa_agent.gui.manual_monitor import ManualMonitorWidget


class FakeClient:
    def __init__(self):
        self.current = "one"
        self.accounts = {"one": {"connected": True, "sharing": True},
                         "two": {"connected": True, "sharing": True}}
        self.new_account_staged = False
        self.catalog = [{"slug": "current", "display_name": "Current",
                         "reasoning_efforts": ["low", "high", "max", "ultra"]}]
        self.network_calls = []
        self.chat_calls = []
        self.chat_started = threading.Event()
        self.chat_release = threading.Event()
        self.chat_release.set()
        self.fail_chat = False
        self.chat_error = None
        self.fail_models = False
        self.revoked = True
        self.signin_sharing = True

    def status(self):
        active = self.accounts.get(self.current, {})
        return {"profile_id": self.current, "label": self.current,
                "connected": active.get("connected", False),
                "sharing": active.get("sharing", False),
                "models": self.catalog if active.get("connected") else []}

    def list_accounts(self):
        return [{"profile_id": key, "label": key, **value}
                for key, value in self.accounts.items()]

    def select_account(self, profile_id):
        if not profile_id:
            return self.begin_new_account()
        self.current = profile_id
        self.new_account_staged = False
        return self.status()

    def begin_new_account(self):
        self.new_account_staged = True
        return self.status()

    def sign_in(self, cancelled=None, progress=None, expected_profile_id=None):
        if expected_profile_id is not None and expected_profile_id != self.current:
            raise ChatGPTError("账户已改变。", "account_changed")
        self.network_calls.append("sign_in")
        self.current = "three"
        self.new_account_staged = False
        self.accounts["three"] = {"connected": True, "sharing": self.signin_sharing}
        return self.status()

    def sign_out(self, cancelled=None, expected_profile_id=None):
        if expected_profile_id is not None and expected_profile_id != self.current:
            raise ChatGPTError("账户已改变。", "account_changed")
        self.network_calls.append("sign_out")
        self.accounts[self.current]["connected"] = False
        return {"revoked": self.revoked, "connected": False}

    def list_models(self, cancelled=None, expected_profile_id=None):
        if expected_profile_id is not None and expected_profile_id != self.current:
            raise ChatGPTError("账户已改变。", "account_changed")
        self.network_calls.append("models")
        if self.fail_models:
            raise RuntimeError("private-token-and-url-must-not-appear")
        return self.catalog

    def chat(self, messages, model, reasoning_effort, cancelled=None, on_delta=None,
             instructions=None, output_schema=None, expected_profile_id=None):
        if expected_profile_id is not None and expected_profile_id != self.current:
            raise ChatGPTError("账户已改变。", "account_changed")
        self.network_calls.append("chat")
        self.chat_calls.append({"messages": messages, "model": model,
                                "reasoning_effort": reasoning_effort, "instructions": instructions,
                                "expected_profile_id": expected_profile_id})
        if on_delta:
            on_delta("临时草稿")
        self.chat_started.set()
        self.chat_release.wait(3)
        if cancelled and cancelled():
            raise RuntimeError("cancelled")
        if self.chat_error:
            raise self.chat_error
        if self.fail_chat:
            raise RuntimeError("private-token-and-url-must-not-appear")
        return {"text": "完成回答", "usage": {}, "model": model}


class ChatGPTAssistantQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = FakeClient()
        self.context_calls = 0
        self.configured = []

        def public_context():
            self.context_calls += 1
            return {"symbol": "TEST", "source": "public-only"}

        self.widget = ChatGPTAssistantWidget(
            client=self.client, context_provider=public_context,
            research_configurator=lambda model, effort: self.configured.append((model, effort)),
        )

    def tearDown(self):
        self.client.chat_release.set()
        self.widget.shutdown()
        self.widget.close()
        self.app.processEvents()

    def finish(self):
        deadline = time.monotonic() + 4
        while self.widget._future is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertIsNone(self.widget._future)

    def test_open_is_local_and_highest_catalog_effort_is_selected(self):
        self.assertEqual(self.client.network_calls, [])
        self.assertEqual(self.context_calls, 0)
        self.assertEqual(self.widget.model_combo.currentData(), "current")
        self.assertEqual(self.widget.effort_combo.currentData(), "max")
        self.assertEqual(self.widget.effort_combo.findData("ultra"), -1)
        self.assertFalse(self.widget.send_button.isEnabled())
        self.widget.use_for_research_button.click()
        self.assertEqual(self.configured, [("current", "max")])
        self.assertEqual(self.client.network_calls, [])

    def test_public_context_is_added_only_after_click_and_visible_before_send(self):
        self.widget.context_button.click()
        self.assertEqual(self.context_calls, 1)
        self.assertIn('"public-only"', self.widget.prompt.toPlainText())
        self.assertEqual(self.client.network_calls, [])

    def test_model_without_effort_metadata_uses_model_default(self):
        self.client.catalog = [{"slug": "visible", "display_name": "Visible"}]
        self.widget.refresh_models_button.click()
        self.finish()
        self.assertEqual(self.widget.model_combo.currentData(), "visible")
        self.assertEqual(self.widget.effort_combo.currentData(), "")
        self.assertEqual(self.widget.effort_combo.currentText(), "模型默认")
        self.widget.use_for_research_button.click()
        self.assertEqual(self.configured, [("visible", "")])
        self.widget.prompt.setPlainText("公开问题")
        self.widget.send_button.click()
        self.finish()
        self.assertEqual(self.client.chat_calls[0]["reasoning_effort"], "")

    def test_chat_stream_is_preview_until_success_and_history_is_per_account(self):
        self.client.chat_release.clear()
        self.widget.prompt.setPlainText("公开问题")
        self.widget.send_button.click()
        self.assertTrue(self.client.chat_started.wait(1))
        self.app.processEvents()
        self.assertIn("临时草稿", self.widget.transcript.toPlainText())
        self.assertEqual(self.widget._history, [])
        self.assertFalse(self.widget.account_combo.isEnabled())
        self.assertTrue(self.widget.cancel_button.isEnabled())
        self.client.chat_release.set()
        self.finish()
        self.assertEqual(self.widget._history, [
            {"role": "user", "content": "公开问题"},
            {"role": "assistant", "content": "完成回答"},
        ])
        self.assertEqual(self.client.chat_calls[0]["reasoning_effort"], "max")
        self.assertIn("只读投资研究助手", self.client.chat_calls[0]["instructions"])
        self.widget.account_combo.setCurrentIndex(self.widget.account_combo.findData("two"))
        self.assertEqual(self.widget._history, [])
        self.assertEqual(self.widget.transcript.toPlainText(), "")

    def test_cancel_and_failure_discard_partial_draft(self):
        self.client.chat_release.clear()
        self.widget.prompt.setPlainText("请解释")
        self.widget.send_button.click()
        self.assertTrue(self.client.chat_started.wait(1))
        self.app.processEvents()
        self.widget.cancel_button.click()
        self.client.chat_release.set()
        self.finish()
        self.assertEqual(self.widget._history, [])
        self.assertNotIn("临时草稿", self.widget.transcript.toPlainText())
        self.assertIn("已取消", self.widget.status_label.text())
        self.assertEqual(self.widget.prompt.toPlainText(), "请解释")

        self.client.fail_chat = True
        self.widget.send_button.click()
        self.finish()
        self.assertEqual(self.widget._history, [])
        self.assertNotIn("临时草稿", self.widget.transcript.toPlainText())
        self.assertNotIn("private-token", self.widget.status_label.text())

    def test_safe_backend_error_is_visible_without_raw_exception(self):
        self.client.chat_error = ChatGPTError("当前账号额度不足，请稍后重试。", "quota_exceeded")
        self.widget.prompt.setPlainText("公开问题")
        self.widget.send_button.click()
        self.finish()
        self.assertIn("额度不足", self.widget.status_label.text())
        self.assertEqual(self.widget._history, [])

    def test_model_refresh_failure_keeps_previous_catalog(self):
        self.client.fail_models = True
        self.widget.refresh_models_button.click()
        self.finish()
        self.assertEqual(self.widget.model_combo.currentData(), "current")
        self.assertEqual(self.widget.effort_combo.currentData(), "max")
        self.assertIn("原有列表已保留", self.widget.status_label.text())
        self.assertNotIn("private-token", self.widget.status_label.text())

    def test_add_account_and_logout_clear_conversation(self):
        self.widget.prompt.setPlainText("未发送内容")
        self.widget.add_account_button.click()
        self.assertEqual(self.client.current, "one")
        self.assertTrue(self.client.new_account_staged)
        self.assertEqual(self.widget.account_combo.currentData(), "")
        self.assertFalse(self.widget.send_button.isEnabled())
        self.assertEqual(self.widget.prompt.toPlainText(), "")
        self.assertEqual(self.client.network_calls, [])
        self.widget.login_button.click()
        self.finish()
        self.assertEqual(self.client.current, "three")
        self.assertEqual(self.client.network_calls, ["sign_in", "models"])
        self.widget._history = [{"role": "user", "content": "temporary"}]
        self.widget.sign_out()
        self.finish()
        self.assertEqual(self.widget._history, [])
        self.assertFalse(self.widget.send_button.isEnabled())

    def test_unconfirmed_remote_revocation_is_explicit(self):
        self.client.revoked = False
        self.widget.sign_out()
        self.finish()
        self.assertIn("远端撤销未确认", self.widget.status_label.text())

    def test_identity_login_without_sharing_is_not_presented_as_ready_for_inference(self):
        self.client.signin_sharing = False
        self.widget.sign_in()
        self.finish()
        self.assertEqual(self.client.network_calls, ["sign_in"])
        self.assertFalse(self.widget.send_button.isEnabled())
        self.assertIn("尚未授权", self.widget.status_label.text())

    def test_other_window_account_change_clears_history_before_send_refresh_or_logout(self):
        for action in (self.widget.send_message, self.widget.refresh_models, self.widget.sign_out,
                       self.widget.sign_in, self.widget.use_for_research):
            with self.subTest(action=action.__name__):
                self.client.current = "one"
                self.widget._refresh_local_state()
                self.widget._history = [{"role": "user", "content": "belongs-to-first-account"}]
                self.widget.prompt.setPlainText("follow-up")
                self.client.current = "two"
                action()
                self.assertEqual(self.widget._history, [])
                self.assertEqual(self.client.network_calls, [])
                self.assertEqual(self.configured, [])
                self.assertIn("其他窗口", self.widget.status_label.text())

    def test_other_window_switch_after_stream_started_discards_completion(self):
        self.client.chat_release.clear()
        self.widget.prompt.setPlainText("first-account question")
        self.widget.send_message()
        self.assertTrue(self.client.chat_started.wait(1))
        self.assertEqual(self.client.chat_calls[0]["expected_profile_id"], "one")
        self.client.current = "two"
        self.client.chat_release.set()
        self.finish()
        self.assertEqual(self.widget._history, [])
        self.assertEqual(self.widget.profile_id, "two")
        self.assertIn("其他窗口", self.widget.status_label.text())


class WorkbenchNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_third_navigation_opens_assistant_without_network(self):
        class Monitor:
            def latest(self):
                return None

        client = FakeClient()
        window = ManualMonitorWidget(service=Monitor(), chatgpt_client=client)
        try:
            self.assertEqual(len(window.nav_buttons), 3)
            window.nav_buttons[2].click()
            self.assertIs(window.tabs.currentWidget(), window.chatgpt_assistant)
            self.assertTrue(window.chatgpt_assistant.context_button.isEnabled())
            self.assertTrue(window.chatgpt_assistant.use_for_research_button.isEnabled())
            self.assertEqual(client.network_calls, [])
        finally:
            window.close()

    def test_real_workbench_research_button_binds_selected_profile_without_network(self):
        import tempfile
        from pathlib import Path
        from pa_agent.installment.service import InstallmentService
        class Monitor:
            def latest(self): return None
        client = FakeClient()
        profile = "a" * 32
        client.accounts[profile] = client.accounts.pop("one")
        client.current = profile
        with tempfile.TemporaryDirectory() as directory:
            service = InstallmentService(Path(directory))
            window = ManualMonitorWidget(service=Monitor(), research_service=service, chatgpt_client=client)
            try:
                window.chatgpt_assistant.use_for_research_button.click()
                self.assertEqual(service.load_engine(), {"kind": "chatgpt_plan", "model": "current", "reasoning_effort": "max", "profile_id": profile})
                self.assertEqual(client.network_calls, [])
                self.assertIn("已保存", window.chatgpt_assistant.status_label.text())
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

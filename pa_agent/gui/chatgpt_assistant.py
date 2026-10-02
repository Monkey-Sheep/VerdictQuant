"""An opt-in, in-memory ChatGPT research conversation for the workbench."""
from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QPlainTextEdit, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from pa_agent.chatgpt.oauth import ChatGPTError
from pa_agent.gui.workbench_ui import label, toolbar_button


READ_ONLY_INSTRUCTIONS = (
    "你是 VerdictQuant 的只读投资研究助手。仅分析用户主动提供的公开资料，"
    "区分已核实事实、推测和待核实项目，指出数据日期与来源缺口。"
    "不得声称已连接券商、查看真实持仓或账户，也不得下单、执行交易或改变真实资金。"
    "不要把聊天回复当作投资计划的自动修改或人工验收。"
)

_EFFORT_RANK = {name: index for index, name in enumerate(
    ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
)}


class _TaskSignals(QObject):
    delta = pyqtSignal(int, str)
    finished = pyqtSignal(int)


class ChatGPTAssistantWidget(QWidget):
    """All network activity starts from an explicit button click."""

    def __init__(self, parent=None, *, client=None, context_provider=None, research_configurator=None):
        super().__init__(parent)
        if client is None:
            try:
                from pa_agent.chatgpt.client import ChatGPTClient
                client = ChatGPTClient()
            except Exception:
                client = None
        self.client = client
        self.context_provider = context_provider
        self.research_configurator = research_configurator
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future | None = None
        self._cancelled: threading.Event | None = None
        self._job_id = 0
        self._task_kind = ""
        self._signals = _TaskSignals()
        self._signals.delta.connect(self._on_delta)
        self._signals.finished.connect(self._finish_task)
        self._closing = False
        self._draft = ""
        self._pending_prompt = ""
        self._history: list[dict[str, str]] = []
        self._models: list[dict] = []
        self._connected = False
        self._sharing = False
        self.profile_id = ""
        self._new_account_staged = False

        page = QVBoxLayout(self)
        page.setContentsMargins(27, 23, 27, 16)
        page.setSpacing(13)
        page.addWidget(label("AI 助手", "pageTitle"))
        page.addWidget(label("使用你主动授权的 ChatGPT 账户讨论公开研究；聊天只保留在本次窗口中。", "mutedLabel", wrap=True))

        account_card = QFrame()
        account_card.setObjectName("surfaceCard")
        account_layout = QVBoxLayout(account_card)
        account_layout.setContentsMargins(17, 14, 17, 14)
        account_layout.setSpacing(10)
        account_layout.addWidget(label("ChatGPT 账户", "sectionTitle"))
        account_row = QHBoxLayout()
        account_row.addWidget(label("账户"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(180)
        account_row.addWidget(self.account_combo, 1)
        self.add_account_button = QPushButton("添加账户")
        self.login_button = QPushButton("网页登录")
        self.logout_button = QPushButton("退出登录")
        for button in (self.add_account_button, self.login_button, self.logout_button):
            account_row.addWidget(button)
        account_layout.addLayout(account_row)
        self.account_status = label("正在读取本地账号状态…", "statusLabel", wrap=True)
        account_layout.addWidget(self.account_status)
        page.addWidget(account_card)

        model_card = QFrame()
        model_card.setObjectName("surfaceCard")
        model_layout = QVBoxLayout(model_card)
        model_layout.setContentsMargins(17, 14, 17, 14)
        model_layout.setSpacing(10)
        model_layout.addWidget(label("模型与研究", "sectionTitle"))
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(210)
        self.effort_combo = QComboBox()
        self.effort_combo.setMinimumWidth(115)
        self.refresh_models_button = QPushButton("刷新模型")
        self.use_for_research_button = QPushButton("用于股票研究")
        model_row.addWidget(label("模型"))
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(label("推理"))
        model_row.addWidget(self.effort_combo)
        model_row.addWidget(self.refresh_models_button)
        model_row.addWidget(self.use_for_research_button)
        model_layout.addLayout(model_row)
        model_layout.addWidget(label("模型列表由当前账号提供；选择后点击“用于股票研究”才会保存本地分析设置。", "metricNote", wrap=True))
        page.addWidget(model_card)

        chat_card = QFrame()
        chat_card.setObjectName("surfaceCard")
        chat_layout = QVBoxLayout(chat_card)
        chat_layout.setContentsMargins(17, 14, 17, 14)
        chat_layout.setSpacing(10)
        chat_layout.addWidget(label("研究对话", "sectionTitle"))
        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("本次窗口还没有对话。")
        chat_layout.addWidget(self.transcript, 1)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("输入公开研究问题；不会自动读取真实账户或持仓。")
        self.prompt.setMaximumHeight(105)
        self.prompt.setStyleSheet("background:#fff;border:1px solid #dce3ec;border-radius:7px;padding:8px;")
        chat_layout.addWidget(self.prompt)
        action_row = QHBoxLayout()
        self.context_button = QPushButton("加入当前股票公开研究")
        self.send_button = toolbar_button("发送", "research", primary=True)
        self.cancel_button = QPushButton("取消生成")
        action_row.addWidget(self.context_button)
        action_row.addStretch()
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.send_button)
        chat_layout.addLayout(action_row)
        self.status_label = label("登录并刷新模型后可开始对话。", "statusLabel", wrap=True)
        chat_layout.addWidget(self.status_label)
        page.addWidget(chat_card, 1)

        self.account_combo.currentIndexChanged.connect(self._account_selected)
        self.add_account_button.clicked.connect(self.add_account)
        self.login_button.clicked.connect(self.sign_in)
        self.logout_button.clicked.connect(self.sign_out)
        self.model_combo.currentIndexChanged.connect(self._model_selected)
        self.effort_combo.currentIndexChanged.connect(self._update_controls)
        self.refresh_models_button.clicked.connect(self.refresh_models)
        self.use_for_research_button.clicked.connect(self.use_for_research)
        self.context_button.clicked.connect(self.add_public_context)
        self.send_button.clicked.connect(self.send_message)
        self.cancel_button.clicked.connect(self.cancel_task)
        self.prompt.textChanged.connect(self._update_controls)
        self._refresh_local_state()

    @staticmethod
    def _catalog(value) -> list[dict]:
        if not isinstance(value, list):
            return []
        models = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            raw_efforts = item.get("reasoning_efforts")
            if not isinstance(slug, str) or not slug or slug in seen:
                continue
            efforts = list(dict.fromkeys(e for e in raw_efforts if isinstance(e, str) and e)) if isinstance(raw_efforts, list) else []
            seen.add(slug)
            models.append({"slug": slug, "display_name": str(item.get("display_name") or slug),
                           "reasoning_efforts": efforts})
        return models

    def _refresh_local_state(self, *, models=None):
        if self.client is None:
            self._connected = self._sharing = False
            self._models = []
            self._set_models([])
            self.account_status.setText("ChatGPT 本地会话存储不可用，请检查本机配置后重新打开软件。")
            self._update_controls()
            return
        try:
            state = self.client.status()
            accounts = self.client.list_accounts()
            if not isinstance(state, dict) or not isinstance(accounts, list):
                raise ValueError("invalid account state")
        except Exception:
            self._connected = self._sharing = False
            self._models = []
            self.account_status.setText("本地账号状态无法读取，请检查配置后重试。")
            self._set_models([])
            self._update_controls()
            return
        self._connected = bool(state.get("connected"))
        self._sharing = bool(state.get("sharing"))
        profile_id = state.get("profile_id") or ""
        self.profile_id = profile_id
        if self._new_account_staged:
            self._connected = self._sharing = False
            profile_id = ""
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem("新账户 / 未选择", "")
        for account in accounts:
            if isinstance(account, dict) and isinstance(account.get("profile_id"), str) and account["profile_id"]:
                self.account_combo.addItem(str(account.get("label") or "已保存账户"), account["profile_id"])
        index = self.account_combo.findData(profile_id)
        self.account_combo.setCurrentIndex(index if index >= 0 else 0)
        self.account_combo.blockSignals(False)
        self._models = self._catalog([] if self._new_account_staged else state.get("models") if models is None else models)
        self._set_models(self._models)
        if self._new_account_staged:
            self.account_status.setText("新账户待登录；原账户仍保留在列表中。")
        elif self._connected and self._sharing:
            self.account_status.setText("已登录，已授权使用 ChatGPT 套餐额度。")
        elif self._connected:
            self.account_status.setText("已登录，但尚未授权使用套餐额度；请点击“网页登录”完成授权。")
        else:
            self.account_status.setText("未登录；点击“网页登录”后在官方页面自行授权。")
        self._update_controls()

    def _set_models(self, models):
        previous = self.model_combo.currentData()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in models:
            self.model_combo.addItem(model["display_name"], model["slug"])
        index = self.model_combo.findData(previous)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)
        self._model_selected()

    def _model_selected(self, *_):
        slug = self.model_combo.currentData()
        model = next((item for item in self._models if item["slug"] == slug), None)
        efforts = model["reasoning_efforts"] if model else []
        previous = self.effort_combo.currentData()
        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        if efforts:
            for effort in efforts:
                self.effort_combo.addItem(effort, effort)
            selected = self.effort_combo.findData(previous)
            if selected < 0:
                selected = max(range(len(efforts)), key=lambda i: _EFFORT_RANK.get(efforts[i], -1))
        elif model:
            self.effort_combo.addItem("模型默认", "")
            selected = 0
        else:
            selected = -1
        if selected >= 0:
            self.effort_combo.setCurrentIndex(selected)
        self.effort_combo.blockSignals(False)
        self._update_controls()

    def _update_controls(self, *_):
        busy = self._future is not None
        ready = self._connected and self._sharing and bool(self.model_combo.currentData()) and self.effort_combo.currentIndex() >= 0
        local_available = self.client is not None
        self.account_combo.setEnabled(not busy and local_available)
        self.add_account_button.setEnabled(not busy and local_available)
        self.login_button.setEnabled(not busy and local_available)
        self.logout_button.setEnabled(not busy and self._connected)
        self.model_combo.setEnabled(not busy and bool(self._models))
        self.effort_combo.setEnabled(not busy and bool(self.model_combo.currentData()))
        self.refresh_models_button.setEnabled(not busy and self._connected and self._sharing)
        self.use_for_research_button.setEnabled(not busy and ready and self.research_configurator is not None)
        self.context_button.setEnabled(not busy and self.context_provider is not None)
        self.prompt.setEnabled(not busy)
        self.send_button.setEnabled(not busy and ready and bool(self.prompt.toPlainText().strip()))
        self.cancel_button.setEnabled(busy and self._cancelled is not None and not self._cancelled.is_set())

    def _clear_conversation(self):
        self._history.clear()
        self._draft = self._pending_prompt = ""
        self.transcript.clear()
        self.prompt.clear()

    def _ensure_account_current(self):
        if self._new_account_staged:
            return True
        try:
            state = self.client.status()
        except Exception:
            self.status_label.setText("无法校验当前账号，本次未发送请求。")
            return False
        if (state.get("profile_id") or "") != self.profile_id or bool(state.get("connected")) != self._connected or bool(state.get("sharing")) != self._sharing:
            self._clear_conversation()
            self._refresh_local_state()
            self.status_label.setText("账号或授权已在其他窗口变化；对话已清空，本次未发送请求。")
            return False
        return True

    def _account_selected(self, *_):
        if self._future is not None:
            return
        profile_id = self.account_combo.currentData() or ""
        try:
            self.client.select_account(profile_id)
        except Exception:
            self.account_status.setText("切换账户失败，请检查本地账号状态。")
            return
        self._new_account_staged = not bool(profile_id)
        self._clear_conversation()
        self._refresh_local_state()
        self.status_label.setText("已切换账户；本次对话已清空。")

    def add_account(self):
        if self._future is not None:
            return
        try:
            self.client.begin_new_account()
        except Exception:
            self.account_status.setText("准备新账户失败，请检查本地账号状态。")
            return
        self._new_account_staged = True
        self._clear_conversation()
        self._refresh_local_state()
        self.status_label.setText("新账户已准备；点击“网页登录”完成授权。")

    def _start_task(self, kind, work):
        if self._future is not None or self._closing:
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chatgpt-assistant")
        self._job_id += 1
        job_id = self._job_id
        self._task_kind = kind
        self._cancelled = threading.Event()
        cancelled = self._cancelled
        signals = self._signals
        self._future = self._executor.submit(work, cancelled.is_set,
                                             lambda delta: signals.delta.emit(job_id, str(delta)))
        self._future.add_done_callback(lambda _: signals.finished.emit(job_id))
        self._update_controls()

    def sign_in(self):
        if self._future is not None:
            return
        if not self._ensure_account_current():
            return
        self.status_label.setText("正在等待网页授权；请在官方页面完成操作。")
        profile_id = self.profile_id

        def work(cancelled, _delta):
            result = self.client.sign_in(cancelled=cancelled, expected_profile_id=profile_id)
            if cancelled():
                return {"result": result, "models": None}
            if not result.get("sharing"):
                return {"result": result, "models": []}
            try:
                models = self.client.list_models(cancelled=cancelled, expected_profile_id=result.get("profile_id"))
                return {"result": result, "models": models}
            except Exception:
                return {"result": result, "models": None}

        self._start_task("sign_in", work)

    def sign_out(self):
        if self._future is not None:
            return
        if not self._ensure_account_current():
            return
        self.status_label.setText("正在退出当前账户…")
        profile_id = self.profile_id
        self._start_task("sign_out", lambda cancelled, _delta: self.client.sign_out(cancelled=cancelled, expected_profile_id=profile_id))

    def refresh_models(self):
        if self._future is not None or not (self._connected and self._sharing):
            return
        if not self._ensure_account_current():
            return
        self.status_label.setText("正在刷新当前账号可用模型…")
        profile_id = self.profile_id
        self._start_task("models", lambda cancelled, _delta: self.client.list_models(cancelled=cancelled, expected_profile_id=profile_id))

    def use_for_research(self):
        if self._future is not None or not (self._connected and self._sharing):
            return
        if not self._ensure_account_current():
            return
        model, effort = self.model_combo.currentData(), self.effort_combo.currentData()
        if not (model and effort is not None and self.research_configurator):
            return
        try:
            self.research_configurator(model, effort)
        except Exception:
            self.status_label.setText("分析设置未保存，请检查本地配置。")
            return
        self.status_label.setText("已保存股票研究分析设置；旧研究结果需要手动更新。")

    def add_public_context(self):
        if self._future is not None or self.context_provider is None:
            return
        try:
            context = self.context_provider()
            if isinstance(context, (dict, list)):
                content = json.dumps(context, ensure_ascii=False, indent=2)
            elif isinstance(context, str):
                content = context
            else:
                content = ""
            if not content.strip() or len(content) > 128_000:
                self.status_label.setText("当前股票公开研究为空或过长，请选择内容更短的股票后重试。")
                return
        except Exception:
            self.status_label.setText("读取当前公开研究失败，未加入提问。")
            return
        current = self.prompt.toPlainText().rstrip()
        self.prompt.setPlainText((current + "\n\n" if current else "") + "【当前公开研究】\n" + content)
        self.status_label.setText("公开研究已加入输入框；发送前可以查看和编辑。")

    def send_message(self):
        if self._future is not None or not (self._connected and self._sharing):
            return
        if not self._ensure_account_current():
            return
        prompt = self.prompt.toPlainText().strip()
        model, effort = self.model_combo.currentData(), self.effort_combo.currentData()
        if not (prompt and model and effort is not None):
            self.status_label.setText("请先登录、刷新模型并输入问题。")
            return
        messages = [*self._history, {"role": "user", "content": prompt}]
        self._pending_prompt = prompt
        self._draft = ""
        self._render_transcript()
        self.status_label.setText("正在生成；完成前的文字仅供预览。")
        profile_id = self.profile_id
        self._start_task("chat", lambda cancelled, delta: self.client.chat(
            messages, model=model, reasoning_effort=effort, cancelled=cancelled,
            on_delta=delta, instructions=READ_ONLY_INSTRUCTIONS, expected_profile_id=profile_id,
        ))

    def _render_transcript(self):
        parts = [f"{'你' if row['role'] == 'user' else '助手'}：\n{row['content']}"
                 for row in self._history]
        if self._pending_prompt:
            parts.extend((f"你：\n{self._pending_prompt}",
                          f"助手（生成中，尚未完成）：\n{self._draft}"))
        self.transcript.setPlainText("\n\n".join(parts))
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)

    def _on_delta(self, job_id, delta):
        if self._closing or job_id != self._job_id or self._task_kind != "chat" or self._cancelled.is_set():
            return
        self._draft += delta
        self._render_transcript()

    def _finish_task(self, job_id):
        if self._closing or job_id != self._job_id or self._future is None:
            return
        kind, future, cancelled = self._task_kind, self._future, self._cancelled.is_set()
        self._future = None
        self._cancelled = None
        self._task_kind = ""
        if kind in {"chat", "models"} and not self._ensure_account_current():
            self._update_controls()
            return
        safe_error = ""
        try:
            result = future.result()
        except Exception as exc:
            result = None
            failed = True
            if isinstance(exc, ChatGPTError):
                safe_error = str(exc)
                cancelled = cancelled or exc.code == "cancelled"
        else:
            failed = False
        if kind == "chat":
            if cancelled or failed or not isinstance(result, dict) or not isinstance(result.get("text"), str):
                self.status_label.setText("生成已取消；未保存未完成回复。" if cancelled else
                                          "生成失败；未完成回复已丢弃。" + (" " + safe_error if safe_error else "请重试。"))
                self._pending_prompt = self._draft = ""
                self._render_transcript()
            else:
                self._history.extend(({"role": "user", "content": self._pending_prompt},
                                      {"role": "assistant", "content": result["text"]}))
                self._pending_prompt = self._draft = ""
                self.prompt.clear()
                self._render_transcript()
                self.status_label.setText("回复已完成；本次对话仅在当前窗口内保存。")
        elif kind == "sign_in":
            if cancelled:
                self._refresh_local_state()
                self.status_label.setText("网页授权已取消；请检查当前账号状态。")
            elif not failed:
                models = result.get("models") if isinstance(result, dict) else None
                self._new_account_staged = False
                self._clear_conversation()
                self._refresh_local_state(models=models)
                if self._connected and not self._sharing:
                    self.status_label.setText("账号已登录，但尚未授权使用 ChatGPT 套餐；请点击网页登录重新授权。")
                else:
                    self.status_label.setText("登录完成；模型已刷新。" if models is not None else "登录完成；模型刷新失败，可手动重试。")
            else:
                self._refresh_local_state()
                self.status_label.setText("登录未完成。" + (" " + safe_error if safe_error else "请检查网页授权或网络后重试。"))
        elif kind == "sign_out":
            if not failed:
                self._new_account_staged = False
                self._clear_conversation()
                self._refresh_local_state()
                if isinstance(result, dict) and result.get("revoked") is False:
                    self.status_label.setText("本机已退出；远端撤销未确认，可在官方账户中检查。本次对话已清空。")
                else:
                    self.status_label.setText("已退出登录；本次对话已清空。")
            else:
                self.status_label.setText("退出登录未完成。" + (" " + safe_error if safe_error else "请重试。"))
        elif kind == "models":
            if not failed and isinstance(result, list):
                self._models = self._catalog(result)
                self._set_models(self._models)
                self.status_label.setText("模型列表已刷新。" if self._models else "当前账号没有可用模型与推理档位。")
            else:
                self.status_label.setText("模型刷新失败；原有列表已保留。" + (" " + safe_error if safe_error else "请重试。"))
        self._update_controls()

    def cancel_task(self):
        if self._cancelled is not None:
            self._cancelled.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在取消；未完成内容不会作为正式回复。")

    def shutdown(self):
        self._closing = True
        if self._cancelled is not None:
            self._cancelled.set()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

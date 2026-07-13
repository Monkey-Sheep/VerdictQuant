"""AI 模型设置对话框 — 只包含 AI 提供商相关字段."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings
from pa_agent.gui.user_guide import open_user_guide, user_guide_uri
from pa_agent.security.policy import provider_configuration_error
from pa_agent.security.secret_store import SecretStoreUnavailable


class AIModelSettingsDialog(QDialog):
    """AI 模型 / 提供商配置对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 模型设置")
        self.setMinimumWidth(520)
        self._settings = settings
        self._setup_ui()
        self._load_values()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        provider_group = QGroupBox("AI 提供商")
        form = QFormLayout(provider_group)

        self._model_edit = QLineEdit()
        form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        form.addRow("Base URL:", self._base_url_edit)

        api_key_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self._api_key_edit)
        self._show_key_btn = QPushButton("Show")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(52)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_key_row.addWidget(self._show_key_btn)
        form.addRow("API Key:", api_key_row)

        self._thinking_check = QCheckBox("启用 Thinking")
        form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        self._agent_tutorial_btn = QPushButton("智能体使用教程及问题解决方法")
        self._agent_tutorial_btn.setToolTip(user_guide_uri())
        self._agent_tutorial_btn.clicked.connect(self._open_agent_tutorial_url)
        form.addRow("", self._agent_tutorial_btn)

        root.addWidget(provider_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setText("保存")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── 加载 / 保存 ────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        p = self._settings.provider
        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)
        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)

    def _on_save(self) -> None:
        p = self._settings.provider
        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        api_key = self._api_key_edit.text().strip()

        field_err = self._validate_provider_fields(model, base_url)
        if field_err:
            QMessageBox.warning(self, "Invalid AI provider", field_err)
            return
        p.model = model
        p.base_url = base_url
        p.api_key = api_key

        p.thinking = self._thinking_check.isChecked()
        p.reasoning_effort = self._reasoning_effort_combo.currentText()  # type: ignore[assignment]

        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
        except (OSError, SecretStoreUnavailable) as exc:
            QMessageBox.critical(
                self,
                "API Key 保存失败",
                "API Key 没有安全持久化，设置未保存。\n\n"
                f"错误类型：{type(exc).__name__}\n"
                "请确认当前 Windows 用户可写入凭据库后重试。",
            )
            return
        self.accept()

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def focus_api_key_field(self) -> None:
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("Hide")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("Show")

    def _apply_cursor_provider(self, *, preferred_model: str = "") -> str | None:
        del preferred_model
        return "Cursor agent routing is disabled in the hardened build."

    def _apply_qclaw_provider(self, *, preferred_model: str = "") -> str | None:
        del preferred_model
        return "QClaw routing is disabled in the hardened build."

    def _apply_workbuddy_provider(self, *, preferred_model: str = "") -> str | None:
        del preferred_model
        return "WorkBuddy routing is disabled in the hardened build."

    @staticmethod
    def _validate_provider_fields(model: str, base_url: str) -> str | None:
        return provider_configuration_error(model, base_url)

    def _open_agent_tutorial_url(self) -> None:
        open_user_guide()

"""Settings for automatic GitHub Release updates."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings
from pa_agent.security.secret_store import SecretStoreUnavailable


class UpdateSettingsDialog(QDialog):
    """Edit automatic update behaviour and private-repository access."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("更新设置")
        self.setMinimumWidth(580)

        root = QVBoxLayout(self)
        explanation = QLabel(
            "程序会在启动后后台检查 GitHub Release，发现新版本后自动下载并校验。\n"
            "当前仓库为私有时，需要一个只授予该仓库 Contents: read 权限的 "
            "fine-grained token；仓库公开后请留空。"
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)

        form = QFormLayout()
        self._auto_check = QCheckBox("启动后自动检查")
        self._auto_download = QCheckBox("发现更新后自动下载")
        self._token = QLineEdit()
        self._token.setEchoMode(QLineEdit.EchoMode.Password)
        self._token.setPlaceholderText("私仓可选；公开仓库留空")
        form.addRow("检查更新:", self._auto_check)
        form.addRow("自动下载:", self._auto_download)
        form.addRow("GitHub Token:", self._token)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setText("保存")
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        update_settings = settings.updates
        self._auto_check.setChecked(update_settings.auto_check)
        self._auto_download.setChecked(update_settings.auto_download)
        self._token.setText(update_settings.github_token)

    def _save(self) -> None:
        update_settings = self._settings.updates
        update_settings.auto_check = self._auto_check.isChecked()
        update_settings.auto_download = self._auto_download.isChecked()
        update_settings.github_token = self._token.text().strip()
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
        except (OSError, SecretStoreUnavailable) as exc:
            QMessageBox.critical(
                self,
                "更新设置保存失败",
                f"GitHub Token 没有安全持久化，设置未保存。\n\n错误类型：{type(exc).__name__}",
            )
            return
        self.accept()

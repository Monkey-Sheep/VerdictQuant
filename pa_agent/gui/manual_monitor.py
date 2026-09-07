"""Open cached monitoring immediately; refresh only on an explicit button click."""
from __future__ import annotations

import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from pa_agent.monitoring.service import MonitoringService, RefreshBusy
from pa_agent.monitoring.view import render


class ManualMonitorWidget(QWidget):
    def __init__(self, parent=None, service=None):
        super().__init__(parent)
        self.service = service or MonitoringService()
        self._executor = None
        self._future = None
        self._cancelled = threading.Event()
        self.setWindowTitle("VerdictQuant · 组合监控")
        self.resize(1180, 860)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        header = QHBoxLayout()
        title = QLabel("组合监控")
        title.setStyleSheet("font-size:24px; font-weight:700;")
        header.addWidget(title)
        header.addStretch()
        self.refresh_button = QPushButton("刷新监控")
        self.refresh_button.setObjectName("primaryButton")
        self.refresh_button.setMinimumHeight(36)
        self.refresh_button.clicked.connect(self.refresh_data)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        subtitle = QLabel("打开查看上次结果，点击刷新才联网。基金与美股分别呈现。")
        subtitle.setObjectName("mutedLabel")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._open_source)
        self.browser.setStyleSheet("QTextBrowser { padding:10px; font-size:13px; }")
        layout.addWidget(self.browser, 1)
        self.status = QLabel("仅读取本地保存结果；不会定时推送。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._finish_refresh)
        try:
            self.browser.setHtml(render(self.service.latest()))
        except (OSError, ValueError, KeyError, TypeError):
            self.browser.setHtml(render(None))
            self.status.setText("保存结果无法校验，请手动刷新；旧文件已保留。")
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _open_source(self, url: QUrl):
        if url.scheme() == "https":
            QDesktopServices.openUrl(url)

    def refresh_data(self):
        if self._future is not None:
            return
        self._cancelled.clear()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manual-monitor")
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("刷新中…")
        self.status.setText("正在获取公开数据，页面仍可查看。不会读取真实账户或执行交易。")
        self._future = self._executor.submit(self.service.refresh, self._cancelled)
        self._poll.start()

    def _finish_refresh(self):
        if self._future is None or not self._future.done():
            return
        self._poll.stop()
        try:
            result = self._future.result()
            self.browser.setHtml(render(result))
            self.status.setText("公开数据已刷新；研究缺口已单独列出。" if not result["data"]["errors"] else "刷新完成，部分数据未通过校验；缺口已列出。")
        except RefreshBusy:
            self.status.setText("另一个窗口正在刷新，稍后再试；当前结果已保留。")
        except CancelledError:
            self.status.setText("刷新已取消，保存结果未变。")
        except Exception:
            self.status.setText("刷新未完成，保留上次结果。请检查网络或数据源后重试。")
        finally:
            self._future = None
            self.refresh_button.setEnabled(True)
            self.refresh_button.setText("刷新监控")

    def shutdown(self):
        self._cancelled.set()
        self._poll.stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


def main(argv=None):
    app = QApplication([arg for arg in (argv or []) if arg != "--monitor"])
    app.setApplicationName("VerdictQuant")
    from pa_agent.gui.theme import apply_theme
    apply_theme(app)
    window = ManualMonitorWidget()
    window.show()
    return app.exec()

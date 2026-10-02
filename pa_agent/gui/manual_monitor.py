"""Open cached monitoring immediately; refresh only on an explicit button click."""
from __future__ import annotations

import copy
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

from PyQt6.QtCore import QTimer, QUrl, QSize, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QButtonGroup, QFrame, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from pa_agent.monitoring.service import MonitoringService, RefreshBusy
from pa_agent.gui.chatgpt_assistant import ChatGPTAssistantWidget
from pa_agent.gui.market_overview import MarketOverviewWidget
from pa_agent.gui.workbench_ui import apply_workbench_style, icon, label, toolbar_button


class ManualMonitorWidget(QWidget):
    def __init__(self, parent=None, service=None, research_service=None, chatgpt_client=None):
        super().__init__(parent)
        self.service = service
        self._executor = None
        self._future = None
        self._cancelled = threading.Event()
        self._market_result = None
        self._cache_read_error = False
        self.setObjectName("investmentWorkbench")
        self.setWindowTitle("VerdictQuant · 投资工作台")
        self.setMinimumSize(1060, 660)
        self.resize(1380, 900)
        apply_workbench_style(self)
        app = QApplication.instance()
        if app is not None and app.primaryScreen() is not None:
            available = app.primaryScreen().availableGeometry()
            self.resize(min(1380, max(1060, available.width() - 80)), min(900, max(660, available.height() - 70)))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("workbenchSidebar")
        sidebar.setFixedWidth(188)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(16, 26, 16, 20)
        navigation.setSpacing(7)
        brand = QHBoxLayout()
        brand.setSpacing(8)
        mark = label("VQ", "brandMark")
        mark.setFixedSize(33, 33)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(mark)
        brand_text = label("VerdictQuant", "brandName")
        brand_text.setStyleSheet("font-size:16px;font-weight:700")
        brand.addWidget(brand_text)
        navigation.addLayout(brand)
        navigation.addSpacing(28)
        navigation.addWidget(label("个人投资", "eyebrow"))
        navigation.addSpacing(4)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for index, (caption, symbol) in enumerate((("股票研究", "research"), ("基金与行情", "market"), ("AI 助手", "assistant"))):
            button = QPushButton(caption)
            button.setObjectName("sidebarButton")
            button.setIcon(icon(symbol))
            button.setIconSize(QSize(20, 20))
            button.setCheckable(True)
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            navigation.addWidget(button)
        navigation.addSpacing(17)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        navigation.addWidget(divider)
        navigation.addSpacing(10)
        self.open_plan_button = QPushButton("投资计划")
        self.open_plan_button.setObjectName("sidebarButton")
        self.open_plan_button.setIcon(icon("plan"))
        navigation.addWidget(self.open_plan_button)
        navigation.addStretch()
        self.open_model_button = QPushButton("分析设置")
        self.open_model_button.setObjectName("sidebarButton")
        self.open_model_button.setIcon(icon("settings"))
        navigation.addWidget(self.open_model_button)
        navigation.addSpacing(12)
        navigation.addWidget(label("本机保存 · 手动更新", "mutedLabel"))
        navigation.addWidget(label("研究与交易分开", "metricNote"))
        layout.addWidget(sidebar)
        self.tabs = QStackedWidget()
        from pa_agent.gui.installment_research import InstallmentResearchWidget
        self.installment = InstallmentResearchWidget(self, service=research_service, embedded=True)
        self.tabs.addWidget(self.installment)
        market_page = QWidget()
        market_layout = QVBoxLayout(market_page)
        market_layout.setContentsMargins(0, 0, 0, 12)
        market_layout.setSpacing(0)
        self.browser = MarketOverviewWidget(self)
        self.browser.body.layout().setContentsMargins(26, 23, 26, 16)
        self.browser.title.setStyleSheet("font-size:26px;font-weight:700;color:#182337")
        self.refresh_button = toolbar_button("刷新行情", "refresh", primary=True)
        self.refresh_button.clicked.connect(self.refresh_data)
        self.browser.header_layout.addWidget(self.refresh_button)
        market_layout.addWidget(self.browser, 1)
        self.status = label("本地保存结果 · 点击刷新才联网", "statusLabel", wrap=True)
        self.status.setContentsMargins(26, 0, 26, 0)
        market_layout.addWidget(self.status)
        self.tabs.addWidget(market_page)
        self.chatgpt_assistant = ChatGPTAssistantWidget(
            self, client=chatgpt_client,
            context_provider=getattr(self.installment, "public_chat_context", None),
            research_configurator=lambda model, effort: self.installment.configure_chatgpt_model(
                model, effort, self.chatgpt_assistant.profile_id),
        )
        self.tabs.addWidget(self.chatgpt_assistant)
        layout.addWidget(self.tabs, 1)
        self.nav_group.idClicked.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self._sync_navigation)
        self._sync_navigation(0)
        self.open_plan_button.clicked.connect(self.installment.edit_plan)
        self.open_model_button.clicked.connect(self.installment.edit_model)
        self.installment.refresh_state_changed.connect(self._research_busy)
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._finish_refresh)
        try:
            self._ensure_service()
            self._market_result = self.service.latest()
            self.browser.set_result(self._market_result)
        except Exception:
            self._market_result = None
            self.browser.set_result(None)
            self._cache_read_error = True
            self.status.setText("监控配置或保存结果无法校验，原文件已保留。请检查本机 paths.json 与保存结果后重试。")
        self._freshness_timer = QTimer(self)
        self._freshness_timer.setInterval(60_000)
        self._freshness_timer.timeout.connect(self._recheck_saved_data)
        self._freshness_timer.start()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _sync_navigation(self, index):
        for row, button in enumerate(self.nav_buttons):
            button.setChecked(row == index)
            button.setIcon(icon(("research", "market", "assistant")[row], "#2446d7" if row == index else "#657187"))

    def _research_busy(self, busy):
        self.open_plan_button.setEnabled(not busy)
        self.open_model_button.setEnabled(not busy)

    def _ensure_service(self):
        if self.service is None:
            self.service = MonitoringService()

    def _recheck_saved_data(self):
        if self._future is not None:
            return
        try:
            self._ensure_service()
            result = self.service.latest()
            if result != self._market_result:
                self.browser.set_result(result)
                self._market_result = result
            if self._cache_read_error:
                self.status.setText("本地保存结果已恢复读取；未联网刷新行情。")
            self._cache_read_error = False
        except Exception:
            stale = copy.deepcopy(self._market_result)
            if stale:
                for symbol, asset in stale["data"]["assets"].items():
                    asset["qualified"] = False
                    asset["errors"] = list(dict.fromkeys([*asset.get("errors", []), "SAVED_DATA_UNREADABLE"]))
                    stale["data"]["errors"][symbol] = asset["errors"]
                    if asset.get("quote"):
                        asset["quote"]["fresh"] = False
            self.browser.set_result(stale)
            self._market_result = stale
            self.status.setText("保存结果暂时无法校验；显示的旧行情仅供回看，原文件已保留。")
            self._cache_read_error = True

    def _open_source(self, url: QUrl):
        if url.scheme() == "https":
            QDesktopServices.openUrl(url)

    def refresh_data(self):
        if self._future is not None:
            return
        try:
            self._ensure_service()
        except (OSError, ValueError, KeyError, TypeError):
            self.status.setText("本地监控配置无法校验，请检查 paths.json 后重试；未联网、原文件已保留。")
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
            self.browser.set_result(result)
            self._market_result = result
            self._cache_read_error = False
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
            self.refresh_button.setText("刷新行情")

    def shutdown(self):
        self._cancelled.set()
        self._poll.stop()
        self._freshness_timer.stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if hasattr(self, "installment"):
            self.installment.shutdown()
        if hasattr(self, "chatgpt_assistant"):
            self.chatgpt_assistant.shutdown()

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

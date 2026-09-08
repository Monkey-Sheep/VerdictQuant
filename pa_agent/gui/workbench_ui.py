"""Scoped visual language for the personal investment workbench."""
from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

COLORS = {
    "background": "#f4f6f9", "surface": "#ffffff", "text": "#182337",
    "muted": "#657187", "border": "#e4e9f0", "brand": "#2446d7",
    "positive": "#16724c", "negative": "#b94b4a", "warning": "#9d6a1a",
}

_STYLE = """
QWidget {
    color: #182337; font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px; background: transparent;
}
QWidget#investmentWorkbench, QWidget#installmentResearch, QDialog { background: #f4f6f9; }
QWidget#workbenchSidebar { background: #ffffff; border-right: 1px solid #e4e9f0; }
QLabel { background: transparent; border: none; padding: 0; }
QLabel#brandName { font-size: 19px; font-weight: 700; color: #182337; }
QLabel#brandMark { background: #2446d7; color: white; border-radius: 10px; font-size: 17px; font-weight: 700; }
QLabel#pageTitle { font-size: 26px; font-weight: 700; color: #182337; }
QLabel#sectionTitle { font-size: 16px; font-weight: 700; color: #182337; }
QLabel#stockTitle { font-size: 23px; font-weight: 700; color: #182337; }
QLabel#eyebrow, QLabel#mutedLabel { color: #657187; font-size: 12px; }
QLabel#metricValue { font-size: 25px; font-weight: 600; color: #182337; }
QLabel#metricNote { color: #657187; font-size: 11px; }
QLabel#statusLabel { color: #657187; font-size: 11px; padding: 4px 0; }
QFrame#surfaceCard, QWidget#surfaceCard {
    background: #ffffff; border: 1px solid #e4e9f0; border-radius: 10px;
}
QFrame#metricCard { background: #ffffff; border: 1px solid #e4e9f0; border-radius: 10px; }
QFrame#noticeCard { background: #fff9ed; border: 1px solid #ead8b0; border-radius: 8px; }
QFrame#divider { background: #e4e9f0; border: none; max-height: 1px; }
QLabel#decisionBadge { border-radius: 6px; padding: 5px 9px; font-size: 12px; font-weight: 600; }
QLabel[tone="positive"] { color: #16724c; }
QLabel[tone="negative"] { color: #b94b4a; }
QLabel[tone="warning"] { color: #9d6a1a; }
QLabel[tone="brand"] { color: #2446d7; }
QLabel#decisionBadge[tone="positive"] { background: #eaf5ee; color: #16724c; }
QLabel#decisionBadge[tone="negative"] { background: #fceeed; color: #a13e3e; }
QLabel#decisionBadge[tone="warning"] { background: #fff3dc; color: #8a5b11; }
QLabel#decisionBadge[tone="brand"] { background: #edf1ff; color: #2446d7; }
QLabel#decisionBadge[tone="muted"] { background: #f0f3f7; color: #5d697b; }
QPushButton, QToolButton {
    background: #ffffff; color: #334155; border: 1px solid #dce3ec; border-radius: 7px;
    padding: 7px 13px; min-height: 23px; font-weight: 500;
}
QPushButton:hover, QToolButton:hover { background: #f2f5ff; border-color: #b8c6ed; color: #2446d7; }
QPushButton:pressed, QToolButton:pressed { background: #e8edfb; }
QPushButton:focus, QToolButton:focus { border: 2px solid #8097f0; padding: 6px 12px; }
QPushButton:disabled, QToolButton:disabled { color: #98a2b1; background: #f5f7fa; border-color: #e4e9f0; }
QPushButton#primaryButton { background: #2446d7; color: #ffffff; border: 1px solid #2446d7; font-weight: 600; }
QPushButton#primaryButton:hover { background: #1c39b5; border-color: #1c39b5; }
QPushButton#primaryButton:disabled { background: #9caee9; color: #ffffff; border-color: #9caee9; }
QPushButton#sidebarButton { text-align: left; border: 1px solid transparent; border-radius: 8px; padding: 11px 12px; color: #667387; background: transparent; }
QPushButton#sidebarButton:hover { background: #f4f6fb; color: #2446d7; }
QPushButton#sidebarButton:checked { background: #edf1ff; color: #2446d7; font-weight: 600; border: 1px solid #e4eafd; }
QPushButton#sidebarButton:focus { border: 1px solid #8097f0; }
QPushButton#textButton, QPushButton#disclosureButton { background: transparent; border: none; color: #657187; padding: 3px 0; text-align: left; }
QPushButton#textButton:hover, QPushButton#disclosureButton:hover { color: #2446d7; }
QPushButton#textButton:focus, QPushButton#disclosureButton:focus { border: 1px solid #8097f0; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #ffffff; color: #182337; border: 1px solid #dce3ec;
    border-radius: 7px; padding: 7px 9px; selection-background-color: #dce5ff;
    selection-color: #182337; min-height: 20px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #8097f0; }
QLineEdit:disabled, QComboBox:disabled { color: #8490a1; background: #f5f7fa; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { color: #182337; background: #ffffff; selection-background-color: #edf1ff; selection-color: #2446d7; border: 1px solid #dce3ec; padding: 5px; }
QTableWidget, QTableView, QListWidget {
    background: #ffffff; color: #182337; border: none;
    gridline-color: #eef1f5; alternate-background-color: #fafbfd;
    selection-background-color: #edf1ff; selection-color: #182337; outline: none;
}
QTableWidget::item, QTableView::item { padding: 8px 6px; border: none; border-bottom: 1px solid #eef1f5; color: #182337; }
QTableWidget::item:alternate, QTableView::item:alternate { background: #fafbfd; color: #182337; }
QTableWidget::item:selected, QTableView::item:selected { background: #edf1ff; color: #182337; }
QTableWidget::item:hover, QTableView::item:hover { background: #f5f7fc; }
QTableWidget::item:selected:hover, QTableView::item:selected:hover { background: #e6ecff; }
QHeaderView::section { background: #f7f9fc; color: #657187; font-size: 11px; font-weight: 500; border: none; border-bottom: 1px solid #e4e9f0; padding: 9px 6px; }
QTableCornerButton::section { background: #f7f9fc; border: none; }
QTextBrowser, QTextEdit { background: #ffffff; color: #182337; border: none; padding: 4px; selection-background-color: #dce5ff; selection-color: #182337; }
QTabWidget::pane { border: none; border-top: 1px solid #e4e9f0; background: #ffffff; top: -1px; }
QTabBar::tab { color: #657187; background: transparent; border: none; border-bottom: 2px solid transparent; padding: 11px 10px; margin: 0 3px 0 0; font-size: 12px; }
QTabBar::tab:selected { color: #2446d7; border-bottom: 2px solid #2446d7; font-weight: 600; }
QTabBar::tab:hover:!selected { color: #2446d7; background: #f7f9fc; }
QTabBar::tab:focus { background: #edf1ff; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 1px; }
QScrollBar::handle:vertical { background: #cbd3df; border-radius: 3px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #a9b5c6; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 1px; }
QScrollBar::handle:horizontal { background: #cbd3df; border-radius: 3px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QSplitter::handle { background: #e4e9f0; width: 1px; margin: 8px 0; }
QCheckBox { color: #334155; spacing: 8px; padding: 4px 0; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #b5c0d0; border-radius: 4px; background: white; }
QCheckBox::indicator:checked { background: #2446d7; border-color: #2446d7; }
QCheckBox::indicator:focus { border: 2px solid #8097f0; }
QProgressBar { border: none; background: #e8edf9; border-radius: 2px; max-height: 3px; color: transparent; }
QProgressBar::chunk { background: #2446d7; border-radius: 2px; }
QGroupBox { border: 1px solid #e4e9f0; background: white; border-radius: 9px; margin-top: 16px; padding: 16px 12px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #334155; }
QMenu { background: #ffffff; color: #182337; border: 1px solid #dce3ec; padding: 4px; }
QMenu::item { padding: 8px 20px; }
QMenu::item:selected { background: #edf1ff; color: #2446d7; }
QToolTip { color: #334155; background: #ffffff; border: 1px solid #cbd3df; padding: 6px; }
"""


def apply_workbench_style(widget: QWidget) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(_STYLE)


def label(text: str = "", role: str = "", *, wrap: bool = False) -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(wrap)
    if role:
        result.setObjectName(role)
    return result


def set_tone(widget: QWidget, tone: str) -> None:
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—", note: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(17, 13, 17, 13)
        layout.setSpacing(5)
        self.caption = label(title, "eyebrow")
        self.value = label(value, "metricValue")
        self.note = label(note, "metricNote", wrap=True)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        layout.addWidget(self.note)

    def update_value(self, value: str, note: str = "", tone: str = "") -> None:
        self.value.setText(value)
        self.note.setText(note)
        set_tone(self.value, tone)


class Disclosure(QWidget):
    """Keyboard-accessible progressive disclosure, with no data side effects."""
    def __init__(self, title: str, content: QWidget, parent=None):
        super().__init__(parent)
        self.content = content
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.button = QPushButton("›  " + title)
        self.button.setObjectName("disclosureButton")
        self.button.setCheckable(True)
        self.button.setAccessibleName(title)
        self.button.toggled.connect(self._toggle)
        layout.addWidget(self.button)
        layout.addWidget(content)
        content.hide()

    def _toggle(self, expanded: bool) -> None:
        self.button.setText(("⌄  " if expanded else "›  ") + self._title)
        self.content.setVisible(expanded)


class FitTextBrowser(QTextBrowser):
    """Let an outer page scroll; never hide the opening summary in an inner scroll."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_height)
        self.document().documentLayout().documentSizeChanged.connect(lambda _: self._fit_timer.start(0))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_timer.start(0)

    def _fit_height(self):
        height = max(50, int(self.document().size().height()) + 10)
        if height != self.height():
            self.setFixedHeight(height)


def format_time(value, *, short: bool = False) -> str:
    if not value:
        return "待更新"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return parsed.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)


def document_html(content: str) -> str:
    return ("<html><head><style>body {font-family:'Microsoft YaHei UI','Segoe UI'; font-size:13px; color:#334155;}"
            "h2 {font-size:19px; color:#182337; margin-top:12px; margin-bottom:12px;}"
            "h3 {font-size:14px; color:#182337; margin-top:19px; margin-bottom:9px;}"
            "p {margin-top:8px; margin-bottom:12px;} li {margin-bottom:8px;}"
            "th {background-color:#f3f6fa; color:#667387; font-size:11px; font-weight:normal;}"
            "td {border-bottom:1px solid #e4e9f0;} a {color:#2446d7; text-decoration:none;}"
            "</style></head><body>" + content + "</body></html>")


def empty_html(title: str, message: str) -> str:
    return document_html(f"<h2>{escape(title)}</h2><p>{escape(message)}</p>")


def icon(name: str, color: str = "#657187") -> QIcon:
    """Small code-native line icons, rendered at 2x for Windows scaling."""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2, 2)
    painter.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name == "research":
        painter.drawLine(3, 16, 17, 16)
        painter.drawLine(5, 12, 8, 8)
        painter.drawLine(8, 8, 11, 10)
        painter.drawLine(11, 10, 16, 4)
    elif name == "market":
        painter.drawRoundedRect(3, 3, 14, 14, 2, 2)
        painter.drawLine(6, 13, 6, 10)
        painter.drawLine(10, 13, 10, 6)
        painter.drawLine(14, 13, 14, 8)
    elif name == "plan":
        painter.drawRoundedRect(4, 3, 12, 14, 2, 2)
        for y in (7, 10, 13):
            painter.drawLine(7, y, 13, y)
    elif name == "settings":
        for x, y in ((5, 7), (10, 12), (15, 8)):
            painter.drawLine(x, 3, x, 17)
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(x - 2, y - 2, 4, 4)
    elif name == "refresh":
        painter.drawArc(3, 3, 14, 14, 35 * 16, 275 * 16)
        painter.drawLine(17, 3, 17, 8)
        painter.drawLine(17, 8, 12, 8)
    elif name == "history":
        painter.drawEllipse(3, 3, 14, 14)
        painter.drawLine(10, 6, 10, 10)
        painter.drawLine(10, 10, 13, 12)
    else:
        painter.drawEllipse(4, 4, 12, 12)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def toolbar_button(text: str, icon_name: str, *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setIcon(icon(icon_name, "#ffffff" if primary else "#657187"))
    button.setIconSize(QSize(18, 18))
    if primary:
        button.setObjectName("primaryButton")
    return button

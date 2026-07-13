from __future__ import annotations

import inspect

from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings


class _DeferredSource:
    def __init__(self) -> None:
        self._connected = False
        self._symbol = ""
        self._timeframe = ""
        self.connect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""

    def list_symbols(self) -> list[str]:
        return []

    def supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "1h", "4h", "1d"]


def test_gui_entry_bootstraps_without_market_connection() -> None:
    from pa_agent.main import main

    assert "AppContext.bootstrap(connect_data_source=False)" in inspect.getsource(main)


def test_window_show_is_non_blocking_and_connects_only_on_demand(qtbot) -> None:
    from pa_agent.gui.main_window import MainWindow

    source = _DeferredSource()
    window = MainWindow(AppContext(settings=Settings(), data_source=source))
    qtbot.addWidget(window)

    window.show()
    qtbot.wait(50)

    assert source.connect_calls == 0
    assert window._connect_current_data_source() is True
    assert source.connect_calls == 1
    assert source._connected is True

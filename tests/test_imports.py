import importlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CORE = ["clipper.sysutil", "clipper.settings", "clipper.edit", "clipper.moments", "clipper.setup", "clipper.pipeline"]
UI = ["clipper.qt.theme", "clipper.qt.widgets", "clipper.qt.dialogs", "clipper.qt.extras", "clipper.qt.app"]


@pytest.mark.parametrize("name", CORE)
def test_core_module_imports(name):
    importlib.import_module(name)


@pytest.mark.parametrize("name", UI)
def test_ui_module_imports(name):
    pytest.importorskip("PySide6.QtWidgets")
    importlib.import_module(name)


def test_main_window_builds():
    """Builds the whole window offscreen, so a broken page or a bad wiring edit fails here instead of at launch."""
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from clipper.qt import theme
    from clipper.qt.app import MainWindow
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    win = MainWindow()
    assert {"Progress", "Setup", "AI picker", "YouTube"} <= set(win.pages)

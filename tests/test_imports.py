import importlib

import pytest

CORE = ["clipper.sysutil", "clipper.settings", "clipper.edit", "clipper.moments", "clipper.setup", "clipper.pipeline"]
UI = ["clipper.ui.theme", "clipper.ui.widgets", "clipper.ui.dialogs", "clipper.ui.app"]


@pytest.mark.parametrize("name", CORE)
def test_core_module_imports(name):
    importlib.import_module(name)


@pytest.mark.parametrize("name", UI)
def test_ui_module_imports(name):
    pytest.importorskip("tkinter")
    importlib.import_module(name)

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication, QMessageBox

from clipper.qt.dialogs import ReviewDialog

APP = QApplication.instance() or QApplication([])


def make():
    clips = [{"start": 0.0, "end": 25.0, "title": "T", "description": "D", "use": True},
             {"start": 30.0, "end": 55.0, "title": "U", "description": "E", "use": True}]
    holder = {"ev": threading.Event(), "res": None, "duration": 100.0, "min_len": 20, "max_len": 40,
              "segs": [{"start": 0.0, "end": 10.0, "text": "hello world"}]}
    return ReviewDialog(None, clips, "v.mp4", holder), holder


def test_review_saves_title_and_caption_edits():
    dlg, holder = make()
    dlg.t.setText("New title"); dlg.cap.setPlainText("hello brave world")
    dlg.accept()
    res = holder["res"]
    assert holder["ev"].is_set() and res[0]["title"] == "New title"
    assert res[0]["cap_edit"]["text"] == "hello brave world"


def test_review_unchanged_caption_stores_no_edit():
    dlg, holder = make()
    dlg.accept()
    assert "cap_edit" not in holder["res"][0]


def test_review_rejects_bad_times(monkeypatch):
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    dlg, holder = make()
    dlg.s.setText("abc"); dlg.accept()
    assert not holder["ev"].is_set()


def test_review_toggle_row_unticks_a_clip():
    dlg, holder = make()
    dlg.toggle_row(1); dlg.accept()
    assert holder["res"][1]["use"] is False

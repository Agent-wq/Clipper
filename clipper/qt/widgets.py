from PySide6.QtCore import Property, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton, QComboBox, QDoubleSpinBox, QSpinBox
from clipper.qt.theme import ACC, KNOB_OFF, TRACK


def mix(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(*(round(x + (y - x) * t) for x, y in ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))))


class Switch(QAbstractButton):
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True); self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor); self.setFixedSize(44, 24)
        self._k = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self); self._anim.setDuration(130)
        self.toggled.connect(self._go)

    def _go(self, on):
        self._anim.stop(); self._anim.setStartValue(self._k); self._anim.setEndValue(1.0 if on else 0.0); self._anim.start()

    def get_knob(self): return self._k
    def set_knob(self, v): self._k = v; self.update()
    knob = Property(float, get_knob, set_knob)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.setPen(Qt.NoPen)
        p.setBrush(mix(TRACK, ACC, self._k)); p.drawRoundedRect(self.rect(), 12, 12)
        d = self.height() - 6
        p.setBrush(mix(KNOB_OFF, "#ffffff", self._k)); p.drawEllipse(QRectF(3 + self._k * (self.width() - d - 6), 3, d, d))


def _nowheel(base):
    """The mouse wheel scrolls the page unless the field has focus, so scrolling never changes a setting by accident."""
    class W(base):
        def __init__(self, *a):
            super().__init__(*a); self.setFocusPolicy(Qt.StrongFocus)
        def wheelEvent(self, e):
            super().wheelEvent(e) if self.hasFocus() else e.ignore()
    return W


Combo, Spin, DSpin = _nowheel(QComboBox), _nowheel(QSpinBox), _nowheel(QDoubleSpinBox)

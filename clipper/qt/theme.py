from string import Template
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from clipper.sysutil import WIN

BG, SIDE, PANEL, FIELD, BORDER, HOVER = "#0a0a0a", "#0f0f0f", "#141414", "#1c1c1c", "#262626", "#1a1a1a"
FG, MUTED, ACC, ACC_HI, ACC_FG = "#f2f2f2", "#8a8a8a", "#ff6a3d", "#ff8560", "#0a0a0a"
DANGER, SUCCESS, TRACK, KNOB_OFF = "#ff4d4d", "#4ade80", "#333333", "#9a9a9a"
COLORS = dict(BG=BG, SIDE=SIDE, PANEL=PANEL, FIELD=FIELD, BORDER=BORDER, HOVER=HOVER, FG=FG, MUTED=MUTED,
              ACC=ACC, ACC_HI=ACC_HI, ACC_FG=ACC_FG)

SUBTITLES = {
    "Source": "Where the video comes from and how it is downloaded.",
    "Transcribe": "Turns speech into text with Whisper. The GPU is used when available.",
    "AI picker": "A local Ollama model chooses the best moments.",
    "Growth": "Finds the moments most likely to hold attention and writes the title, caption, hashtags and cover.",
    "Clips": "How many clips to make and how long they are.",
    "Captions": "Subtitles burned into the video.",
    "Split screen": "Face cam enlarged on one half, main content on the other, like a streamer short.",
    "Watermark": "A small credit for the original creator, shown on every clip.",
    "Editing": "The AI plans zooms, animated captions, text and sound for each clip, then renders it finished.",
    "Video": "Shape, quality and encoder of the finished clips.",
    "Output": "Where files are saved and what gets saved.",
    "YouTube": "Upload finished clips to your YouTube channel. The title, description and hashtags are filled from the AI picker.",
}

QSS = Template("""
QWidget#root { background: $BG; }
QLabel { background: transparent; }
QLabel#title { font-size: 21pt; font-weight: 700; }
QLabel#subtitle, QLabel#hint { color: $MUTED; }
QLabel#hint { font-size: 9pt; }
QLabel#section { color: $MUTED; font-size: 8pt; font-weight: 700; }
QLabel#brand { font-size: 17pt; font-weight: 700; }
QLabel#status { font-size: 16pt; font-weight: 700; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

QFrame#side { background: $SIDE; border-right: 1px solid $BORDER; }
QFrame#card { background: $PANEL; border: 1px solid $BORDER; border-radius: 12px; }
QFrame#divider { background: $BORDER; border: none; }

QPushButton { background: $FIELD; border: 1px solid $BORDER; border-radius: 8px; padding: 8px 18px; }
QPushButton:hover { background: #262626; }
QPushButton:disabled { background: $PANEL; color: #555555; }
QPushButton#primary { background: $ACC; border: 1px solid $ACC; color: $ACC_FG; font-weight: 700; }
QPushButton#primary:hover { background: $ACC_HI; border-color: $ACC_HI; }
QPushButton#primary:disabled { background: #2a2a2a; border-color: #2a2a2a; color: #6a6a6a; }
QPushButton#nav { text-align: left; padding: 9px 16px; border: none; border-left: 3px solid transparent;
                  border-radius: 0; background: transparent; color: $MUTED; }
QPushButton#nav:hover { background: $HOVER; color: $FG; }
QPushButton#nav:checked { background: $HOVER; color: $FG; border-left: 3px solid $ACC; }

QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: $FIELD; border: 1px solid $BORDER; border-radius: 8px; padding: 7px 10px;
    selection-background-color: $ACC; selection-color: $ACC_FG; }
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 1px solid $ACC; }
QComboBox QAbstractItemView { background: $FIELD; border: 1px solid $BORDER; outline: 0;
                              selection-background-color: $ACC; selection-color: $ACC_FG; }
QPlainTextEdit#log { background: $PANEL; border-radius: 12px; padding: 12px; }

QProgressBar { background: $FIELD; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: $ACC; border-radius: 3px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2c2c2c; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3a3a3a; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid $BORDER; border-radius: 4px; background: $FIELD; }
QCheckBox::indicator:checked { background: $ACC; border-color: $ACC; }
QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid $BORDER; border-radius: 8px; background: $FIELD; }
QRadioButton::indicator:checked { background: $ACC; border-color: $ACC; }
QTableWidget { background: $FIELD; border: 1px solid $BORDER; border-radius: 8px; outline: 0; }
QTableWidget::item { padding: 6px 8px; border: none; }
QTableWidget::item:selected { background: #2a2a2a; color: $FG; }
QHeaderView::section { background: $SIDE; color: $MUTED; border: none; padding: 8px 9px; font-weight: 700; font-size: 9pt; }
""").substitute(COLORS)


def apply(app):
    app.setStyle("Fusion")
    fams = set(QFontDatabase.families())
    for f in (("Segoe UI",) if WIN else ()) + ("Inter", "Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans"):
        if f in fams:
            app.setFont(QFont(f, 10)); break
    pal = QPalette()
    for role, col in ((QPalette.Window, BG), (QPalette.WindowText, FG), (QPalette.Base, FIELD), (QPalette.AlternateBase, PANEL),
                      (QPalette.Text, FG), (QPalette.Button, FIELD), (QPalette.ButtonText, FG), (QPalette.Highlight, ACC),
                      (QPalette.HighlightedText, ACC_FG), (QPalette.ToolTipBase, FIELD), (QPalette.ToolTipText, FG),
                      (QPalette.PlaceholderText, MUTED)):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(QSS)

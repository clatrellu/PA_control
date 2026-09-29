"""Entry point — run with: uv run python main.py [--mock] [--scope {redpitaya,picoscope}]"""
import sys
import argparse
#import logging

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import Qt

from gui.main_window import MainWindow

# Temporary diagnostic logging — checking capture timing after the
# TCP_QUICKACK fix. Remove once confirmed.
#logging.basicConfig(level=logging.DEBUG, format="%(message)s")


def _apply_dark_palette(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Base,            QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(50, 50, 50))
    palette.setColor(QPalette.ColorRole.Text,            QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Button,          QColor(60, 60, 60))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Link,            QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor(50, 50, 50))
    palette.setColor(QPalette.ColorRole.ToolTipText,     QColor(220, 220, 220))
    app.setPalette(palette)


def main() -> None:
    parser = argparse.ArgumentParser(description="PA Setup Control GUI")
    parser.add_argument(
        "--mock", action="store_true",
        help="Use simulated hardware (no instruments needed)",
    )
    parser.add_argument(
        "--scope", choices=["redpitaya", "picoscope"], default="redpitaya",
        help="Oscilloscope model to use (default: redpitaya)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("PA Setup Control")
    app.setStyle("Fusion")
    _apply_dark_palette(app)

    window = MainWindow(mock=args.mock, scope_type=args.scope)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

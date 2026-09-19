"""Flower's entry point: one window, one page, no navigation.

The taskbar identity is claimed before any window exists, because Windows groups
taskbar buttons by AppUserModelID and a source-run Python process otherwise
inherits python.exe's.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon, FluentWindow

from flower.config import Settings
from flower.gui.page import MainPage

APP_ID = "Offblink.Flower"  # the taskbar identity, claimed before any window exists
ICON = "assets/flower.ico"
WINDOW_SIZE = (820, 500)


def resource_path(*parts: str) -> Path:
    """A bundled file: unpacked by PyInstaller when frozen, beside the source otherwise."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base).joinpath(*parts)


def claim_taskbar_identity() -> None:
    with contextlib.suppress(Exception):  # not fatal: only the taskbar button suffers
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


class FlowerWindow(FluentWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.setWindowTitle("Flower")
        self.resize(*WINDOW_SIZE)
        self.page = MainPage(settings, self)
        self.addSubInterface(self.page, FluentIcon.DOWNLOAD, "下载")
        self.navigationInterface.hide()  # one page: the sidebar would be a lie

    def closeEvent(self, event) -> None:
        self.page.shutdown()
        super().closeEvent(event)


def main() -> int:
    claim_taskbar_identity()
    app = QApplication(sys.argv)
    icon = QIcon(str(resource_path(ICON)))
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = FlowerWindow(Settings.load())
    window.setWindowIcon(app.windowIcon())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

"""Flower's entry point: one window, one page, no navigation.

The taskbar identity is claimed before any window exists, because Windows groups
taskbar buttons by AppUserModelID and a source-run Python process otherwise
inherits python.exe's.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys

from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon, FluentWindow

from flower.config import Settings
from flower.gui.page import MainPage

APP_ID = "Offblink.Flower"
WINDOW_SIZE = (820, 500)


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
    window = FlowerWindow(Settings.load())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

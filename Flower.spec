# -*- mode: python ; coding: utf-8 -*-
"""Flower 的打包描述：一只窗口 exe + 一个 _internal。

单面程序，所以没有多 exe 那套：一张脸、一枚图标、一枚 AUMID。
命令行那一半（`flower/cli.py`）不出 exe —— 它长在有 Python 的地方，
源码里 `python -m flower` 就跑，装过的话 `flower` 也能用。
datas 里带 pyproject.toml（版本号的唯一出处）与 assets/flower.ico（窗口/任务栏图标）。
"""

from pathlib import Path

ROOT = Path(SPECPATH)
ICON = ROOT / "assets" / "flower.ico"

shared = {
    "pathex": [str(ROOT)],
    "datas": [("pyproject.toml", "."), ("assets/flower.ico", "assets")],
    "excludes": [
        # The shared-site-packages dev venv drags these into the analysis (one run had
        # hooks for torch and tensorflow fire), and PyInstaller refuses to freeze two Qt
        # bindings at once when the PyQt5 flavour of qfluentwidgets is also visible.
        # None of them is imported by Flower; in a clean build venv they are no-ops.
        "torch",
        "tensorflow",
        "zmq",
        "orjson",
        "pygame",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "shiboken2",
        # The pyi_rth_pkgres runtime hook needs jaraco.text and dies without it, and
        # nothing in Flower imports any of this.
        "pkg_resources",
        "setuptools",
        "jaraco",
        "pip",
        "_distutils_hack",
    ],
    "noarchive": False,
}

app = Analysis(["app.py"], **shared)
pyz = PYZ(app.pure)
exe = EXE(
    pyz,
    app.scripts,
    [],
    exclude_binaries=True,
    name="Flower",
    console=False,  # windowed: nothing reads this process's stdout
    icon=str(ICON),
)
coll = COLLECT(exe, app.binaries, app.datas, name="Flower", upx=False)

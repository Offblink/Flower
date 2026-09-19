"""打包 Flower 并自检 —— 不信 PyInstaller 的退出码。

**在 `.venv-build` 里构建，不是当前解释器**：开发用的 `.venv` 共享全局 site-packages
（那里有 PyQt5 版 qfluentwidgets、torch、tensorflow），PyInstaller 会把它们拖进分析图 ——
一次实测里 `hook-torch` 与 tensorflow 都被触发，包会因此膨胀到不可接受。
`.venv-build` 只装了 PySide6 + PySide6-Fluent-Widgets + PyInstaller，所以图上只有该有的东西。

自检三件事（每条都真的抓到过问题）：`Copying icon to EXE` 少一次＝图标没进去；窗口 exe 没有
stdout、import 一崩就静默死掉（活着才算过）；包大小要是超了说明又混进了别的东西。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_PYTHON = ROOT / ".venv-build" / "Scripts" / "python.exe"
DIST = ROOT / "dist" / "Flower"
EXE = DIST / "Flower.exe"
ALIVE_SECONDS = 6
SANE_MB = 160  # PySide6 + fluent 大约 90–120 MB；再多就是拖进了别的东西


def main() -> int:
    if not BUILD_PYTHON.is_file():
        print(
            "missing the build venv. Create it (it shares nothing with .venv):\n"
            f'  "$BASE" -m venv --without-pip "{ROOT / ".venv-build"}"\n'
            f'  "$BASE" -m pip --python "{BUILD_PYTHON}" install '
            '"PySide6==6.10.2" "PySide6-Fluent-Widgets==1.11.3" "pyinstaller==6.17.0"'
        )
        return 2

    for stale in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(stale, ignore_errors=True)
    log = subprocess.run(
        [
            str(BUILD_PYTHON),
            "-m",
            "PyInstaller",
            "Flower.spec",
            "--noconfirm",
            "--distpath",
            "dist",
            "--workpath",
            "build",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    print("\n".join(log.stdout.splitlines()[-8:]))
    if log.returncode != 0:
        print(log.stderr[-2000:])
        return log.returncode

    # PyInstaller's INFO lines go to stderr; the banner Qt prints on import goes to stdout.
    said = log.stdout + log.stderr
    hits = len(re.findall(r"Copying icon to EXE", said))
    assert hits == 1, f"the icon was embedded {hits} times (expected 1)"
    assert EXE.is_file(), f"missing {EXE}"
    for other in ("hook-torch", "hook-tensorflow", "hook-PyQt5"):
        assert other not in said, f"{other} ran: something leaked into the bundle"

    mb = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    assert mb < SANE_MB, f"the bundle is {mb:.0f} MB: something unexpected got collected"

    # The windowed exe has no stdout: if an import fails the traceback goes nowhere and
    # the process just dies. Still running after a few seconds is the only signal.
    process = subprocess.Popen([str(EXE)], cwd=str(DIST))
    time.sleep(ALIVE_SECONDS)
    alive = process.poll() is None
    if alive:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False
        )
    assert alive, "the windowed exe died on startup (no stdout to explain it)"

    print(f"OK  {EXE}  ({mb:.1f} MB in {DIST})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

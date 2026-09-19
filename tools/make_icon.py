"""画出 Flower 的图标：一条流分成四条。

每档单独渲染（16/24/32/48/64/128/256），不是把大图缩下来 —— 24 px 以下换简化画法
（四条支流减成两条、间距放大），否则小尺寸里四条会糊成一块。

用法：python tools/make_icon.py     → assets/flower.ico + assets/flower-256.png
注意：必须在真实桌面平台渲染（offscreen 平台什么都画不出来）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter

SIZES = (16, 24, 32, 48, 64, 128, 256)
ASSETS = Path(__file__).resolve().parent.parent / "assets"
ACCENT_LIGHT = "#2dd4bf"
ACCENT_DARK = "#0f766e"


def render(size: int) -> QImage:
    """One size, drawn at that size: rounded tile behind, the split stream on top."""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    inset = size * (0.06 if size > 24 else 0.03)
    tile = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    gradient = QLinearGradient(tile.topLeft(), tile.bottomRight())
    gradient.setColorAt(0.0, QColor(ACCENT_LIGHT))
    gradient.setColorAt(1.0, QColor(ACCENT_DARK))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    radius = size * 0.22
    painter.drawRoundedRect(tile, radius, radius)

    painter.setBrush(QColor("#ffffff"))
    small = size <= 24
    lane_width = tile.width() * (0.17 if small else 0.09)
    gap = tile.width() * (0.10 if small else 0.05)
    lanes = 2 if small else 4
    span = lanes * lane_width + (lanes - 1) * gap
    left = tile.center().x() - span / 2

    trunk_width = tile.width() * (0.15 if small else 0.13)
    painter.drawRect(
        QRectF(
            tile.center().x() - trunk_width / 2,
            tile.top() + tile.height() * 0.16,
            trunk_width,
            tile.height() * 0.36,
        )
    )
    # The manifold is what makes it read as one stream splitting rather than as a
    # colon: without it the trunk and the lanes never touch.
    painter.drawRect(QRectF(left, tile.top() + tile.height() * 0.50, span, tile.height() * 0.10))
    for index in range(lanes):
        painter.drawRect(
            QRectF(
                left + index * (lane_width + gap),
                tile.top() + tile.height() * 0.58,
                lane_width,
                tile.height() * 0.26,
            )
        )
    painter.end()
    return image


def to_pillow(image: QImage) -> Image.Image:
    buffer = image.constBits().tobytes()
    return Image.frombytes("RGBA", (image.width(), image.height()), buffer, "raw", "BGRA")


def main() -> int:
    QGuiApplication(sys.argv)  # the real platform: offscreen renders nothing at all
    ASSETS.mkdir(parents=True, exist_ok=True)
    frames = {size: to_pillow(render(size)) for size in SIZES}
    icon_path = ASSETS / "flower.ico"
    frames[256].save(
        icon_path,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=[frames[size] for size in SIZES if size != 256],
    )
    frames[256].save(ASSETS / "flower-256.png")
    with Image.open(icon_path) as written:
        print(f"{icon_path} sizes={sorted(written.ico.sizes())}")
    print(f"{ASSETS / 'flower-256.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

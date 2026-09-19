"""画出 Flower 的图标：🌷 本身（用户点名的字形，不带底）。

彩色 emoji 只有 Qt6/PySide6 渲得出来（Qt5 只能得单色轮廓），而且必须在真实桌面平台上渲染。
每档单独渲染（16/24/32/48/64/128/256），不是把大图缩下来；emoji 先在 1024 上画好、按 ink bbox
裁掉字形自带边距，再按每档的填充率缩进去 —— 小尺寸填充率给大一点，否则 16 px 里看不出来。

底色一开始垫了应用自己的青绿渐变，用户看后否掉：**不要背景**（绿底又丑、和粉花对比度也低）。
所以这张 .ico 是透明底的纯字形，跟 Fungi 那枚 🍄 一样。

用法：python tools/make_icon.py     → assets/flower.ico + assets/flower-256.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QPainter

SIZES = (16, 24, 32, 48, 64, 128, 256)
ASSETS = Path(__file__).resolve().parent.parent / "assets"
EMOJI = "\U0001f337"  # 🌷
EMOJI_FONT = "Segoe UI Emoji"
CANVAS = 1024  # the emoji is drawn once, big, then cropped to its ink


def glyph() -> QImage:
    """The tulip on its own, cropped to the pixels that are actually painted."""
    font = QFont(EMOJI_FONT)
    font.setPixelSize(int(CANVAS * 0.72))
    image = QImage(CANVAS, CANVAS, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setFont(font)
    painter.setPen(QColor("#000000"))
    box = QFontMetricsF(font).boundingRect(EMOJI)
    painter.drawText(
        QRectF(0, (CANVAS - box.height()) / 2, CANVAS, box.height()),
        Qt.AlignCenter,
        EMOJI,
    )
    painter.end()
    return crop_to_ink(image)


def crop_to_ink(image: QImage) -> QImage:
    """Trim the font's own margins: an emoji's box is not its glyph, and the padding
    differs per glyph, so scaling by the canvas would leave the mark off-centre."""
    box = to_pillow(image).getbbox()
    if box is None:
        return image
    left, top, right, bottom = box
    return image.copy(left, top, right - left, bottom - top)


def render(size: int, source: QImage) -> QImage:
    """One size, drawn at that size: the tulip scaled to sit inside the square."""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    fill = 0.90 if size > 24 else 0.96  # small sizes need every pixel they can get
    side = size * fill
    ratio = source.width() / source.height()
    if ratio >= 1:  # wider than tall: the width is what has to fit
        width, height = side, side / ratio
    else:  # taller than wide (the tulip is): the height is
        width, height = side * ratio, side
    target = QRectF(size / 2 - width / 2, size / 2 - height / 2, width, height)
    painter.drawImage(target, source)
    painter.end()
    return image


def to_pillow(image: QImage) -> Image.Image:
    buffer = image.constBits().tobytes()
    return Image.frombytes("RGBA", (image.width(), image.height()), buffer, "raw", "BGRA")


def main() -> int:
    QGuiApplication(sys.argv)  # the real platform: offscreen renders no emoji at all
    ASSETS.mkdir(parents=True, exist_ok=True)
    source = glyph()
    frames = {size: to_pillow(render(size, source)) for size in SIZES}
    colours = len(set(frames[256].getdata()))
    print(f"tulip ink {source.width()}x{source.height()}, distinct colours at 256: {colours}")
    assert colours > 200, "the emoji came out flat: this platform cannot render colour fonts"

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
    return 0


if __name__ == "__main__":
    sys.exit(main())

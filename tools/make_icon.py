"""Draw the app icon with Qt and save it as a multi-size assets/icon.ico.

    python tools/make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPolygonF

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 256, size / 256)

    # Background tile
    grad = QLinearGradient(0, 0, 256, 256)
    grad.setColorAt(0, QColor("#ff8a5c"))
    grad.setColorAt(1, QColor("#d9415f"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 52, 52)

    # Two clips: the back one faded, the front one solid with a play button
    p.setBrush(QColor(255, 255, 255, 110))
    p.drawRoundedRect(QRectF(40, 58, 116, 92), 16, 16)
    p.setBrush(QColor("white"))
    p.drawRoundedRect(QRectF(100, 106, 116, 92), 16, 16)
    p.setBrush(QColor("#d9415f"))
    p.drawPolygon(QPolygonF([QPointF(146, 130), QPointF(146, 174), QPointF(182, 152)]))
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def main() -> int:
    QGuiApplication(sys.argv)
    images = [png_bytes(draw(s)) for s in SIZES]
    # ICO file: header, one directory entry per size, then PNG data.
    header = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = 6 + 16 * len(SIZES)
    entries = b""
    for size, data in zip(SIZES, images):
        dim = 0 if size >= 256 else size      # 0 means 256 in ICO
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(header + entries + b"".join(images))
    draw(256).save(str(OUT.with_suffix(".png")))
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

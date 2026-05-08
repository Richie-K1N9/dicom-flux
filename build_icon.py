#!/usr/bin/env python3
"""Render assets/dicom_flux.svg to a multi-resolution Windows .ico.

Run once before bundling with PyInstaller; the resulting .ico is referenced
via the `--icon` flag for Windows builds.

    python build_icon.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_svg_to_png(svg_path: Path, size: int) -> bytes:
    renderer = QSvgRenderer(str(svg_path))
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def build_ico(svg_path: Path, ico_path: Path, sizes=ICON_SIZES) -> None:
    images = [(s, render_svg_to_png(svg_path, s)) for s in sizes]
    n = len(images)

    # ICONDIR (6 bytes): reserved=0, type=1 (icon), count
    header = struct.pack("<HHH", 0, 1, n)

    # ICONDIRENTRY (16 bytes per image), then image payloads.
    entries = b""
    payloads = b""
    offset = 6 + 16 * n

    for size, data in images:
        # In ICO format, 0 in the width/height byte means 256.
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entry = struct.pack(
            "<BBBBHHII",
            w, h,
            0,    # color count (0 for >= 256 colors)
            0,    # reserved
            1,    # color planes
            32,   # bits per pixel
            len(data),
            offset,
        )
        entries += entry
        payloads += data
        offset += len(data)

    ico_path.write_bytes(header + entries + payloads)


def main() -> int:
    here = Path(__file__).resolve().parent
    svg = here / "assets" / "dicom_flux.svg"
    ico = here / "assets" / "dicom_flux.ico"
    if not svg.exists():
        print(f"ERROR: {svg} not found", file=sys.stderr)
        return 1
    QApplication(sys.argv)  # required for QImage / QSvgRenderer
    build_ico(svg, ico)
    sizes = ", ".join(str(s) for s in ICON_SIZES)
    print(f"Wrote {ico} ({sizes})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

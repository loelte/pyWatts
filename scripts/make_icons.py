"""Regenerate icon.png and icon.ico from icon.svg. Run after editing the SVG."""

import struct
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parents[1] / "src" / "pywatts" / "ui" / "assets"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_png(svg: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    svg.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"cannot encode {size} px PNG")
    return bytes(buffer.data())


def build_ico(images: dict[int, bytes]) -> bytes:
    """ICO container with PNG entries (Windows Vista and later)."""
    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    for size, png in images.items():
        side = size % 256  # 0 means 256
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return directory + b"".join(images.values())


def main() -> None:
    app = QGuiApplication(["make_icons", "-platform", "offscreen"])  # noqa: F841
    svg = QSvgRenderer(str(ASSETS / "icon.svg"))
    if not svg.isValid():
        raise SystemExit("icon.svg is not valid")
    (ASSETS / "icon.png").write_bytes(render_png(svg, 256))
    (ASSETS / "icon.ico").write_bytes(build_ico({s: render_png(svg, s) for s in ICO_SIZES}))


if __name__ == "__main__":
    main()

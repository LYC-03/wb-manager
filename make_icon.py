"""生成 wb-manager 的图标与 logo。

图形：蓝底圆角方块 + 三条左对齐的白条，长度与粗细依次递减。
表达的是这个工具干的事——把长短不齐的会话名梳理成有秩序的列表。

三处用到同一套几何参数，改一处就得三处一起改：
  1. docs/icon.ico  给 exe / 桌面快捷方式（多尺寸，16px 单独简化）
  2. docs/icon.png  给 README / 文档
  3. docs/logo.svg  给界面头部内联引用

16px 单独画：三条线在这个尺寸会糊成一团白噪点，所以只留两条。
"""
import struct
from pathlib import Path

from PIL import Image, ImageDraw

BLUE = (77, 128, 239, 255)   # WorkBuddy brand-blue #4d80ef
WHITE = (255, 255, 255, 255)
SS = 4                      # 超采样倍数

# 几何参数（相对 1.0）
RADIUS = 0.2237             # 圆角，iOS squircle 比例
LEFT = 0.222                # 三条线的左端基准（算过视觉重心，落在画布中线附近）
BARS = (                    # (长度, 粗细, 中心 y)
    (0.596, 0.126, 0.315),
    (0.528, 0.108, 0.500),
    (0.460, 0.090, 0.685),
)
BARS_SIMPLE = ((0.60, 0.165, 0.375), (0.54, 0.165, 0.625))


def render(size: int, simple: bool = False) -> Image.Image:
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * RADIUS), fill=BLUE)
    for ln, hh, cy in (BARS_SIMPLE if simple else BARS):
        h = max(2, int(S * hh))
        r = h // 2
        y = int(S * cy)
        d.rounded_rectangle([int(S * LEFT), y - r, int(S * LEFT) + int(S * ln), y + r],
                            radius=r, fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    """手写 ICO 容器。

    不用 PIL 自带的 ico 保存，是因为它只会拿一张大图按比例缩，
    没法让 16px 用另一套图形。
    """
    import io as _io
    blobs = []
    for s in sizes:
        buf = _io.BytesIO()
        render(s, simple=(s <= 16)).save(buf, format="PNG")
        blobs.append((s, buf.getvalue()))

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, data = b"", b""
    for s, blob in blobs:
        w = 0 if s >= 256 else s          # 256 在 ICO 里记 0
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    path.write_bytes(header + entries + data)


def write_svg(path: Path) -> None:
    """矢量版，数值和 PIL 那边一一对应（都按 256 的坐标系）。"""
    S = 256
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" '
             f'width="{S}" height="{S}" fill="none">',
             f'  <rect width="{S}" height="{S}" rx="{round(S * RADIUS)}" fill="#4d80ef"/>']
    for ln, hh, cy in BARS:
        w = round(S * ln)
        h = round(S * hh)
        y = round(S * cy - h / 2)
        parts.append(f'  <rect x="{round(S * LEFT)}" y="{y}" width="{w}" height="{h}" '
                     f'rx="{h // 2}" fill="#fff"/>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


if __name__ == "__main__":
    out = Path("docs")
    out.mkdir(exist_ok=True)
    render(256).save(out / "icon.png")
    render(256).save(out / "_logo_preview.png")
    write_ico(out / "icon.ico")
    write_svg(out / "logo.svg")
    print("已生成:")
    print("  docs/icon.ico   ", (out / "icon.ico").stat().st_size, "字节")
    print("  docs/icon.png   ", (out / "icon.png").stat().st_size, "字节")
    print("  docs/logo.svg   ", (out / "logo.svg").stat().st_size, "字节")

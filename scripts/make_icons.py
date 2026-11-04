"""Draw the PWA icons (frontend/public/icons) with Pillow: yellow rounded square + black clapperboard."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "frontend" / "public" / "icons"
YELLOW, BLACK, BG = (255, 230, 0, 255), (12, 12, 15, 255), (12, 12, 15, 255)


def draw(size: int, maskable: bool) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG if maskable else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(size * (0.18 if maskable else 0.04))  # maskable: keep the art inside the 80 % safe zone
    box = (pad, pad, size - pad, size - pad)
    d.rounded_rectangle(box, radius=int((size - 2 * pad) * 0.22), fill=YELLOW)
    s = size - 2 * pad
    x0, y0 = pad + s * 0.24, pad + s * 0.30
    w, h = s * 0.52, s * 0.42
    # clapper body
    d.rounded_rectangle((x0, y0 + h * 0.28, x0 + w, y0 + h), radius=int(s * 0.03), outline=BLACK, width=max(2, int(s * 0.055)))
    # clapper stick (tilted bar with stripes)
    stick = [(x0, y0 + h * 0.18), (x0 + w * 0.98, y0 - h * 0.08), (x0 + w, y0 + h * 0.10), (x0 + w * 0.02, y0 + h * 0.36)]
    d.polygon(stick, fill=BLACK)
    for i in range(1, 4):
        t = i / 4
        a = (x0 + w * t, y0 + h * 0.18 - h * 0.26 * t)
        b = (a[0] + w * 0.07, a[1] - h * 0.02)
        d.line([a, (a[0] + w * 0.09, a[1] + h * 0.20)], fill=YELLOW, width=max(2, int(s * 0.03)))
        del b
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, False).save(OUT / "icon-192.png")
    draw(512, False).save(OUT / "icon-512.png")
    draw(512, True).save(OUT / "maskable-512.png")
    draw(180, True).convert("RGB").save(OUT / "apple-touch-icon.png")
    print("icons ->", OUT)


if __name__ == "__main__":
    main()

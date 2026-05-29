"""Generate a custom Dispatch icon at 1024x1024 (and an .iconset directory
ready for `iconutil -c icns`).

Concept: a command-post / radar console at the center, with a constellation
of bot units in orbit around it, connected by glowing signal lines. The user
(you) is the dispatcher inside the hub; the bots are the live claude sessions.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_PNG = Path(__file__).resolve().parent / "Dispatch.png"
ICONSET_DIR = Path(__file__).resolve().parent / "Dispatch.iconset"
ICNS_PATH = Path(__file__).resolve().parent / "Dispatch.icns"

SIZE = 1024
RADIUS = 225          # rounded square corner
N_BOTS = 6
ORBIT_R = 330
BOT_R = 78
HUB_SIZE = 230
ACCENT_PEACH = (255, 150, 105, 255)
ACCENT_PEACH_GLOW = (255, 150, 105, 70)
ACCENT_CYAN = (130, 200, 255, 255)
WIRE = (140, 200, 255, 90)
HUB_FILL = (32, 48, 88, 255)
HUB_STROKE = (110, 180, 240, 255)
SWEEP = (255, 200, 120, 220)


def vertical_gradient(size, top, bottom):
    im = Image.new("RGB", (size, size), top)
    d = ImageDraw.Draw(im)
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c)
    return im


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
    return m


def make_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 1. Rounded-square background with vertical gradient
    bg = vertical_gradient(SIZE, (12, 18, 38), (28, 42, 78)).convert("RGBA")
    bg.putalpha(rounded_mask(SIZE, RADIUS))
    img = Image.alpha_composite(img, bg)

    # 2. Soft top highlight — overlay only where bg is opaque
    highlight = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    for y in range(int(SIZE * 0.4)):
        a = int(28 * (1 - y / (SIZE * 0.4)))
        hd.line([(0, y), (SIZE, y)], fill=(255, 255, 255, a))
    # Multiply layer's own alpha by the rounded mask so it doesn't bleed out.
    h_alpha = highlight.split()[3]
    from PIL import ImageChops
    highlight.putalpha(ImageChops.multiply(h_alpha, rounded_mask(SIZE, RADIUS)))
    img = Image.alpha_composite(img, highlight)

    cx, cy = SIZE // 2, SIZE // 2

    # 3. Bot positions on orbit (start at top, go clockwise)
    bots = []
    for i in range(N_BOTS):
        ang = -math.pi / 2 + 2 * math.pi * i / N_BOTS
        bots.append((cx + ORBIT_R * math.cos(ang),
                     cy + ORBIT_R * math.sin(ang)))

    # 4. Glowing signal lines (drawn under everything)
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for bx, by in bots:
        gd.line([(cx, cy), (bx, by)], fill=WIRE, width=10)
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    img = Image.alpha_composite(img, glow)

    # crisp wire on top
    draw = ImageDraw.Draw(img)
    for bx, by in bots:
        draw.line([(cx, cy), (bx, by)], fill=(140, 200, 255, 110), width=3)

    # 5. Faint orbit ring
    ring = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((cx - ORBIT_R, cy - ORBIT_R, cx + ORBIT_R, cy + ORBIT_R),
               outline=(120, 180, 240, 60), width=2)
    img = Image.alpha_composite(img, ring)

    draw = ImageDraw.Draw(img)

    # 6. Central hub (radar console)
    hub_box = (cx - HUB_SIZE // 2, cy - HUB_SIZE // 2,
               cx + HUB_SIZE // 2, cy + HUB_SIZE // 2)
    # outer hub
    draw.rounded_rectangle(hub_box, radius=46, fill=HUB_FILL,
                           outline=HUB_STROKE, width=3)
    # inner radar circles
    for r in (78, 54, 30):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     outline=(150, 210, 255, 180), width=2)
    # tiny center dot
    draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=ACCENT_CYAN)
    # radar sweep wedge
    wedge = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wedge)
    wd.pieslice((cx - 78, cy - 78, cx + 78, cy + 78),
                start=-45, end=-15, fill=(255, 200, 120, 110))
    wedge = wedge.filter(ImageFilter.GaussianBlur(3))
    img = Image.alpha_composite(img, wedge)
    draw = ImageDraw.Draw(img)
    # sweep line
    sweep_a = math.radians(-25)
    draw.line([(cx, cy),
               (cx + 78 * math.cos(sweep_a), cy + 78 * math.sin(sweep_a))],
              fill=SWEEP, width=3)

    # 7. Bot heads
    for bx, by in bots:
        # peach glow halo
        halo = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        hd2 = ImageDraw.Draw(halo)
        hd2.ellipse((bx - BOT_R * 1.6, by - BOT_R * 1.6,
                     bx + BOT_R * 1.6, by + BOT_R * 1.6),
                    fill=ACCENT_PEACH_GLOW)
        halo = halo.filter(ImageFilter.GaussianBlur(20))
        img = Image.alpha_composite(img, halo)
        draw = ImageDraw.Draw(img)
        # body — rounded square
        draw.rounded_rectangle(
            (bx - BOT_R, by - BOT_R, bx + BOT_R, by + BOT_R),
            radius=22, fill=ACCENT_PEACH,
            outline=(255, 200, 170, 255), width=3,
        )
        # eyes
        eye_r = 9
        ey = by - 6
        draw.ellipse((bx - 26 - eye_r, ey - eye_r, bx - 26 + eye_r, ey + eye_r),
                     fill=(34, 28, 40, 255))
        draw.ellipse((bx + 26 - eye_r, ey - eye_r, bx + 26 + eye_r, ey + eye_r),
                     fill=(34, 28, 40, 255))
        # antenna
        draw.line([(bx, by - BOT_R), (bx, by - BOT_R - 22)],
                  fill=(255, 200, 170, 255), width=4)
        # antenna tip (cyan dot)
        draw.ellipse((bx - 7, by - BOT_R - 29, bx + 7, by - BOT_R - 15),
                     fill=ACCENT_CYAN)

    # 8. Subtle inner shadow at the bottom for depth (no putalpha — keep transparent)
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for y in range(int(SIZE * 0.3)):
        a = int(50 * (y / (SIZE * 0.3)))
        sd.line([(0, SIZE - y - 1), (SIZE, SIZE - y - 1)], fill=(0, 0, 0, a))
    # clip to rounded shape via alpha-mask multiply
    from PIL import ImageChops
    s_alpha = shadow.split()[3]
    shadow.putalpha(ImageChops.multiply(s_alpha, rounded_mask(SIZE, RADIUS)))
    img = Image.alpha_composite(img, shadow)

    return img


def pack_iconset(png: Path):
    """Create iconset/ with all sizes Apple wants, then iconutil → .icns."""
    if ICONSET_DIR.exists():
        for f in ICONSET_DIR.glob("*"):
            f.unlink()
        ICONSET_DIR.rmdir()
    ICONSET_DIR.mkdir()

    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for px, name in sizes:
        subprocess.run(
            ["sips", "-z", str(px), str(px), str(png),
             "--out", str(ICONSET_DIR / name)],
            check=True, capture_output=True,
        )

    if ICNS_PATH.exists():
        ICNS_PATH.unlink()
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)],
        check=True, capture_output=True,
    )


if __name__ == "__main__":
    icon = make_icon()
    icon.save(OUT_PNG)
    pack_iconset(OUT_PNG)
    print(f"png  → {OUT_PNG}")
    print(f"icns → {ICNS_PATH}")

# -*- coding: utf-8 -*-
# Crop just the runner out of the campaign poster and feather her edges
# (left + bottom) so she blends into the dark site background instead of
# sitting inside a hard rectangle.
from PIL import Image, ImageDraw
import os

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "static", "1d3f922b-d796-4c89-a307-1501ce382767.jpg")
OUT = os.path.join(BASE, "static", "runner.png")

im = Image.open(SRC).convert("RGBA")
w, h = im.size  # 1280 x 720

# Crop the right-hand portion containing the woman.
crop_box = (540, 0, w, h)  # x0, y0, x1, y1
runner = im.crop(crop_box)
cw, ch = runner.size

# Build an alpha mask: fade in from the left edge, fade out at the very bottom.
mask = Image.new("L", (cw, ch), 255)
draw = ImageDraw.Draw(mask)

fade_w = int(cw * 0.38)   # left-edge fade zone
fade_h = int(ch * 0.22)   # bottom fade zone

for x in range(fade_w):
    alpha = int(255 * (x / fade_w))
    draw.line([(x, 0), (x, ch)], fill=alpha)

for y in range(ch - fade_h, ch):
    t = (y - (ch - fade_h)) / fade_h
    alpha = int(255 * (1 - t))
    # combine with existing (left-fade) value by taking the min
    for x in range(cw):
        cur = mask.getpixel((x, y))
        mask.putpixel((x, y), min(cur, alpha))

r, g, b, a = runner.split()
runner = Image.merge("RGBA", (r, g, b, mask))
runner.save(OUT)
print("Saved:", OUT, runner.size)

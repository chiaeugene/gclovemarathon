# -*- coding: utf-8 -*-
# Cut the runner out of the campaign poster with real background removal
# (rembg / U2Net), then feather her bottom edge slightly so she sits
# naturally against the site's dark background.
import os
from PIL import Image
from rembg import remove

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "static", "1d3f922b-d796-4c89-a307-1501ce382767.jpg")
OUT = os.path.join(BASE, "static", "runner.png")

im = Image.open(SRC).convert("RGBA")
w, h = im.size  # 1280 x 720

# Crop generously around the woman first (faster + more accurate matting).
crop_box = (520, 0, w, h)
runner = im.crop(crop_box)

cut = remove(runner)  # returns RGBA with proper alpha cutout
cw, ch = cut.size

# Gentle fade at the very bottom so her feet don't end in a hard alpha edge.
r, g, b, a_band = cut.split()
a_px = a_band.load()
fade_h = int(ch * 0.10)
for y in range(ch - fade_h, ch):
    t = (y - (ch - fade_h)) / fade_h
    factor = 1 - t
    for x in range(cw):
        a_px[x, y] = int(a_px[x, y] * factor)

cut = Image.merge("RGBA", (r, g, b, a_band))
cut.save(OUT)
print("Saved:", OUT, cut.size)

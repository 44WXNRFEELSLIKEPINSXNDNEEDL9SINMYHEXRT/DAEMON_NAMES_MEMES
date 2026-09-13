"""RapidOCR engine smoke test: cyrillic pack load + recognize on a synthetic image."""
import os
import time
from PIL import Image, ImageDraw

t0 = time.perf_counter()
from ocr import RapidOcrEngine
eng = RapidOcrEngine()
eng.load()
print(f"engine load: {time.perf_counter()-t0:.1f}s")

# synthetic test images: Latin + Cyrillic caption text
from PIL import ImageFont
font = None
for fp in ["/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
           "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
           "/usr/share/fonts/adwaita-sans-fonts/AdwaitaSans-Regular.ttf",
           "/usr/share/fonts/google-droid-sans-fonts/DroidSans.ttf"]:
    try:
        font = ImageFont.truetype(fp, 48)
        print("font:", fp)
        break
    except OSError:
        continue
if font is None:
    print("no TTF found, using default (cyrillic may not render)")

for text in ["WHEN THE CODE WORKS", "бедный хомячок в ложке"]:
    img = Image.new("RGB", (900, 200), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 70), text, fill="black", font=font)
    t0 = time.perf_counter()
    res = eng.recognize(img)
    print(f"{time.perf_counter()-t0:.2f}s conf={res.confidence:.2f} text={res.text!r} usable={res.usable}")

# real image used in earlier VLM tests
real = "../worker/test.png"
if os.path.exists(real):
    img = Image.open(real).convert("RGB")
    t0 = time.perf_counter()
    res = eng.recognize(img)
    print(f"REAL {real} ({img.size}): {time.perf_counter()-t0:.2f}s conf={res.confidence:.2f} text={res.text!r} usable={res.usable}")

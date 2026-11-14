"""生成与主界面品牌标记一致的 Windows 应用图标。"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)
image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((32, 32, 992, 992), radius=224, fill="#007AFF")
for center, height in zip((312, 412, 512, 612, 712), (240, 380, 500, 380, 240)):
    top = (1024 - height) // 2
    draw.rounded_rectangle((center - 29, top, center + 29, top + height),
                           radius=29, fill="#FFFFFF")
image.save(OUT / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                    (64, 64), (128, 128), (256, 256)])
image.resize((256, 256), Image.Resampling.LANCZOS).save(OUT / "app.png")
print(OUT / "app.ico")

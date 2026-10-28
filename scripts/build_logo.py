"""Raster counterpart of assets/logo.svg; Pillow is a development-only dependency."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCALE = 4
SIZE = 512


def box(coords):
    return tuple(int(v * SCALE) for v in coords)


def build_logo() -> None:
    size = SIZE * SCALE
    image = Image.new("RGBA", (size, size))
    gradient = Image.new("RGBA", (size, size))
    pixels = gradient.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            pixels[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip((18, 60, 82), (11, 105, 104))
            ) + (255,)
    mask = Image.new("L", (size, size))
    ImageDraw.Draw(mask).rounded_rectangle(box((12, 12, 500, 500)), radius=116 * SCALE, fill=255)
    image.paste(gradient, (0, 0), mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        box((26, 26, 486, 486)), radius=104 * SCALE, outline=(65, 113, 119, 255), width=2 * SCALE
    )
    cards = Image.new("RGBA", (size, size))
    cd = ImageDraw.Draw(cards)
    cd.rounded_rectangle(box((165, 149, 306, 289)), radius=22 * SCALE, fill=(42, 126, 128))
    cd.rounded_rectangle(box((143, 170, 284, 310)), radius=22 * SCALE, fill=(167, 223, 205))
    for y, length in [(203, 80), (226, 58), (249, 72)]:
        cd.line(box((170, y, 170 + length, y)), fill=(28, 104, 109), width=10 * SCALE)
        for x in (170, 170 + length):
            cd.ellipse(box((x - 5, y - 5, x + 5, y + 5)), fill=(28, 104, 109))
    image.alpha_composite(
        cards.rotate(9, resample=Image.Resampling.BICUBIC, center=(225 * SCALE, 228 * SCALE))
    )
    draw = ImageDraw.Draw(image)
    draw.line(box((301, 300, 376, 375)), fill=(7, 59, 69), width=48 * SCALE)
    draw.ellipse(box((352, 351, 400, 399)), fill=(7, 59, 69))
    draw.line(box((307, 307, 382, 382)), fill=(247, 225, 177), width=36 * SCALE)
    draw.ellipse(box((364, 364, 400, 400)), fill=(239, 215, 160))
    draw.ellipse(
        box((106 - 13.5, 106 - 13.5, 338 + 13.5, 338 + 13.5)),
        outline=(255, 242, 213),
        width=27 * SCALE,
    )
    draw.arc(box((140, 140, 304, 304)), start=205, end=246, fill=(255, 252, 240), width=9 * SCALE)
    draw.polygon(
        [
            box((372, 100)),
            box((386, 132)),
            box((418, 146)),
            box((386, 160)),
            box((372, 192)),
            box((358, 160)),
            box((326, 146)),
            box((358, 132)),
        ],
        fill=(246, 198, 101),
    )
    draw.ellipse(box((94, 356, 112, 374)), fill=(158, 217, 199))
    draw.ellipse(box((122, 385, 130, 393)), fill=(106, 168, 157))
    image.resize((SIZE, SIZE), Image.Resampling.LANCZOS).save(ROOT / "logo.png", optimize=True)


if __name__ == "__main__":
    build_logo()

"""Draw the hook caption as a transparent PNG, TikTok "text with background" style:
black bold text, each line on its own white rounded box, boxes touching, color emoji."""
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent / "fonts"
TEXT_FONT = str(HERE / "Inter-SemiBold.otf")
EMOJI_FONT = str(HERE / "NotoColorEmoji.ttf")

SIZE = 76          # text size on a 1080-wide video
MAX_W = 900        # widest a line may be
PAD_X, PAD_Y = 26, 8
RADIUS = 22

EMOJI_RE = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2})|"
    "(?:[☀-➿\U0001F000-\U0001FAFF][️\U0001F3FB-\U0001F3FF]?"
    "(?:‍[☀-➿\U0001F000-\U0001FAFF][️\U0001F3FB-\U0001F3FF]?)*)")

_font = ImageFont.truetype(TEXT_FONT, SIZE)
_efont = ImageFont.truetype(EMOJI_FONT, 109)  # Noto Color Emoji only draws at 109px


def _pieces(text):
    out, i = [], 0
    for m in EMOJI_RE.finditer(text):
        if m.start() > i:
            out.append(("t", text[i:m.start()]))
        out.append(("e", m.group(0)))
        i = m.end()
    if i < len(text):
        out.append(("t", text[i:]))
    return out


def _emoji_img(e):
    im = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, 0), e, font=_efont, embedded_color=True)
    bbox = im.getbbox() or (0, 0, 1, 1)
    im = im.crop(bbox)
    h = int(SIZE * 1.08)
    return im.resize((max(1, int(im.width * h / im.height)), h), Image.LANCZOS)


def _width(text):
    w = 0
    for kind, s in _pieces(text):
        w += _font.getlength(s) if kind == "t" else int(SIZE * 1.08) + 2
    return w


def _wrap(text):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if cur and _width(trial) > MAX_W:
            lines.append(cur)
            cur = wd
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def render(text, out_path):
    lines = _wrap(text.strip())
    asc, desc = _font.getmetrics()
    line_h = asc + desc + PAD_Y * 2
    widths = [int(_width(l)) + PAD_X * 2 for l in lines]
    W = max(widths) + 8
    H = line_h * len(lines) + 8
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # white boxes (drawn first so they merge where lines touch)
    for i, lw in enumerate(widths):
        x0 = (W - lw) // 2
        y0 = 4 + i * line_h
        d.rounded_rectangle([x0, y0, x0 + lw, y0 + line_h], radius=RADIUS, fill=(255, 255, 255, 255))
    # text + emoji
    for i, line in enumerate(lines):
        x = (W - widths[i]) // 2 + PAD_X
        y = 4 + i * line_h + PAD_Y
        for kind, s in _pieces(line):
            if kind == "t":
                d.text((x, y), s, font=_font, fill=(0, 0, 0, 255))
                x += _font.getlength(s)
            else:
                e = _emoji_img(s)
                img.alpha_composite(e, (int(x) + 1, int(y + (asc + desc - e.height) / 2)))
                x += e.width + 2
    img.save(out_path)
    return out_path


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "hook.png")

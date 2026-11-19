"""Draw the hook caption as a transparent PNG, TikTok "text with background" style:
black bold text in one wide white rounded box (like the reference clip Nick sent), text only."""
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent / "fonts"
TEXT_FONT = str(HERE / "Inter-Bold.otf")
EMOJI_FONT = str(HERE / "NotoColorEmoji.ttf")

SIZE = 72          # text size on a 1080-wide video
MAX_W = 940        # widest a line may be
PAD_X, PAD_Y = 30, 18   # box padding (sides, top/bottom)
LINE_GAP = 2
RADIUS = 18

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


def strip_emoji(text):
    """Captions are text only (no emoji)."""
    return " ".join(EMOJI_RE.sub("", text).replace("\uFE0F", "").split())


def render(text, out_path):
    lines = _wrap(strip_emoji(text))
    asc, desc = _font.getmetrics()
    lh = asc + desc + LINE_GAP
    widths = [int(_width(l)) for l in lines]
    bw = max(widths) + PAD_X * 2
    bh = lh * len(lines) - LINE_GAP + PAD_Y * 2
    W, H = bw + 8, bh + 8
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 4 + bw, 4 + bh], radius=RADIUS, fill=(255, 255, 255, 255))
    for i, line in enumerate(lines):
        x = (W - widths[i]) / 2
        y = 4 + PAD_Y + i * lh
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

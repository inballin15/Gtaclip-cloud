"""Draw the hook caption as a transparent PNG, TikTok "text with background" style:
black bold text in one wide white rounded box (like the reference clip Nick sent), text only."""
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent / "fonts"
TEXT_FONT = str(HERE / "Inter-Bold.otf")
EMOJI_FONT = str(HERE / "NotoColorEmoji.ttf")

SIZE = 50          # text size on a 1080-wide video
MAX_W = 720         # TikTok zooms 9:16 videos on tall phones and cuts ~9% off each side: stay in the middle
PAD_X, PAD_Y = 22, 14   # box padding (sides, top/bottom)
LINE_GAP = 2
RADIUS = 16

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


def render(text, out_path, style="white", scale=1.0):
    """style: white (white box, black text) · black (black box, white text) · outline (no box, white text, black edge).
    scale: overall size (the editor's pinch)."""
    lines = _wrap(strip_emoji(text))
    asc, desc = _font.getmetrics()
    lh = asc + desc + LINE_GAP
    widths = [int(_width(l)) for l in lines]
    bw = max(widths) + PAD_X * 2
    bh = lh * len(lines) - LINE_GAP + PAD_Y * 2
    W, H = bw + 8, bh + 8
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if style != "outline":
        d.rounded_rectangle([4, 4, 4 + bw, 4 + bh], radius=RADIUS,
                            fill=(255, 255, 255, 255) if style != "black" else (12, 12, 16, 235))
    ink = (0, 0, 0, 255) if style == "white" else (255, 255, 255, 255)
    for i, line in enumerate(lines):
        x = (W - widths[i]) / 2
        y = 4 + PAD_Y + i * lh
        for kind, s in _pieces(line):
            if kind == "t":
                if style == "outline":
                    d.text((x, y), s, font=_font, fill=ink, stroke_width=5, stroke_fill=(0, 0, 0, 255))
                else:
                    d.text((x, y), s, font=_font, fill=ink)
                x += _font.getlength(s)
            else:
                e = _emoji_img(s)
                img.alpha_composite(e, (int(x) + 1, int(y + (asc + desc - e.height) / 2)))
                x += e.width + 2
    if abs(scale - 1.0) > 0.01:
        scale = max(0.6, min(1.6, scale))
        img = img.resize((max(1, int(W * scale)), max(1, int(H * scale))), Image.LANCZOS)
    img.save(out_path)
    return out_path


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "hook.png")


# ---------------------------------------------------------------- word-by-word subtitles (WhisperX-style captions)
_sub_font = None


def render_sub(text: str, out: str, size: int = 76) -> str:
    """One subtitle chunk (1-3 words): big white bold letters with a thick black outline, like top clip pages."""
    global _sub_font
    if _sub_font is None or _sub_font.size != size:
        _sub_font = ImageFont.truetype(TEXT_FONT, size)
    text = strip_emoji(text).strip().upper()
    stroke = max(6, size // 9)
    l, t, r, b = _sub_font.getbbox(text, stroke_width=stroke)
    im = Image.new("RGBA", (r - l + 8, b - t + 8), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((4 - l, 4 - t), text, font=_sub_font, fill=(255, 255, 255, 255),
                            stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    im.save(out)
    return out


def sub_chunks(words: list, max_words: int = 3, max_chars: int = 16) -> list[tuple[float, float, str]]:
    """Group (start, end, word) into short on-screen chunks, each shown until the next one starts."""
    out, cur = [], []
    for w in words:
        word = str(w[2]).strip()
        if not word:
            continue
        if cur and (len(cur) >= max_words or len(" ".join(x[2] for x in cur) + " " + word) > max_chars
                    or w[0] - cur[-1][1] > 0.6):
            out.append(cur)
            cur = []
        cur.append((float(w[0]), float(w[1]), word))
    if cur:
        out.append(cur)
    res = []
    for i, ch in enumerate(out):
        a = ch[0][0]
        b = ch[-1][1] + 0.25
        if i + 1 < len(out):
            b = min(b + 0.6, out[i + 1][0][0])
        res.append((a, max(a + 0.25, b), " ".join(x[2] for x in ch)))
    return res

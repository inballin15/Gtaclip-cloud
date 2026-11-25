"""Things in every posted clip that win followers and keep people watching (Nov 25).

- a little animated "Follow for daily clips" button that pops up right at the start (next to the hook, never over
  the webcam or under TikTok / Shorts' own buttons), pulses once, and pops away again
- an optional progress bar along the top that fills as the clip plays (the learner tests it: on for half the clips)

Used by pipeline.render_final, quickedit and voice (anything that makes a posted video with a hook on it).
No emojis: the + and the button are drawn.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT = ROOT / "fonts" / "Inter-Bold.otf"
FPS = 30
TEXT = "Follow for daily clips"
PINK = (255, 46, 136)
SAFE_TOP, SAFE_BOTTOM = 150, 300      # TikTok / Shorts cover these (same numbers as pipeline.py)
START, IN_S, HOLD_S, OUT_S = 0.7, 0.35, 2.6, 0.3   # appears 0.7 s in (the very first frame stays clean)

_cache: dict[str, tuple[str, int, int]] = {}


def _button(scale: float, alpha: float, ss: int = 2) -> Image.Image:
    """The button at a given size / opacity, drawn big then shrunk (smooth edges)."""
    fs = 46 * ss
    font = ImageFont.truetype(str(FONT), fs)
    tw = font.getbbox(TEXT)[2]
    h = 104 * ss
    circ = 72 * ss
    pad = 16 * ss
    w = pad + circ + 22 * ss + tw + 34 * ss
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=(255, 255, 255, 255))
    cy = h // 2
    cx = pad + circ // 2
    d.ellipse([cx - circ // 2, cy - circ // 2, cx + circ // 2, cy + circ // 2], fill=PINK + (255,))
    arm, th = 17 * ss, 7 * ss   # the "+"
    d.rounded_rectangle([cx - arm, cy - th // 2, cx + arm, cy + th // 2], radius=th // 2, fill=(255, 255, 255, 255))
    d.rounded_rectangle([cx - th // 2, cy - arm, cx + th // 2, cy + arm], radius=th // 2, fill=(255, 255, 255, 255))
    tb = font.getbbox(TEXT)
    d.text((pad + circ + 22 * ss, cy - (tb[1] + tb[3]) / 2), TEXT, font=font, fill=(10, 10, 14, 255))
    im = im.resize((max(1, int(w / ss * scale)), max(1, int(h / ss * scale))), Image.LANCZOS)
    if alpha < 1:
        a = im.getchannel("A").point(lambda v: int(v * alpha))
        im.putalpha(a)
    return im


def _ease_back(x: float) -> float:   # overshoot a little, then settle (a "pop")
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def cta_frames(out_dir: Path) -> tuple[str, int, int]:
    """PNG frames of the whole pop-up (pop in, pulse, pop out) on a fixed-size see-through canvas."""
    key = str(out_dir)
    if key in _cache:
        return _cache[key]
    out_dir.mkdir(parents=True, exist_ok=True)
    full = _button(1.0, 1.0)
    W, H = int(full.width * 1.12) + 4, int(full.height * 1.12) + 4
    n = int(round((IN_S + HOLD_S + OUT_S) * FPS))
    for i in range(n):
        t = i / FPS
        if t < IN_S:                                   # pop in
            x = t / IN_S
            s, a = 0.55 + 0.45 * _ease_back(x), min(1.0, x * 1.6)
        elif t < IN_S + HOLD_S:                        # one gentle pulse halfway (draws the eye without nagging)
            u = (t - IN_S) / HOLD_S
            s, a = 1.0 + (0.06 * math.sin((u - 0.35) / 0.3 * math.pi) if 0.35 <= u <= 0.65 else 0.0), 1.0
        else:                                          # pop out
            x = (t - IN_S - HOLD_S) / OUT_S
            s, a = 1.0 - 0.25 * x, max(0.0, 1 - x)
        b = _button(max(0.05, s), a)
        can = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        can.alpha_composite(b, ((W - b.width) // 2, (H - b.height) // 2))
        can.save(out_dir / f"cta_{i:03d}.png")
    _cache[key] = (str(out_dir / "cta_%03d.png"), W, H)
    return _cache[key]


def cta_y(hook_top: float | None, hook_h: float | None, layout: str, ch: int) -> int:
    """Next to the hook: below it, or above it when the face is below the hook (webcam-only clips) or there's no room."""
    if hook_top is None:
        return int(1920 * 0.62 - ch / 2)
    below = hook_top + (hook_h or 0) + 18
    above = hook_top - ch - 18
    y = above if layout == "cam" else below
    if y + ch > 1920 - SAFE_BOTTOM:
        y = above
    if y < SAFE_TOP:
        y = below if below + ch <= 1920 - SAFE_BOTTOM else SAFE_TOP
    return int(y)


def add(inputs: list, flt: str, last: str, dur: float, work: Path, hook_top: float | None = None,
        hook_h: float | None = None, layout: str = "split", cta: bool = True, pbar: bool = False) -> tuple[list, str, str]:
    """Adds the follow pop-up / progress bar to an ffmpeg filter graph. Returns (inputs, filter, last label)."""
    if cta and dur > 4:
        pat, cw, ch = cta_frames(work / "cta")
        k = inputs.count("-i")
        inputs = inputs + ["-framerate", str(FPS), "-i", pat]
        y = cta_y(hook_top, hook_h, layout, ch)
        flt += (f";[{k}:v]format=rgba,setpts=PTS-STARTPTS+{START}/TB[ctav]"
                f";[{last}][ctav]overlay=x=(W-w)/2:y={y}:eof_action=pass:format=auto[ctao]")
        last = "ctao"
    if pbar and dur > 2:
        k = inputs.count("-i")
        inputs = inputs + ["-f", "lavfi", "-t", f"{dur + 0.5:.2f}", "-i", f"color=c=0x{PINK[0]:02X}{PINK[1]:02X}{PINK[2]:02X}:s=1080x10:r={FPS}"]
        flt += (f";[{last}]drawbox=x=0:y=0:w=iw:h=10:color=black@0.35:t=fill[pbt]"
                f";[pbt][{k}:v]overlay=x='-w+w*t/{dur:.3f}':y=0:eof_action=pass[pbo]")
        last = "pbo"
    return inputs, flt, last


def hook_top(lay: str, hy: float | None, top_h: int, hh: int) -> int:
    """Where the hook box goes (top edge, px) - same rules as pipeline.render_final, for quick edits / new voice."""
    y = (1920 * hy - hh / 2 if hy is not None else (1920 - hh) / 2 if lay == "full"
         else max(SAFE_TOP, 400 - hh - 26) if lay == "react" else 1920 * 0.2 - hh / 2 if lay == "fill"
         else 1920 * 0.6 if lay.startswith("irl") else 1920 * 0.12 if lay == "cam" else top_h - hh * 0.2)
    return int(max(SAFE_TOP, min(1920 - hh - SAFE_BOTTOM, y)))


def hooked(ff_exe: str, clean: Path, png: str, out: Path, lay: str, hy: float | None, top_h: int, work: Path,
           pbar: bool = False, audio_copy: bool = True) -> None:
    """clean copy + hook + follow pop-up (+ progress bar) -> the posted video (quick edits / new voice)."""
    import re
    import subprocess
    hh = Image.open(png).size[1]
    y = hook_top(lay, hy, top_h, hh)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", subprocess.run([ff_exe, "-hide_banner", "-i", str(clean)],
                                                                    capture_output=True, text=True).stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    inputs = ["-i", str(clean), "-i", png]
    flt = f"[0:v][1:v]overlay=(W-w)/2:{y}[vh]"
    inputs, flt, last = add(inputs, flt, "vh", dur, work, y, hh, lay, cta=True, pbar=pbar)
    flt += f";[{last}]format=yuv420p[out]"
    subprocess.run([ff_exe, "-y", "-loglevel", "error", *inputs, "-filter_complex", flt, "-map", "[out]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-maxrate", "12000k", "-bufsize", "24M",
                    "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)

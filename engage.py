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
TEXT = "Follow"                      # Nov 27 redesign: a small dark "glass" chip in the top-right corner
TEXT2 = "for daily clips"
PINK = (255, 46, 136)
SAFE_TOP, SAFE_BOTTOM = 150, 300      # TikTok / Shorts cover these (same numbers as pipeline.py)
START, IN_S, HOLD_S, OUT_S = 0.7, 0.25, 3.0, 0.3   # appears 0.7 s in, slides in, rests 3 s, fades (no pulse)
MARGIN, SLIDE = 36, 40   # from the right edge; slides in from 40 px further right

_cache: dict[str, tuple[str, int, int]] = {}


def _button(alpha: float = 1.0, ss: int = 3) -> Image.Image:
    """The chip: dark see-through glass, a thin light edge, a small pink + circle, 'Follow' + 'for daily clips'."""
    font = ImageFont.truetype(str(FONT), 30 * ss)
    font2 = ImageFont.truetype(str(ROOT / "fonts" / "Inter-SemiBold.otf"), 25 * ss)
    tw, tw2 = font.getbbox(TEXT)[2], font2.getbbox(TEXT2)[2]
    h, circ, pad = 64 * ss, 42 * ss, 11 * ss
    w = pad + circ + 14 * ss + tw + 10 * ss + tw2 + 24 * ss
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=(12, 12, 16, 150), outline=(255, 255, 255, 52), width=2 * ss)
    cy, cx = h // 2, pad + circ // 2
    d.ellipse([cx - circ // 2, cy - circ // 2, cx + circ // 2, cy + circ // 2], fill=PINK + (255,))
    arm, th = 10 * ss, 4 * ss
    d.rounded_rectangle([cx - arm, cy - th // 2, cx + arm, cy + th // 2], radius=th // 2, fill=(255, 255, 255, 255))
    d.rounded_rectangle([cx - th // 2, cy - arm, cx + th // 2, cy + arm], radius=th // 2, fill=(255, 255, 255, 255))
    x0 = pad + circ + 14 * ss
    tb = font.getbbox(TEXT)
    d.text((x0, cy - (tb[1] + tb[3]) / 2), TEXT, font=font, fill=(255, 255, 255, 255))
    tb2 = font2.getbbox(TEXT2)
    d.text((x0 + tw + 10 * ss, cy - (tb2[1] + tb2[3]) / 2 + ss), TEXT2, font=font2, fill=(255, 255, 255, 190))
    im = im.resize((max(1, w // ss), max(1, h // ss)), Image.LANCZOS)
    if alpha < 1:
        im.putalpha(im.getchannel("A").point(lambda v: int(v * alpha)))
    return im


def _ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def cta_frames(out_dir: Path) -> tuple[str, int, int]:
    """PNG frames of the chip (slide + fade in, rest, fade out) on a fixed see-through canvas, right-aligned."""
    key = str(out_dir)
    if key in _cache:
        return _cache[key]
    out_dir.mkdir(parents=True, exist_ok=True)
    full = _button()
    W, H = full.width + SLIDE + 2, full.height + 2
    n = int(round((IN_S + HOLD_S + OUT_S) * FPS))
    for i in range(n):
        t = i / FPS
        if t < IN_S:
            x = _ease_out(t / IN_S)
            dx, a = SLIDE * (1 - x), x
        elif t < IN_S + HOLD_S:
            dx, a = 0, 1.0
        else:
            dx, a = 0, max(0.0, 1 - (t - IN_S - HOLD_S) / OUT_S)
        b = _button(a)
        can = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        can.alpha_composite(b, (W - b.width - SLIDE + int(round(dx)), 1))
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
        y = SAFE_TOP + 24   # top-right corner, just below TikTok / Shorts' top bar: away from the action
        flt += (f";[{k}:v]format=rgba,setpts=PTS-STARTPTS+{START}/TB[ctav]"
                f";[{last}][ctav]overlay=x=W-w-{MARGIN - SLIDE}:y={y}:eof_action=pass:format=auto[ctao]")
        last = "ctao"
    if pbar and dur > 2:
        k = inputs.count("-i")
        inputs = inputs + ["-f", "lavfi", "-t", f"{dur + 0.5:.2f}", "-i", f"color=c=0x{PINK[0]:02X}{PINK[1]:02X}{PINK[2]:02X}:s=1080x10:r={FPS}"]
        flt += (f";[{last}]drawbox=x=0:y=0:w=iw:h=10:color=black@0.35:t=fill[pbt]"
                f";[pbt][{k}:v]overlay=x='-w+w*t/{dur:.3f}':y=0:eof_action=pass[pbo]")
        last = "pbo"
    return inputs, flt, last


POP_AT = START + 0.04   # the click lands as the chip starts sliding in


def pop_wav(path: Path) -> Path:
    """A soft, crisp UI click (made here - no stock sound): a 3 ms tick + a tiny 2.4 kHz tap that dies in ~12 ms."""
    path = path.with_name("click.wav")
    if path.exists():
        return path
    import random
    import struct
    import wave
    sr, n = 48000, int(48000 * 0.05)
    rnd = random.Random(11)
    out = []
    for i in range(n):
        t = i / sr
        tick = (rnd.random() * 2 - 1) * math.exp(-t / 0.0009) * 0.55
        tap = math.sin(2 * math.pi * 2400 * t) * math.exp(-t / 0.004) * 0.6
        low = math.sin(2 * math.pi * 900 * t) * math.exp(-t / 0.006) * 0.25
        out.append(max(-1.0, min(1.0, tick + tap + low)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<hh", int(v * 22000), int(v * 22000)) for v in out))
    return path


# the spoken follow line (Nov 25 - like vicecityfiles' "make sure to follow"): an AI voice made once per voice
# (voice.py --samples, on the "voices" release); which voice = Settings on the site (control.json "follow_voice")
FOLLOW_LINE = "Don't forget to follow and subscribe for daily clips!"
SAY_AT = POP_AT + 0.22            # right after the pop
SAY_DUCK = 0.12                   # the clip's sound under the voice (x0.12 = about -18 dB), straight back after
SAY_GAIN = 1.8                    # the voice itself (+5 dB): clearly on top of a loud stream
VOICE = None                      # set by the clip maker from your settings; None = read them here
REPO = "inballin15/Gtaclip-cloud"


def follow_voice_id() -> str:
    """Your pick in Settings (am_puck if never set; "off" = no voice, just the pop)."""
    global VOICE
    if VOICE is None:
        try:
            import json
            import urllib.request
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/{REPO}/posted/control.json", timeout=15) as r:
                VOICE = str(json.loads(r.read()).get("follow_voice") or "am_puck")
        except Exception:  # noqa: BLE001
            VOICE = "am_puck"
    return VOICE


def follow_voice(work: Path, voice: str | None = None) -> tuple[Path, float] | None:
    """(wav, seconds) of the follow line - this clip's own voice when you picked one, else your favorite.
    None when off or not made yet."""
    v = voice or follow_voice_id()
    if not v or v == "off":
        return None
    p = work / f"follow_{v}.wav"
    try:
        if not (p.exists() and p.stat().st_size > 2000):
            import urllib.request
            p.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(f"https://github.com/{REPO}/releases/download/voices/follow_{v}.wav", timeout=30) as r:
                p.write_bytes(r.read())
        import wave
        with wave.open(str(p)) as w:
            return p, w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return None


def add_audio(inputs: list, flt: str, work: Path, src_a: str = "0:a", say: bool = True,
              voice: str | None = None) -> tuple[list, str, str]:
    """Nov 27: just a soft click as the chip slides in - no spoken line, the clip's sound isn't touched.
    (say / voice are kept so older callers still work; they do nothing now.) Returns (inputs, filter, audio label)."""
    k = inputs.count("-i")
    inputs = inputs + ["-i", str(pop_wav(work / "pop.wav"))]
    ms = int(POP_AT * 1000)
    flt += (f";[{src_a}]aresample=48000[adk]"
            f";[{k}:a]aresample=48000,adelay={ms}|{ms},volume=0.55[apop]"
            ";[adk][apop]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=false[amix]")
    return inputs, flt, "amix"


def hook_top(lay: str, hy: float | None, top_h: int, hh: int) -> int:
    """Where the hook box goes (top edge, px) - same rules as pipeline.render_final, for quick edits / new voice."""
    y = (1920 * hy - hh / 2 if hy is not None else (1920 - hh) / 2 if lay == "full"
         else max(SAFE_TOP, 400 - hh - 26) if lay == "react" else 1920 * 0.2 - hh / 2 if lay == "fill"
         else 1920 * 0.6 if lay.startswith("irl") else 1920 * 0.12 if lay == "cam" else top_h - hh * 0.2)
    return int(max(SAFE_TOP, min(1920 - hh - SAFE_BOTTOM, y)))


def hooked(ff_exe: str, clean: Path, png: str, out: Path, lay: str, hy: float | None, top_h: int, work: Path,
           pbar: bool = False, audio_copy: bool = True, voice: str | None = None) -> None:
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
    has_a = "Audio:" in subprocess.run([ff_exe, "-hide_banner", "-i", str(clean)], capture_output=True, text=True).stderr
    if has_a and dur > 4:   # the pop + a small dip of the clip's sound
        inputs, flt, al = add_audio(inputs, flt, work, voice=voice)
        amap = ["-map", f"[{al}]", "-c:a", "aac", "-b:a", "192k"]
    else:
        amap = ["-map", "0:a?", "-c:a", "copy"]
    subprocess.run([ff_exe, "-y", "-loglevel", "error", *inputs, "-filter_complex", flt, "-map", "[out]", *amap,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-maxrate", "16000k", "-bufsize", "32M", "-profile:v", "high",
                    "-movflags", "+faststart", str(out)], check=True)

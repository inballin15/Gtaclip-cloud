"""Long-form tips videos from YOUR gameplay - you only upload the footage.

Site: Make clips -> "Long video" -> upload gameplay. That opens an issue "longform: <id>" (same upload pieces as
your clips). This then:
  1. puts the video back together (bars off, HDR phone video -> normal colours, 1080p 30 fps)
  2. WATCHES it: the AI looks at a frame every couple of seconds and logs what happens when
  3. WRITES the script from that log: picks the topic itself (e.g. "8 GTA 6 tips you missed"), every line is tied
     to the moment it talks about, so the narration matches what's on screen; only says what it can see
  4. reads it in the AI voice you picked (Kokoro, free, runs here)
  5. edits: only the useful parts, travel sped up, game sound ducked under the voice, tip titles, a strong first
     10 seconds; thumbnail, title, description and chapters
  6. puts it in the site's "Long videos" section for you to watch first - nothing is posted.
The full-quality file goes on the "longform" release (download it for YouTube); a smaller preview plays on the site.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
REPO = os.environ.get("GITHUB_REPOSITORY", "inballin15/Gtaclip-cloud")
TOKEN = os.environ.get("GH_TOKEN", "")
PAGES = "https://inballin15.github.io/Gtaclip-cloud"
WORK = Path(os.environ.get("LF_WORK", "/tmp/lf"))
WORK.mkdir(parents=True, exist_ok=True)
try:
    import imageio_ffmpeg
    FF = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # noqa: BLE001
    FF = "ffmpeg"
FONT = str(ROOT / "fonts" / "Inter-Bold.otf")
W, H, FPS = 1920, 1080, 30
SR = 24000                       # Kokoro voice sample rate
WPS = 2.6                        # narration speed: words per second (Kokoro at speed 1.0 is ~2.6-2.9)
KEEP_PREVIEWS = 1                # long-video previews kept on the site (Pages has a 1 GB limit, shared with the clips);
PREVIEW_MAX_MB = 45              # the full-quality files stay on the "longform" release


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ff(*args: str) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True)


def duration_of(p: Path) -> float:
    out = subprocess.run([FF, "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def mmss(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


# ------------------------------------------------------------------ GitHub
def gh(method: str, path: str, body=None):
    import requests
    r = requests.request(method, f"https://api.github.com/repos/{REPO}{path}", json=body, timeout=60,
                         headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def comment(num: int, text: str) -> None:
    if num and TOKEN:
        gh("POST", f"/issues/{num}/comments", {"body": text})


def git(*args: str, inp: bytes | None = None, env: dict | None = None) -> str:
    r = subprocess.run(["git", *args], input=inp, capture_output=True, env={**os.environ, **(env or {})})
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.decode()[:300]}")
    return r.stdout.decode().strip()


# ------------------------------------------------------------------ 1. the footage
def get_source(job: dict) -> tuple[Path, str]:
    """Your uploaded video, put back together from its pieces (from the upload branch, or its commit later)."""
    src = WORK / "src.mp4"
    if src.exists() and src.stat().st_size > 0:
        return src, job.get("commit", "")
    ref = job.get("ref") or f"upload-{job['id']}"
    sha = job.get("commit") or ""
    if not sha:
        try:
            git("fetch", "-q", "--depth=1", "origin", f"+{ref}:refs/remotes/origin/{ref}")
            sha = git("rev-parse", f"origin/{ref}")
        except RuntimeError:
            code, acts = gh("GET", f"/activity?ref=refs/heads/{ref}&per_page=20")
            for a in acts if isinstance(acts, list) else []:
                if a.get("activity_type") in ("branch_creation", "push") and a.get("after", "0" * 40) != "0" * 40:
                    sha = a["after"]
                    break
            if not sha:
                raise
            git("fetch", "-q", "--depth=1", "origin", sha)
    parts = job.get("parts") or sorted(p for p in git("ls-tree", "-r", "--name-only", sha).split() if "/part" in p)
    with open(src, "wb") as fh:
        for part in parts:
            fh.write(subprocess.run(["git", "show", f"{sha}:{part}"], capture_output=True, check=True).stdout)
    log(f"got your footage back together: {src.stat().st_size // 1_000_000} MB, {mmss(duration_of(src))}")
    return src, sha


def make_master(src: Path) -> Path:
    """One clean working copy: bars off, HDR -> normal colours, 1920x1080, 30 fps."""
    master = WORK / "master.mp4"
    if master.exists() and master.stat().st_size > 0:
        return master
    vf = []
    try:
        sys.path.insert(0, str(ROOT))
        import pipeline as P
        crop = P.find_bars(src)
        if crop:
            vf.append(f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}")
            log(f"cut off the bars around the picture: {crop[2]}x{crop[3]}")
        hdr = P.is_hdr(src) and P._has_filter("zscale")
    except Exception as e:  # noqa: BLE001
        log(f"bar / colour check skipped: {e}")
        hdr = False
    vf.append("fps=30")
    if hdr:
        vf.append("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=clip:desat=0,"
                  "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        log("HDR recording -> converting to normal colours")
    vf.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
              "setsar=1,format=yuv420p")
    has_a = "Audio:" in subprocess.run([FF, "-hide_banner", "-i", str(src)], capture_output=True, text=True).stderr
    ff("-i", str(src), *([] if has_a else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]),
       "-vf", ",".join(vf), "-map", "0:v:0", "-map", "0:a:0" if has_a else "1:a", "-shortest",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
       "-movflags", "+faststart", str(master))
    log(f"working copy ready: {mmss(duration_of(master))}")
    return master


# ------------------------------------------------------------------ 2. watch it (AI)
DAILY = {"hit": False}


def ai(parts: list, log_name: str, tries: int = 10, temperature: float = 0.4) -> dict | list | None:
    """One Gemini request -> parsed JSON. Google's free tier limits requests per minute: on "too many requests" it
    waits (as long as Google says) and tries again - it doesn't give up on a model for a busy minute."""
    import urllib.error
    import urllib.request
    sys.path.insert(0, str(ROOT))
    import viral
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log("no Gemini key - can't watch / write")
        return None
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
    models = [m for m in viral.gemini_models(key, log)] or ["gemini-3-flash-preview", "gemini-3.1-flash-lite-preview"]
    for attempt in range(tries):
        wait, daily = 0, 0
        for model in models:
            try:
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    res = json.loads(r.read())
                txt = res["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(txt)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()[:2000]
                except Exception:  # noqa: BLE001
                    pass
                m = re.search(r'"retryDelay":\s*"(\d+)', detail)
                per_day = "PerDay" in detail or "per day" in detail.lower()
                if e.code == 404:
                    models = [x for x in models if x != model]
                if e.code in (429, 503):
                    wait = max(wait, int(m.group(1)) + 2 if m else 30 * (attempt + 1))
                    daily += int(per_day)
                log(f"{log_name} ({model}): HTTP {e.code}{' daily limit' if per_day else ''}"
                    f"{' - Google says wait ' + m.group(1) + 's' if m else ''}")
            except Exception as e:  # noqa: BLE001
                wait = max(wait, 20)
                log(f"{log_name} ({model}): {str(e)[:90]}")
        if not models or not wait:
            break
        if models and daily >= len(models):
            DAILY["hit"] = True
            log(f"{log_name}: Google's free daily AI limit is used up for today")
            return None
        wait = min(wait, 180)
        log(f"{log_name}: Google's free AI is busy - waiting {wait}s (try {attempt + 1}/{tries})")
        time.sleep(wait)
    return None


def watch(master: Path, game: str) -> list[dict]:
    """A frame every few seconds -> what happens at each moment (the AI looks at 30 frames per request)."""
    import base64
    dur = duration_of(master)
    step = 2 if dur <= 20 * 60 else 3 if dur <= 45 * 60 else 4
    fdir = WORK / "frames"
    if not fdir.exists() or not any(fdir.iterdir()):
        fdir.mkdir(exist_ok=True)
        ff("-i", str(master), "-vf", f"fps=1/{step},scale=640:-2", "-q:v", "6", str(fdir / "%05d.jpg"))
    frames = sorted(fdir.glob("*.jpg"))
    cache = WORK / "scene_log.json"
    got = json.loads(cache.read_text()) if cache.exists() else []
    done = {round(r["t"]) for r in got}
    log(f"watching your footage: {len(frames)} frames, one every {step}s")
    batch = 30
    for b0 in range(0, len(frames), batch):
        chunk = frames[b0:b0 + batch]
        ts = [(b0 + i) * step for i in range(len(chunk))]
        if all(round(t) in done for t in ts):
            continue
        parts = [{"text": (
            f"These are frames from {game} gameplay, in order, each with its time in the video. For EACH frame write "
            "one JSON object: {\"t\": seconds, \"what\": what the player is doing (5-15 words), \"where\": the place "
            "if you can tell (else \"\"), \"ui\": any readable on-screen text that matters (money, prompts, mission "
            "text, map labels; else \"\"), \"tip\": one specific useful thing a player could learn from this moment "
            "(a shortcut, a hidden feature, a money method, a control, a mechanic) - or \"\" if nothing, "
            "\"score\": 0-10 how good this moment is for a tips video (clear, useful, visually interesting), "
            "\"boring\": true if it's travel / loading / menus with nothing to learn}. Only describe what you can "
            "SEE - never guess names you can't read, never invent numbers. Return a JSON list.")}]
        for t, f in zip(ts, chunk):
            parts.append({"text": f"time {t}s ({mmss(t)})"})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(f.read_bytes()).decode()}})
        res = ai(parts, f"watching {mmss(ts[0])}-{mmss(ts[-1])}")
        if res is None and DAILY["hit"]:
            break
        rows = res if isinstance(res, list) else (res or {}).get("frames", []) if isinstance(res, dict) else []
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            try:
                t = float(r.get("t", ts[min(i, len(ts) - 1)]))
            except Exception:  # noqa: BLE001
                t = ts[min(i, len(ts) - 1)]
            got.append({"t": t, "what": str(r.get("what", ""))[:120], "where": str(r.get("where", ""))[:60],
                        "ui": str(r.get("ui", ""))[:100], "tip": str(r.get("tip", ""))[:160],
                        "score": float(r.get("score") or 0), "boring": bool(r.get("boring"))})
        cache.write_text(json.dumps(got))
        log(f"watched up to {mmss(ts[-1])} ({len(got)} moments logged)")
        time.sleep(4)   # stay under the free per-minute limit
    got.sort(key=lambda r: r["t"])
    return got


# ------------------------------------------------------------------ 3. the script (AI)
def target_len(dur: float) -> float:
    """How long the finished video should be: about half the footage (the good parts), 3-12 minutes."""
    return max(min(dur, 150.0), min(12 * 60.0, dur * 0.55))


def write_script(scenes: list[dict], dur: float, game: str, topic: str = "") -> dict | None:
    lines = []
    for r in scenes:
        bits = [r["what"]]
        if r["where"]:
            bits.append(f"@ {r['where']}")
        if r["ui"]:
            bits.append(f"[screen: {r['ui']}]")
        if r["tip"]:
            bits.append(f"TIP: {r['tip']}")
        lines.append(f"{mmss(r['t'])} ({int(r['t'])}s) s{int(r['score'])}{' boring' if r['boring'] else ''} | " + " | ".join(bits))
    L = target_len(dur)
    prompt = (
        f"You write the script for a faceless {game} TIPS & TRICKS YouTube video, narrated by an AI voice over the "
        f"player's own gameplay. Below is a log of the footage ({mmss(dur)} long): each line is a moment with its time "
        "in seconds, a score for how useful/interesting it is, and what is on screen.\n"
        + (f"The creator wants this topic: {topic}\n" if topic else
           "Pick the topic yourself from what the footage actually shows (e.g. '7 GTA 6 tips you missed', "
           "'GTA 6 money tips', 'GTA 6 driving tricks').\n") +
        f"Make a video about {int(L // 60)}-{int(L // 60) + 2} minutes long.\n"
        "STRUCTURE: an intro section (8-15 s) on the most eye-catching moment with a hook that promises what they'll "
        "learn, then 4-10 tip sections, then a short outro (ask them to subscribe for more " + game + " tips).\n"
        "EACH SECTION uses ONE continuous stretch of the footage that SHOWS that tip (start/end in seconds from the "
        "log), and its narration lines are tied to the moment they talk about: 'at' = the second in the footage when "
        "that thing is on screen. Use speed 1.5 or 2 for driving / travel stretches, 1 when the tip is happening.\n"
        "NARRATION RULES: talk to the viewer ('you'), clear and energetic, like a top GTA guide channel; each line "
        "8-22 words; say only things the log shows - never invent prices, numbers, place names or mechanics that "
        "aren't in it (on-screen text in [screen: ...] can be quoted); no emojis; numbers as words a voice can read. "
        "Keep enough footage per line: a section needs about (words / 2.6) seconds of footage after its speed-up.\n"
        "Title: max 70 characters, makes people curious, true to the video. Thumbnail: the most striking moment's "
        "second + 2-4 BIG words. Description: 2-3 sentences + 5 tags.\n"
        "Return JSON: {\"title\": \"\", \"description\": \"\", \"tags\": [\"\"], \"thumbnail\": {\"t\": 0, \"text\": \"\"}, "
        "\"sections\": [{\"title\": \"short title shown on screen\", \"start\": 0, \"end\": 0, \"speed\": 1, "
        "\"lines\": [{\"at\": 0, \"text\": \"\"}]}]}\n\nFOOTAGE LOG:\n" + "\n".join(lines))
    plan = ai([{"text": prompt}], "writing the script", temperature=0.7)
    if isinstance(plan, list):
        plan = plan[0] if plan else None
    return plan if isinstance(plan, dict) else None


def clean_plan(plan: dict, dur: float) -> dict:
    """Make the AI's plan safe to edit: sections inside the footage, at least 4 s, lines inside their section."""
    secs = []
    for s in plan.get("sections") or []:
        try:
            a, b = float(s.get("start", 0)), float(s.get("end", 0))
        except Exception:  # noqa: BLE001
            continue
        a, b = max(0.0, min(a, dur - 1)), max(0.0, min(b, dur))
        if b - a < 4:
            continue
        sp = float(s.get("speed") or 1)
        sp = min((1.0, 1.25, 1.5, 2.0), key=lambda v: abs(v - sp))
        lines = []
        for ln in s.get("lines") or []:
            txt = re.sub(r"\s+", " ", str((ln or {}).get("text", ""))).strip()
            txt = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", txt)
            if not txt:
                continue
            try:
                at = float(ln.get("at", a))
            except Exception:  # noqa: BLE001
                at = a
            lines.append({"at": max(a, min(at, b - 0.5)), "text": txt})
        lines.sort(key=lambda x: x["at"])
        secs.append({"title": re.sub(r"\s+", " ", str(s.get("title", "")))[:60].strip(), "start": a, "end": b,
                     "speed": sp, "lines": lines})
    plan["sections"] = secs
    th = plan.get("thumbnail") or {}
    try:
        tt = float(th.get("t", secs[0]["start"] if secs else 0))
    except Exception:  # noqa: BLE001
        tt = 0.0
    plan["thumbnail"] = {"t": max(0.0, min(tt, dur - 0.5)), "text": str(th.get("text", ""))[:40]}
    plan["title"] = str(plan.get("title") or "GTA 6 tips you need to know")[:100]
    return plan


# ------------------------------------------------------------------ 4. the voice
def voice_lines(plan: dict, voice: str) -> None:
    """Every line read by the AI voice -> wav + its length."""
    sys.path.insert(0, str(ROOT))
    import voice as V
    vdir = WORK / "voice"
    vdir.mkdir(exist_ok=True)
    n = 0
    for si, s in enumerate(plan["sections"]):
        for li, ln in enumerate(s["lines"]):
            wav = vdir / f"{si:02d}_{li:02d}.wav"
            if not wav.exists():
                x = V.tts(ln["text"], voice, speed=1.05)
                V.wav_write(wav, x)
            ln["wav"] = str(wav)
            ln["secs"] = duration_of(wav)
            n += 1
    log(f"voice: {n} lines read by {voice}")


# ------------------------------------------------------------------ 5. the edit
def fit(plan: dict, dur: float) -> None:
    """Place every line on the finished video's timeline so nothing overlaps and the footage under each line is the
    moment it talks about. If a section is too short for its lines: slow its speed-up, then use more of the footage
    after it, and only then drop the last lines."""
    for s in plan["sections"]:
        need = 0.3
        for ln in s["lines"]:
            need += ln["secs"] + 0.25
        need += 0.6
        out = (s["end"] - s["start"]) / s["speed"]
        if need > out and s["speed"] > 1:
            s["speed"] = max(1.0, (s["end"] - s["start"]) / need)
            out = (s["end"] - s["start"]) / s["speed"]
        if need > out:
            s["end"] = min(dur, s["start"] + need * s["speed"])
            out = (s["end"] - s["start"]) / s["speed"]
        while s["lines"] and need > out + 0.01:
            dropped = s["lines"].pop()
            need -= dropped["secs"] + 0.25
            log(f"line didn't fit its footage, dropped: {dropped['text'][:60]}")
        pos = 0.3
        for ln in s["lines"]:
            want = (ln["at"] - s["start"]) / s["speed"]
            ln["pos"] = max(pos, min(want, out - ln["secs"] - 0.3))
            pos = ln["pos"] + ln["secs"] + 0.25
        s["out"] = out


def card_png(text: str, idx: int, path: Path) -> None:
    """Tip title shown at the start of each tip (lower left)."""
    from PIL import Image, ImageDraw, ImageFont
    f1 = ImageFont.truetype(FONT, 58)
    f0 = ImageFont.truetype(FONT, 34)
    label = f"TIP {idx}" if idx > 0 else ""
    tw = max(f1.getlength(text), f0.getlength(label) if label else 0)
    bw, bh = int(tw + 80), 170 if label else 110
    im = Image.new("RGBA", (bw + 20, bh + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((10, 10, 10 + bw, 10 + bh), radius=22, fill=(12, 12, 16, 225))
    d.rounded_rectangle((10, 10, 22, 10 + bh), radius=6, fill=(255, 46, 136, 255))
    y = 22
    if label:
        d.text((50, y), label, font=f0, fill=(255, 46, 136, 255))
        y += 50
    d.text((50, y), text, font=f1, fill=(255, 255, 255, 255))
    im.save(path)


def thumbnail(master: Path, plan: dict, out: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance
    fr = WORK / "thumb_src.jpg"
    ff("-ss", f"{plan['thumbnail']['t']:.2f}", "-i", str(master), "-frames:v", "1", "-q:v", "2", str(fr))
    im = Image.open(fr).convert("RGB").resize((1280, 720))
    im = ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(1.35)).enhance(1.15)
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for x in range(700):   # dark fade on the left for the words
        d.line([(x, 0), (x, 720)], fill=(0, 0, 0, int(190 * (1 - x / 700))))
    im = Image.alpha_composite(im.convert("RGBA"), ov)
    words = (plan["thumbnail"]["text"] or "GTA 6 TIPS").upper().split()
    rows, cur = [], ""
    for w_ in words:
        if len(cur + " " + w_) > 10 and cur:
            rows.append(cur)
            cur = w_
        else:
            cur = (cur + " " + w_).strip()
    rows.append(cur)
    size = 118 if len(rows) <= 2 else 92
    f = ImageFont.truetype(FONT, size)
    d = ImageDraw.Draw(im)
    y = 360 - len(rows) * (size + 10) // 2
    for i, r in enumerate(rows):
        col = (255, 220, 40) if i == len(rows) - 1 else (255, 255, 255)
        d.text((56, y), r, font=f, fill=col, stroke_width=8, stroke_fill=(0, 0, 0))
        y += size + 10
    im.convert("RGB").save(out, quality=90)


def render(master: Path, plan: dict, out: Path) -> float:
    """The finished video: each section cut + sped up, tip titles, the voice on top, game sound ducked under it."""
    sdir = WORK / "secs"
    shutil.rmtree(sdir, ignore_errors=True)
    sdir.mkdir()
    lst = []
    t0, tip_no = 0.0, 0
    voice_track = []
    cards = []
    for i, s in enumerate(plan["sections"]):
        seg = sdir / f"{i:02d}.mp4"
        sp = s["speed"]
        a_f = ",".join(["atempo=2.0"] * int(math.log2(sp)) + ([f"atempo={sp / 2 ** int(math.log2(sp)):.4f}"]
                                                            if sp / 2 ** int(math.log2(sp)) > 1.001 else [])) or "anull"
        ff("-ss", f"{s['start']:.3f}", "-t", f"{s['end'] - s['start']:.3f}", "-i", str(master),
           "-filter_complex", f"[0:v]setpts=(PTS-STARTPTS)/{sp:.4f},fps={FPS}[v];[0:a]asetpts=PTS-STARTPTS,{a_f}[a]",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(seg))
        real = duration_of(seg)
        lst.append(f"file '{seg}'")
        is_intro = i == 0 and len(plan["sections"]) > 2
        is_outro = i == len(plan["sections"]) - 1 and len(plan["sections"]) > 2
        if s["title"] and not is_intro and not is_outro:
            tip_no += 1
            png = sdir / f"card{i:02d}.png"
            card_png(s["title"], tip_no, png)
            cards.append((png, t0 + 0.2, t0 + min(4.5, real - 0.3)))
        for ln in s["lines"]:
            voice_track.append((t0 + ln["pos"], ln["wav"]))
        s["out_start"] = t0
        t0 += real
    (sdir / "list.txt").write_text("\n".join(lst))
    body = WORK / "body.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(sdir / "list.txt"), "-c", "copy", str(body))
    total = duration_of(body)
    # the voice track: every line at its place on the timeline
    import wave
    vo = np.zeros(int((total + 1) * SR), np.float32)
    for pos, wav in voice_track:
        with wave.open(wav, "rb") as wf:
            sr, n, sw = wf.getframerate(), wf.getnframes(), wf.getsampwidth()
            raw = wf.readframes(n)
        x = (np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0) if sw == 2 else \
            np.frombuffer(raw, np.float32) if sw == 4 else np.zeros(1, np.float32)
        if sr != SR:
            x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * SR / sr)), np.arange(len(x)), x).astype(np.float32)
        i0 = int(pos * SR)
        seg_ = x[: max(0, len(vo) - i0)]
        vo[i0:i0 + len(seg_)] += seg_
    vwav = WORK / "voice_track.wav"
    with wave.open(str(vwav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((np.clip(vo, -1, 1) * 32767).astype(np.int16).tobytes())
    # final mix + tip titles
    inputs = ["-i", str(body), "-i", str(vwav)]
    flt, last = "", "0:v"
    for k, (png, a, b) in enumerate(cards):
        inputs += ["-i", str(png)]
        flt += f"[{last}][{k + 2}:v]overlay=60:H-h-90:enable='between(t,{a:.2f},{b:.2f})'[c{k}];"
        last = f"c{k}"
    flt += (f"[{last}]format=yuv420p[vout];"
            "[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0,volume=1.35,asplit=2[vo1][vo2];"
            "[0:a]volume=0.6[g];[g][vo1]sidechaincompress=threshold=0.015:ratio=12:attack=15:release=350[gd];"
            "[gd][vo2]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    ff(*inputs, "-filter_complex", flt, "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium",
       "-crf", "20", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(out))
    return duration_of(out)


def preview(full: Path, out: Path, secs: float) -> None:
    """Smaller copy that plays on the site (under GitHub's file limit)."""
    vb = max(350, min(2200, int(PREVIEW_MAX_MB * 8 * 1000 / max(secs, 1)) - 140))
    ff("-i", str(full), "-vf", "scale=-2:720" if vb >= 900 else "scale=-2:540", "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{vb}k",
       "-maxrate", f"{int(vb * 1.3)}k", "-bufsize", f"{vb * 2}k", "-c:a", "aac", "-b:a", "128k",
       "-movflags", "+faststart", str(out))


def chapters(plan: dict) -> list[dict]:
    out, tip = [], 0
    n = len(plan["sections"])
    for i, s in enumerate(plan["sections"]):
        if i == 0 and n > 2:
            name = "Intro"
        elif i == n - 1 and n > 2:
            name = "Outro"
        else:
            tip += 1
            name = f"Tip {tip}: {s['title']}" if s["title"] else f"Tip {tip}"
        out.append({"t": round(s.get("out_start", 0), 1), "title": name})
    if out:
        out[0]["t"] = 0
    return out


# ------------------------------------------------------------------ 6. put it on the site + release
def release_upload(path: Path, name: str, ctype: str) -> str:
    import requests
    code, rel = gh("GET", "/releases/tags/longform")
    if code != 200:
        code, rel = gh("POST", "/releases", {"tag_name": "longform", "name": "Long videos", "target_commitish": "main",
                                             "body": "Full-quality long videos (download for YouTube).", "prerelease": True})
    code, assets = gh("GET", f"/releases/{rel['id']}/assets?per_page=100")
    for a in assets if isinstance(assets, list) else []:
        if a.get("name") == name:
            gh("DELETE", f"/releases/assets/{a['id']}")
    with open(path, "rb") as fh:
        r = requests.post(f"https://uploads.github.com/repos/{REPO}/releases/{rel['id']}/assets?name={name}", data=fh,
                          timeout=1800, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype,
                                                 "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json().get("browser_download_url", "")


def publish_site(entry: dict, files: dict[str, Path]) -> None:
    """Add this video to the site's longs.json + its preview / thumbnail in v/l/, on top of the live site."""
    blob = {p: git("hash-object", "-w", str(f)) for p, f in files.items()}
    for attempt in range(8):
        git("fetch", "-q", "--depth=1", "origin", "+gh-pages:refs/remotes/origin/gh-pages")
        base = git("rev-parse", "origin/gh-pages")
        try:
            cur = json.loads(git("show", "origin/gh-pages:longs.json"))
        except RuntimeError:
            cur = {"videos": []}
        vids = [v for v in cur.get("videos", []) if v.get("id") != entry["id"]]
        vids.insert(0, entry)
        keep_prev = {v["id"] for v in vids[:KEEP_PREVIEWS]}
        for v in vids[KEEP_PREVIEWS:]:
            v["preview"] = ""   # older ones: watch / download the full file from the release
        lj = git("hash-object", "-w", "--stdin",
                 inp=json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "videos": vids[:40]},
                                indent=1, ensure_ascii=False).encode())
        idx = {"GIT_INDEX_FILE": "/tmp/idx-long"}
        Path("/tmp/idx-long").unlink(missing_ok=True)
        git("read-tree", base, env=idx)
        for p in git("ls-files", "v/l", env=idx).splitlines():
            vid = Path(p).stem.split("_")[0]
            if p.endswith(".mp4") and vid not in keep_prev and p not in blob:
                git("update-index", "--force-remove", p, env=idx)
        for p, sha in blob.items():
            git("update-index", "--add", "--cacheinfo", f"100644,{sha},{p}", env=idx)
        git("update-index", "--add", "--cacheinfo", f"100644,{lj},longs.json", env=idx)
        tree = git("write-tree", env=idx)
        c = git("-c", "user.name=gtaclip-bot", "-c", "user.email=gtaclip-bot@users.noreply.github.com",
                "commit-tree", tree, "-p", base, "-m", f"long video {entry['id']}")
        try:
            git("push", "-q", "origin", f"{c}:refs/heads/gh-pages")
            log("on the site: Long videos")
            return
        except RuntimeError as e:
            log(f"site changed, retrying ({attempt + 1}): {str(e)[:100]}")
            time.sleep(3 + attempt * 3)
    raise RuntimeError("couldn't publish to the site")


def status(job: dict, state: str, msg: str = "", extra: dict | None = None) -> None:
    """Progress the site can show while it's being made (longs.json entry with status)."""
    if os.environ.get("LF_LOCAL") or job.get("test"):
        log(f"[status] {state}: {msg}")
        return
    try:
        publish_site({"id": job["id"], "title": job.get("name") or "Your long video", "status": state, "msg": msg,
                      "created": job.get("created") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      **(extra or {})}, {})
    except Exception as e:  # noqa: BLE001
        log(f"status not shown: {e}")


# ------------------------------------------------------------------ main
def make(job: dict, num: int = 0) -> dict:
    game = job.get("game") or "GTA 6"
    voice = job.get("voice") or "am_michael"
    job.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    status(job, "making", "Getting your footage ready")
    src, sha = get_source(job)
    job["commit"] = sha
    master = make_master(src)
    dur = duration_of(master)
    status(job, "making", "Watching your footage")
    scenes = watch(master, game)
    if len(scenes) < 5:
        raise RuntimeError("Google's free AI daily limit is used up for today - tap Try again tomorrow" if DAILY["hit"]
                           else "the AI couldn't watch the footage (Google's free AI is busy) - tap Try again in a bit")
    status(job, "making", "Writing the script")
    plan = None
    for _ in range(3):
        plan = write_script(scenes, dur, game, job.get("topic", ""))
        if plan:
            plan = clean_plan(plan, dur)
            if len(plan["sections"]) >= 2:
                break
        plan = None
        time.sleep(30)
    if not plan:
        raise RuntimeError("the AI couldn't write a script right now (Google's free AI is busy)")
    (WORK / "plan.json").write_text(json.dumps(plan, indent=1))
    log(f"script: '{plan['title']}' - {len(plan['sections'])} sections, "
        f"{sum(len(s['lines']) for s in plan['sections'])} lines")
    status(job, "making", "Recording the voice")
    voice_lines(plan, voice)
    fit(plan, dur)
    status(job, "making", "Editing the video")
    full = WORK / f"{job['id']}.mp4"
    secs = render(master, plan, full)
    thumb = WORK / f"{job['id']}.jpg"
    thumbnail(master, plan, thumb)
    prev = WORK / f"{job['id']}_preview.mp4"
    preview(full, prev, secs)
    chap = chapters(plan)
    desc = (plan.get("description") or "").strip()
    desc_full = desc + "\n\n" + "\n".join(f"{mmss(c['t'])} {c['title']}" for c in chap) + \
        ("\n\n" + " ".join("#" + re.sub(r"\W", "", t) for t in plan.get("tags", [])[:6] if t) if plan.get("tags") else "")
    script = [{"section": s["title"], "t": round(s.get("out_start", 0) + ln["pos"], 1), "text": ln["text"]}
              for s in plan["sections"] for ln in s["lines"]]
    entry = {"id": job["id"], "status": "done", "title": plan["title"], "description": desc_full,
             "chapters": chap, "tags": plan.get("tags", []), "seconds": round(secs), "voice": voice,
             "created": job["created"], "source": job.get("name", ""), "script": script,
             "thumb": f"v/l/{job['id']}.jpg", "preview": f"v/l/{job['id']}_preview.mp4"}
    if os.environ.get("LF_LOCAL"):
        log(f"[local] done: {full} ({mmss(secs)})")
        entry["full"] = str(full)
        return entry
    if job.get("test"):   # a test run: results go to the "longtest" branch for checking, not on your site
        tdir = WORK / "testout"
        tdir.mkdir(exist_ok=True)
        for f in (prev, thumb, WORK / "plan.json", WORK / "scene_log.json"):
            shutil.copy(f, tdir / f.name)
        (tdir / "entry.json").write_text(json.dumps(entry, indent=1))
        idx = {"GIT_INDEX_FILE": "/tmp/idx-lftest"}
        Path("/tmp/idx-lftest").unlink(missing_ok=True)
        git("--work-tree", str(tdir), "add", "-A", env=idx)
        tree = git("write-tree", env=idx)
        c = git("-c", "user.name=gtaclip-bot", "-c", "user.email=gtaclip-bot@users.noreply.github.com",
                "commit-tree", tree, "-m", "long video test")
        git("push", "-qf", "origin", f"{c}:refs/heads/longtest")
        log("test results on the longtest branch")
        return entry
    entry["full"] = release_upload(full, f"{job['id']}.mp4", "video/mp4")
    entry["thumb_full"] = release_upload(thumb, f"{job['id']}.jpg", "image/jpeg")
    publish_site(entry, {f"v/l/{job['id']}.jpg": thumb, f"v/l/{job['id']}_preview.mp4": prev})
    return entry


def main() -> None:
    issue = json.loads(os.environ.get("ISSUE_JSON") or "{}")
    num = int(issue.get("number") or 0)
    if issue:
        body = issue.get("body") or "{}"
        job = json.loads(body[body.index("{"):body.rindex("}") + 1])
    else:
        job = json.loads(os.environ.get("LF_JOB") or "{}")
    job.setdefault("id", re.sub(r"^longform:\s*", "", issue.get("title", "")).strip() or str(int(time.time())))
    try:
        e = make(job, num)
        if job.get("test"):
            return
        comment(num, f"Your long video is ready: **{e['title']}** ({mmss(e['seconds'])}) - it's in **Long videos** on the "
                     f"site. Full quality for YouTube: {e.get('full', '')}\n\nChapters:\n" +
                "\n".join(f"{mmss(c['t'])} {c['title']}" for c in e["chapters"]))
        if num:
            gh("PATCH", f"/issues/{num}", {"state": "closed"})
            ref = job.get("ref") or f"upload-{job['id']}"
            gh("DELETE", f"/git/refs/heads/{ref}")   # the commit stays: it can be made again from it
    except Exception as e:  # noqa: BLE001
        log(f"long video failed: {e}")
        status(job, "failed", str(e)[:200])
        comment(num, f"Couldn't make the long video: {str(e)[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()

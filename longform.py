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
PAGES = os.environ.get("SITE_URL", "https://gtaclip-studio.pages.dev")   # Cloudflare since Nov 25 (GitHub Pages was capped at 1 GB)
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
KEEP_PREVIEWS = 4                # long-video previews kept on the site (Pages has a 1 GB limit, shared with the clips);
PREVIEW_MAX_MB = 100             # the full-quality files stay on the "longform" release


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
    if sha:   # a change of voice / a re-make: the footage's commit (the upload branch is gone by then)
        git("fetch", "-q", "--depth=1", "origin", sha)
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


def ai(parts: list, log_name: str, tries: int = 10, temperature: float = 0.4, strong: bool = False) -> dict | list | None:
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
    models = sorted(models, key=lambda m: "lite" in m)   # the full models first, the small "lite" ones last
    if strong:   # the fact-check: only the full models (the small one can make a good script worse)
        models = [m for m in models if "lite" not in m]
        if not models:
            log(f"{log_name}: only the small AI model is free right now - skipping this step")
            return None
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
        f"Make a video about {int(L // 60)}-{int(L // 60) + 2} minutes long: the sections' footage (after speed-ups) "
        f"should add up to about {int(L)} seconds, so give each tip 10-30 s of footage and 2-4 lines.\n"
        "STRUCTURE (copy how the biggest " + game + " guide channels do it): an intro section (10-18 s) on the most "
        "eye-catching moment. The intro's FIRST line is a warning/command with stakes, e.g. 'Do not start your next "
        "mission until you watch this video' or 'Stop playing " + game + " until you know these', then why it matters "
        "(you'll waste money / miss things you can't get back), then the promise with the exact count ('I'm going "
        "through N mistakes I wish someone warned me about'), then a tease that the list goes from something useful "
        "'all the way to' the biggest one. Then 4-10 tip sections, then a short outro.\n"
        "Each tip's FIRST line names it with its number and a link from the last one, e.g. 'Now that you've got "
        "your money sorted, mistake number two is...' / 'Tip number three is a big one.' Put the strongest tip near "
        "the end and say it's the most important. ONE time, at the start of a tip about halfway through, add a quick "
        "playful subscribe joke, e.g. 'The biggest mistake you can make is not subscribing. Nah, I'm kidding - but "
        "subscribe anyway, this channel covers everything " + game + ".' The outro: a short heartfelt last line "
        "(enjoy the game your own way) + subscribe for more " + game + " tips.\n"
        "EACH SECTION uses ONE continuous stretch of the footage that SHOWS that tip (start/end in seconds from the "
        "log), and its narration lines are tied to the moment they talk about: 'at' = the second in the footage when "
        "that thing is on screen. Use speed 1.5 or 2 for driving / travel stretches, 1 when the tip is happening.\n"
        "NARRATION RULES: talk to the viewer ('you'), clear and energetic, like a top GTA guide channel; each line "
        "8-22 words; say only things the log shows - never invent prices, numbers, place names or mechanics that "
        "aren't in it (on-screen text in [screen: ...] can be quoted); no emojis; numbers as words a voice can read. "
        "Each line must be about what is on screen at its 'at' second (within 2 s) - not about a moment before or after. "
        "If the title or the intro says a number of tips, it must equal the number of tip sections. "
        "Ignore streamer webcams / overlays / chat in the footage - talk only about the game. "
        "Keep enough footage per line: the voice reads about 2.2 words a second, so a section needs about "
        "(words / 2.2 + 1) seconds of footage after its speed-up.\n"
        "TITLE in the style of top GTA guide channels: a number + a strong claim + a CAPS word + the game in brackets, "
        "e.g. '9 Early Game Mistakes NOBODY Warned Me About (GTA 6)', 'Every SECRET Money Spot in GTA 6', "
        "'7 Things You MISSED in Chapter 1 (GTA 6)'. Max 70 characters, one or two CAPS words, true to the video - "
        "never promise something the footage doesn't show. Each tip section's title: 2-5 punchy words "
        "(e.g. 'Missing Free Money', 'Save Before Missions'). Thumbnail: the most striking moment's "
        "second + 2-4 BIG words. DESCRIPTION: line 1 a short urgent hook in title case ending in '...' "
        "(e.g. 'Do NOT Finish Chapter 1 Until You Do THIS...'), line 2 'The ULTIMATE " + game + " <topic> Guide: "
        "<the title's promise> (NO SPOILERS)' (only say NO SPOILERS if the narration has no story spoilers), then 1 "
        "sentence on what the video covers. 5 tags. Talk casually like a real gamer, never robotic.\n"
        "Return JSON: {\"title\": \"\", \"description\": \"\", \"tags\": [\"\"], \"thumbnail\": {\"t\": 0, \"text\": \"\"}, "
        "\"sections\": [{\"kind\": \"intro|tip|outro\", \"title\": \"short title shown on screen\", \"start\": 0, \"end\": 0, \"speed\": 1, "
        "\"lines\": [{\"at\": 0, \"text\": \"\"}]}]}\n\nFOOTAGE LOG:\n" + "\n".join(lines))
    plan = ai([{"text": prompt}], "writing the script", temperature=0.7)   # full models first, the small one if that's all
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
        k = str(s.get("kind", "")).lower().strip()
        secs.append({"kind": k if k in ("intro", "tip", "outro") else "", "title": re.sub(r"\s+", " ", str(s.get("title", "")))[:60].strip(),
                     "start": a, "end": b, "speed": sp, "lines": lines})
    if secs and not any(x["kind"] for x in secs):   # the AI didn't say: first = intro, last = outro (if there are tips between)
        for i, x in enumerate(secs):
            x["kind"] = "intro" if i == 0 and len(secs) > 2 else "outro" if i == len(secs) - 1 and len(secs) > 2 else "tip"
    for x in secs:
        x["kind"] = x["kind"] or "tip"
    plan["sections"] = secs
    th = plan.get("thumbnail") or {}
    try:
        tt = float(th.get("t", secs[0]["start"] if secs else 0))
    except Exception:  # noqa: BLE001
        tt = 0.0
    plan["thumbnail"] = {"t": max(0.0, min(tt, dur - 0.5)), "text": str(th.get("text", ""))[:40]}
    plan["title"] = str(plan.get("title") or "GTA 6 tips you need to know")[:100]
    fix_tip_count(plan)
    return plan


NUMW = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen twenty").split()


def fix_tip_count(plan: dict) -> None:
    """'5 Hidden Mechanics' with only 4 tips -> '4 Hidden Mechanics' (title, intro, outro, description).
    Never touches the game's own number (GTA 6 / Grand Theft Auto six)."""
    secs = plan.get("sections") or []
    n = sum(1 for x in secs if x.get("kind", "tip") == "tip")
    if n < 2:
        return
    num = r"(\d{1,2}|" + "|".join(NUMW[2:]) + r")"
    pat = (r"(?<!gta )(?<!gta)(?<!auto )(?<!\bvi )\b" + num +
           r"\b(?=\s+(?:[\w'-]+\s+){0,4}(?:tips?|tricks?|mechanics?|things?|secrets?|features?|ways?|hacks?|methods?|"
           r"mistakes?|details?|spots?|locations?|facts?|reasons?|items?|weapons?|cars?|skills?)\b)")

    def rp(m: re.Match) -> str:
        v = m.group(1)
        cur = int(v) if v.isdigit() else NUMW.index(v.lower())
        if cur == n:
            return v
        if v.isdigit():
            return str(n)
        w = NUMW[n] if n < len(NUMW) else str(n)
        return w.capitalize() if v[0].isupper() else w

    def fix(t: str) -> str:
        return re.sub(pat, rp, t or "", flags=re.I)

    old = plan.get("title", "")
    plan["title"] = fix(old)
    plan["description"] = fix(plan.get("description", ""))
    for s in secs:
        if s.get("kind") in ("intro", "outro"):
            for ln in s["lines"]:
                ln["text"] = fix(ln["text"])
    if plan["title"] != old:
        log(f"title said a different number of tips than the video has ({n}) - now: {plan['title']}")


def check_script(plan: dict, scenes: list[dict], game: str, dur: float) -> None:
    """A second AI pass, like an editor with the whole footage log: is each tip on footage that really shows it
    (else move it to where the log shows it), is each line at the second it talks about, and does it say only what
    the log shows (an invented item, mission, number, place or mechanic gets rewritten)."""
    secs = plan["sections"]
    tips = [i for i, x in enumerate(secs) if x["kind"] == "tip"]
    if not tips:
        return
    log_txt = "\n".join(f"{int(r['t'])}s | {r['what']}" + (f" [screen: {r['ui']}]" if r["ui"] else "") +
                        (f" (tip: {r['tip']})" if r["tip"] else "") + (" boring" if r["boring"] else "") for r in scenes)
    draft = [{"i": k, "kind": secs[i]["kind"], "title": secs[i]["title"], "start": secs[i]["start"], "end": secs[i]["end"],
              "lines": [{"at": ln["at"], "text": ln["text"]} for ln in secs[i]["lines"]]}
             for k, i in enumerate(i for i, x in enumerate(secs) if x["kind"] in ("intro", "tip"))]
    prompt = (f"You are the editor of a narrated {game} tips video. Below: the FOOTAGE LOG (what is on screen every "
              "few seconds) and the DRAFT script (sections with their footage start/end in seconds, and lines with "
              "the second 'at' they talk about). Fix the draft so it is TRUE TO THE FOOTAGE:\n"
              "1) a tip section must sit on footage that shows that tip - if the log shows it somewhere else, change "
              "start/end to that stretch (keep a similar length, 10-30 s);\n"
              "2) each line's 'at' must be the second when the log shows what the line says (inside its section);\n"
              "3) a line may only say what the log shows - rewrite anything invented (items, missions, story, numbers, "
              "places, controls, mechanics); keep it energetic, talking to 'you', 8-22 words, no emojis;\n"
              "4) don't talk about streamer webcams, chat or overlays - only the game.\n"
              "Leave alone, on purpose: the one playful subscribe joke at the start of a middle tip and the outro's "
              "subscribe line - they are part of the channel's style, not mistakes.\n"
              "Keep everything that is already right exactly as it is. Keep the same sections in the same order "
              "(same 'i'). The intro is a hook over the most eye-catching moment - only check its words.\n"
              "Return JSON: {\"sections\": [{\"i\": 0, \"start\": 0, \"end\": 0, \"lines\": [{\"at\": 0, \"text\": \"\"}]}], "
              "\"changes\": [\"short note of each fix\"]}\n\nDRAFT:\n" + json.dumps(draft) + "\n\nFOOTAGE LOG:\n" + log_txt)
    res = ai([{"text": prompt}], "checking the script against the footage", temperature=0.2, strong=True)
    if not isinstance(res, dict) or not isinstance(res.get("sections"), list):
        log("script check: no answer from the AI - keeping the first draft")
        return
    order = [i for i, x in enumerate(secs) if x["kind"] in ("intro", "tip")]
    fixed = 0
    for r in res["sections"]:
        try:
            k = int(r.get("i"))
            i = order[k]
            a, b = float(r.get("start")), float(r.get("end"))
        except Exception:  # noqa: BLE001
            continue
        sec = secs[i]
        a, b = max(0.0, min(a, dur - 1)), max(0.0, min(b, dur))
        if b - a < 4:
            continue
        lines = []
        for ln in r.get("lines") or []:
            txt = re.sub(r"\s+", " ", str((ln or {}).get("text", ""))).strip()
            txt = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", txt)
            try:
                at = float(ln.get("at", a))
            except Exception:  # noqa: BLE001
                at = a
            if 4 <= len(txt.split()) <= 26:
                lines.append({"at": max(a, min(at, b - 0.5)), "text": txt})
        if not lines:
            continue
        lines.sort(key=lambda x: x["at"])
        before = (round(sec["start"]), round(sec["end"]), [x["text"] for x in sec["lines"]])
        if sec["kind"] == "intro":   # only its words
            for old, new in zip(sec["lines"], lines):
                old["text"] = new["text"]
        else:
            sec.update(start=a, end=b, lines=lines)
        after = (round(sec["start"]), round(sec["end"]), [x["text"] for x in sec["lines"]])
        if after != before:
            fixed += 1
    for c in (res.get("changes") or [])[:12]:
        log(f"script check: {str(c)[:140]}")
    log(f"script checked against the footage: {fixed} of {len(order)} sections changed")


_STOP = set("""about after again also always before being between both could does doing down during each even every
from have here into just like make more most much must need only other over really right same should some such sure take
than that their them then there these they this those through very want when where which while will with without would
your youre yours what whats gonna gotta know dont game games player players character characters video screen screens
streamer streamers reacts reacting reaction watching shows showing shown gta grand theft auto vice city""".split())


def _toks(txt: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", (txt or "").lower()) if w not in _STOP}


def place_sections(plan: dict, scenes: list[dict], dur: float) -> None:
    """Without the AI: make sure each tip sits on footage that shows it (words of the tip vs the footage log) and
    each line is at the moment it talks about. The script AI sometimes puts a tip on the wrong part of the video."""
    if not scenes:
        return
    rows = [(r["t"], _toks(" ".join((r["what"], r["ui"], r["tip"], r["where"])))) for r in scenes]

    def win_score(toks: set, a: float, b: float) -> int:
        return sum(len(toks & tk) for t, tk in rows if a - 0.5 <= t <= b + 0.5)
    step = max(1.0, (rows[1][0] - rows[0][0]) if len(rows) > 1 else 2.0)
    for s in plan["sections"]:
        if s["kind"] != "tip":
            continue
        toks = _toks(s["title"] + " " + " ".join(x["text"] for x in s["lines"]))
        if len(toks) < 3:
            continue
        ln = s["end"] - s["start"]
        cur = win_score(toks, s["start"], s["end"])
        best, bt = cur, s["start"]
        t = 0.0
        while t + ln <= dur + 0.01:
            sc = win_score(toks, t, t + ln)
            if sc > best:
                best, bt = sc, t
            t += step
        if best >= max(4, cur * 2 + 1) and abs(bt - s["start"]) >= ln * 0.5:
            sh = bt - s["start"]
            log(f"'{s['title']}' was on the wrong footage ({mmss(s['start'])}) - moved to {mmss(bt)} where it shows")
            s["start"], s["end"] = bt, bt + ln
            for x in s["lines"]:
                x["at"] = x["at"] + sh
        for x in s["lines"]:   # each line at the moment in its section that matches it best
            lt = _toks(x["text"])
            cand = [(len(lt & tk), -abs(t - x["at"]), t) for t, tk in rows if s["start"] <= t <= s["end"] - 1]
            if cand:
                m = max(cand)
                if m[0] >= 2:
                    x["at"] = m[2]
            x["at"] = max(s["start"], min(x["at"], s["end"] - 0.5))
        s["lines"].sort(key=lambda x: x["at"])
        if s["lines"]:   # no long silent tail after the last line (fit() adds footage back if the voice needs it)
            last = s["lines"][-1]
            tail = last["at"] + (len(last["text"].split()) / 2.2 + 3) * s["speed"]
            if s["end"] > tail + 2 and tail - s["start"] >= 6:
                log(f"'{s['title']}': cut {s['end'] - tail:.0f}s of silent footage after its last line")
                s["end"] = tail
    for s in plan["sections"]:   # the intro: the most eye-catching stretch, not a boring / off-topic one
        if s["kind"] != "intro":
            continue
        ln = s["end"] - s["start"]

        def avg(a: float, b: float) -> float:
            v = [(0 if r["boring"] else r["score"]) for r in scenes if a - 0.5 <= r["t"] <= b + 0.5]
            return sum(v) / len(v) if v else 0.0
        tips = [(x["start"], x["end"]) for x in plan["sections"] if x["kind"] == "tip"]
        clash = lambda a, b: any(x < b and a < y for x, y in tips)   # the intro shouldn't show a tip's footage twice
        cur = avg(s["start"], s["end"]) if not clash(s["start"], s["end"]) else -1.0
        best, bt, t = cur, s["start"], 0.0
        while t + ln <= dur + 0.01:
            a = avg(t, t + ln)
            if a > best + 0.01 and not clash(t, t + ln):
                best, bt = a, t
            t += step
        if (best >= cur + 2 or cur < 0) and bt != s["start"]:
            sh = bt - s["start"]
            log(f"intro moved to the most eye-catching footage at {mmss(bt)} (score {best:.1f} vs {cur:.1f})")
            s["start"], s["end"] = bt, bt + ln
            for x in s["lines"]:
                x["at"] += sh


def tidy_outro(plan: dict, dur: float) -> None:
    """The outro shouldn't replay footage a tip just showed: move it to the next unused stretch."""
    secs = plan["sections"]
    if len(secs) < 3:
        return
    if secs[-1]["kind"] != "outro":
        return
    o = secs[-1]
    used = [(s["start"], s["end"]) for s in secs if s["kind"] == "tip"]
    hit = lambda a, b: any(x < b and a < y for x, y in used)
    if not hit(o["start"], o["end"]):
        return
    ln = o["end"] - o["start"]
    for c in [secs[-2]["end"]] + sorted(y for _, y in used):
        if c + ln <= dur and not hit(c, c + ln):
            sh = c - o["start"]
            o["start"] += sh
            o["end"] += sh
            for x in o["lines"]:
                x["at"] = min(o["end"] - 0.5, x["at"] + sh)
            log(f"outro moved to unused footage at {mmss(c)}")
            return


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
            if not wav.exists() or not ln.get("words"):
                try:
                    x, ln["words"] = V.tts_timed(ln["text"], voice, speed=1.05)
                except Exception as e:  # noqa: BLE001
                    log(f"word timings not available ({e}) - subtitles timed by length instead")
                    x, ln["words"] = V.tts(ln["text"], voice, speed=1.05), []
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
    order = [x for x in plan["sections"] if x["kind"] != "intro"] + [x for x in plan["sections"] if x["kind"] == "intro"]
    for s in order:   # tips first: the intro is only a hook, so it moves out of a tip's way
        need = 0.3
        for ln in s["lines"]:
            need += ln["secs"] + 0.25
        need += 0.6
        if s["kind"] == "intro":
            tips = sorted((x["start"], x["end"]) for x in plan["sections"] if x is not s)
            ln_ = max(s["end"] - s["start"], need * s["speed"])
            if any(a < s["start"] + ln_ and s["start"] < b for a, b in tips):   # clashes with a tip's footage now
                gaps, t0 = [], 0.0
                for a, b in tips + [(dur, dur)]:
                    if a - t0 >= ln_:
                        gaps.append(t0)
                    t0 = max(t0, b)
                if gaps:
                    st = min(gaps, key=lambda g: abs(g - s["start"]))
                    sh = st - s["start"]
                    s["start"], s["end"] = st, st + (s["end"] - s["start"])
                    for x in s["lines"]:
                        x["at"] += sh
                    log(f"intro moved to {mmss(st)} so it doesn't repeat a tip's footage")
        out = (s["end"] - s["start"]) / s["speed"]
        if need > out and s["speed"] > 1:
            s["speed"] = max(1.0, (s["end"] - s["start"]) / need)
            out = (s["end"] - s["start"]) / s["speed"]
        if need > out:   # more footage: after it first, then before it - never into another section's footage
            others = [(x["start"], x["end"]) for x in plan["sections"] if x is not s and x["kind"] != "intro"]
            if s["kind"] == "intro":
                others = [(x["start"], x["end"]) for x in plan["sections"] if x is not s]
            hi = min([a for a, _ in others if a >= s["end"] - 0.01] + [dur])
            lo = max([b for _, b in others if b <= s["start"] + 0.01] + [0.0])
            want_len = need * s["speed"]
            s["end"] = min(hi, s["start"] + want_len)
            if s["end"] - s["start"] < want_len:
                s["start"] = max(lo, s["end"] - want_len)
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


def card_png(text: str, idx: int, path: Path, w: int = 1920) -> None:
    """Tip title shown big across the top at the start of each tip (white caps, thick black outline) - the
    guide-channel look. Sized to fit the frame width."""
    from PIL import Image, ImageDraw, ImageFont
    text = text.upper()
    size = round(w * 0.05)
    f1 = ImageFont.truetype(FONT, size)
    while f1.getlength(text) > w * 0.86 and size > 30:
        size -= 2
        f1 = ImageFont.truetype(FONT, size)
    sw = max(4, round(size * 0.11))
    tw = int(f1.getlength(text))
    im = Image.new("RGBA", (tw + sw * 2 + 20, int(size * 1.35) + sw * 2 + 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((sw + 10 + 4, sw + 6), text, font=f1, fill=(0, 0, 0, 150))   # soft drop shadow
    d.text((sw + 10, sw), text, font=f1, fill=(255, 255, 255, 255), stroke_width=sw, stroke_fill=(0, 0, 0, 255))
    im.save(path)


THUMB_FONT = str(Path(__file__).resolve().parent / "fonts" / "Anton-Regular.ttf")   # heavy display font (OFL)


def _thumb_frames(master: Path, plan: dict, n: int = 3) -> list:
    """The best real frames for a thumbnail: sharp, colourful, well lit - picked from the intro, every tip and the
    AI's pick, at least 20 s apart (so the 3 options look different)."""
    import cv2
    import numpy as np
    from PIL import Image
    ts = [float(plan["thumbnail"]["t"])]
    for sec in plan["sections"]:
        a, b = float(sec["start"]), float(sec["end"])
        ts += [a + (b - a) * f for f in (0.25, 0.5, 0.75)]
    scored = []
    for k, t in enumerate(ts):
        fr = WORK / f"thc_{k}.jpg"
        try:
            ff("-ss", f"{max(0.0, t):.2f}", "-i", str(master), "-frames:v", "1", "-q:v", "2", "-vf", "scale=1280:-2", str(fr))
            im = cv2.imread(str(fr))
            if im is None:
                continue
            g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
            sharp = cv2.Laplacian(g, cv2.CV_64F).var()
            b_, g_, r_ = [im[:, :, i].astype(float) for i in range(3)]
            rg, yb = np.abs(r_ - g_), np.abs(0.5 * (r_ + g_) - b_)
            colour = np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
            light = g.mean()
            # menus / phone screens / map UI look flat: lots of blocks with almost no texture -> not thumbnail material
            sm = cv2.resize(g, (128, 72)).astype(float)
            blocks = sm.reshape(9, 8, 16, 8).std(axis=(1, 3))
            flat = float((blocks < 6).mean())
            score = np.log1p(sharp) * 1.0 + colour / 25 - abs(light - 120) / 60 - flat * 4 + (2.5 if k == 0 else 0)   # the script's own pick first
            scored.append((score, t, Image.open(fr).convert("RGB")))
        except Exception:  # noqa: BLE001
            continue
    scored.sort(key=lambda x: -x[0])
    out = []
    for sc, t, im in scored:
        if all(abs(t - t2) > 20 for _, t2, _ in out):
            out.append((sc, t, im))
        if len(out) >= n:
            break
    return [im for _, _, im in out] or [s_[2] for s_ in scored[:1]]


def _grade(im):
    """Punchy but real: a slight zoom (cuts the game's corner HUD), auto levels, colour, contrast, sharpening and
    a soft vignette - what a thumbnail editor does by hand."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
    w, h = im.size
    z = 1.12
    cw, ch = int(w / z), int(h / z)
    im = im.crop(((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch)).resize((1280, 720), Image.LANCZOS)
    im = ImageOps.autocontrast(im, cutoff=1)
    im = ImageEnhance.Color(im).enhance(1.4)
    im = ImageEnhance.Contrast(im).enhance(1.15)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=2))
    vig = Image.new("L", (1280, 720), 0)
    d = ImageDraw.Draw(vig)
    for i in range(60):   # radial vignette
        a = int(255 * (i / 60) ** 2)
        d.ellipse([-260 + i * 9, -200 + i * 6, 1540 - i * 9, 920 - i * 6], fill=a)
    vig = vig.filter(ImageFilter.GaussianBlur(60))
    dark = Image.new("RGB", (1280, 720), (0, 0, 0))
    return Image.composite(im, Image.blend(im, dark, 0.55), vig)


def _fit_font(text: str, max_w: int, max_size: int, min_size: int = 60):
    from PIL import ImageFont
    size = max_size
    while size > min_size:
        f = ImageFont.truetype(THUMB_FONT, size)
        if f.getbbox(text)[2] <= max_w:
            return f
        size -= 6
    return ImageFont.truetype(THUMB_FONT, min_size)


def _bold_text(im, xy, text, font, fill, stroke=10, anchor="la"):
    """Big thumbnail lettering: hard drop shadow + thick black outline (reads at phone size)."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    x, y = xy
    d.text((x + 9, y + 10), text, font=font, fill=(0, 0, 0), stroke_width=stroke, stroke_fill=(0, 0, 0), anchor=anchor)
    d.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0), anchor=anchor)


def _thumb_words(plan: dict) -> tuple[str, str]:
    """Two short lines: the hook number/words big on top, the payoff under it (e.g. '9 TIPS' / 'YOU NEED')."""
    words = (plan["thumbnail"].get("text") or "").upper().replace("-", " ").split()
    if not words:
        n = sum(1 for x in plan["sections"] if x.get("kind") == "tip")
        words = [str(n), "TIPS", "YOU", "NEED"] if n else ["GTA", "6", "TIPS"]
    if len(words) <= 2:
        return " ".join(words), ""
    cut = 2 if words[0].isdigit() or len(words[0]) <= 3 else 1
    return " ".join(words[:cut]), " ".join(words[cut:cut + 3])


def thumbnail(master: Path, plan: dict, out: Path) -> list[Path]:
    """3 hand-made-style thumbnail options from REAL frames of your footage (no AI art): the best-looking moments,
    graded like a thumbnail editor would, with big bold lettering in 3 proven layouts. `out` = option 1 (the one
    used by default); returns all 3."""
    from PIL import Image, ImageDraw
    frames = _thumb_frames(master, plan, 3)
    top, bot = _thumb_words(plan)
    outs = []
    for k in range(3):
        base = _grade(frames[k % len(frames)]).convert("RGBA")
        ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        if k == 0:   # 1) words left, dark fade behind them, yellow payoff
            for x in range(760):
                d.line([(x, 0), (x, 720)], fill=(0, 0, 0, int(200 * (1 - x / 760) ** 1.4)))
            im = Image.alpha_composite(base, ov).convert("RGB")
            f1 = _fit_font(top, 600, 250)
            f2 = _fit_font(bot, 600, 150) if bot else None
            h1 = f1.getbbox(top)[3]
            y = 360 - (h1 + (f2.getbbox(bot)[3] + 10 if f2 else 0)) // 2
            _bold_text(im, (60, y), top, f1, (255, 255, 255))
            if f2:
                _bold_text(im, (64, y + h1 + 12), bot, f2, (255, 214, 0))
        elif k == 1:   # 2) one huge line across the bottom on a yellow bar, small label on top
            for y_ in range(300):
                d.line([(0, 720 - y_), (1280, 720 - y_)], fill=(0, 0, 0, int(170 * (1 - y_ / 300))))
            im = Image.alpha_composite(base, ov).convert("RGB")
            line = (top + " " + bot).strip()
            f = _fit_font(line, 1120, 170)
            dd = ImageDraw.Draw(im)
            bb = dd.textbbox((0, 0), line, font=f)
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            x0 = (1280 - bw) // 2 - bb[0]
            y0 = 672 - bh - bb[1]   # the bar sits fully inside the picture
            bx = dd.textbbox((x0, y0), line, font=f)
            dd.rounded_rectangle([bx[0] - 30, bx[1] - 22, bx[2] + 30, bx[3] + 22], radius=16, fill=(255, 214, 0))
            dd.text((x0, y0), line, font=f, fill=(12, 12, 12))
        else:   # 3) big number in a red badge on the right, words on the left
            im = Image.alpha_composite(base, ov).convert("RGB")
            num = next((w for w in (top + " " + bot).split() if w.isdigit()), "")
            rest = " ".join(w for w in (top + " " + bot).split() if w != num) or "GTA 6"
            dd = ImageDraw.Draw(im)
            if num:
                dd.ellipse([980, 50, 1230, 300], fill=(225, 30, 45), outline=(255, 255, 255), width=10)
                fn = _fit_font(num, 180, 200)
                _bold_text(im, (1105, 178), num, fn, (255, 255, 255), stroke=6, anchor="mm")
            parts = rest.split()
            l1, l2 = " ".join(parts[:2]), " ".join(parts[2:4])
            f1 = _fit_font(l1, 860, 180)
            _bold_text(im, (56, 520 if l2 else 660), l1, f1, (255, 255, 255), anchor="ls")
            if l2:
                f2 = _fit_font(l2, 860, 150)
                _bold_text(im, (60, 675), l2, f2, (255, 214, 0), anchor="ls")
        p = out if k == 0 else out.with_name(out.stem + f"_{k + 1}.jpg")
        im.save(p, quality=92)
        outs.append(p)
    return outs


def sub_events(plan: dict) -> list[tuple[float, float, str]]:
    """Subtitles straight from the script (so the words are exactly what the narrator says), timed by when the voice
    actually says each word. Short chunks of a few words, one line, like YouTube captions."""
    ev = []
    for s in plan["sections"]:
        for ln in s["lines"]:
            base = s.get("out_start", 0) + ln["pos"]
            toks = []   # (word, start, end) - word pieces the voice splits ("ca"+"n't") joined back together
            prev_ws = " "
            for tx, st, en, ws in (ln.get("words") or []):
                if toks and (prev_ws == "" or not tx[:1].isalnum() and tx[:1] not in "$#"):
                    w0, s0, e0 = toks[-1]
                    toks[-1] = (w0 + tx, s0, max(e0, en))
                else:
                    toks.append((tx, st, en))
                prev_ws = ws
            spoken = " ".join(w for w, _, _ in toks)
            if not toks or abs(len(spoken) - len(ln["text"])) > max(6, len(ln["text"]) * 0.25):   # by length instead
                words = ln["text"].split()
                tot = sum(len(w) + 1 for w in words) or 1
                span = max(0.5, ln.get("secs", 2) - 0.15)
                acc, toks = 0.05, []
                for w in words:
                    d = span * (len(w) + 1) / tot
                    toks.append((w, acc, acc + d))
                    acc += d
            chunk = []
            groups = []
            for w in toks:
                chunk.append(w)
                text = " ".join(x[0] for x in chunk)
                if len(chunk) >= 5 or len(text) >= 24 or (w[0][-1:] in ".,!?;:" and len(chunk) >= 2):
                    groups.append(chunk)
                    chunk = []
            if chunk:
                if groups and len(chunk) <= 1 and len(" ".join(x[0] for x in groups[-1] + chunk)) <= 28:
                    groups[-1] += chunk
                else:
                    groups.append(chunk)
            for gi, g in enumerate(groups):
                a = base + g[0][1]
                b = base + (groups[gi + 1][0][1] if gi + 1 < len(groups) else g[-1][2] + 0.35)
                ev.append((a, max(b, a + 0.6), " ".join(x[0] for x in g)))
    ev.sort()
    for i in range(len(ev) - 1):   # never two on screen at once
        if ev[i][1] > ev[i + 1][0]:
            ev[i] = (ev[i][0], max(ev[i][0] + 0.3, ev[i + 1][0] - 0.02), ev[i][2])
    return ev


def _ts(t: float, srt: bool = False) -> str:
    t = max(0.0, t)
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s % 1) * 1000)) % 1000:03d}" if srt else f"{h}:{m:02d}:{s:05.2f}"


_STOP = set("the a an and or but to of in on at for with is are was were be it its this that you your youre "
             "you're can will just get got have has had not dont don't then than so if as by from up out into "
             "about there their they them what when where which who how all any one".split())


def _hot(words: list[str]) -> int:
    """Which word of a caption gets the colour: a number first, else the longest word that isn't filler."""
    best, bi = -1, -1
    for k, w in enumerate(words):
        c = re.sub(r"[^\w$#%']", "", w).lower()
        if not c or c in _STOP:
            continue
        sc = 100 if re.search(r"\d|\$", c) else len(c)
        if sc > best:
            best, bi = sc, k
    return bi if best >= 4 else -1


def write_subs(ev: list, w: int, h: int, ass: Path, srt: Path) -> None:
    """Burned-in subtitles in the guide-channel look: big white CAPS with a thick black outline, a few words at a
    time, one key word coloured (pink / blue in turn). The .srt keeps normal case for YouTube's own captions."""
    fs = max(30, round(h * 0.06))
    head = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
            f"PlayResX: {w}\nPlayResY: {h}\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Sub,Inter,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,"
            f"{max(3, round(fs * 0.12))},{max(1, round(fs * 0.05))},2,{round(w * 0.08)},{round(w * 0.08)},{round(h * 0.075)},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    cols = ["&HD84FFF&", "&HFFC21E&"]   # pink, blue (ASS is BGR)
    lines = []
    for n, (a, b, t) in enumerate(ev):
        words = t.replace(chr(92), "").replace("{", "(").replace("}", ")").upper().split()
        k = _hot(words)
        if k >= 0:
            words[k] = "{\\c" + cols[n % 2] + "}" + words[k] + "{\\c&HFFFFFF&}"
        lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Sub,,0,0,0,,{' '.join(words)}")
    ass.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    srt.write_text("\n".join(f"{i + 1}\n{_ts(a, True)} --> {_ts(b, True)}\n{t}\n" for i, (a, b, t) in enumerate(ev)),
                   encoding="utf-8")


def strip_voices(wav: Path) -> Path | None:
    """Take every voice out of the footage's own sound (another narrator, a streamer, chat TTS...) and keep the game
    sounds + music, so only our narrator talks. Uses Demucs (free, runs on the job's computer)."""
    out_dir = WORK / "sep"
    try:
        r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-j", "4",
                            "-o", str(out_dir), str(wav)], capture_output=True, text=True, timeout=3600)
        res = out_dir / "htdemucs" / wav.stem / "no_vocals.wav"
        if r.returncode == 0 and res.exists():
            return res
        log(f"couldn't take the voices out of the footage sound: {(r.stderr or r.stdout)[-300:]}")
    except Exception as e:  # noqa: BLE001
        log(f"couldn't take the voices out of the footage sound: {e}")
    return None


def render(master: Path, plan: dict, out: Path) -> float:
    """The finished video: each section cut + sped up, tip titles, the voice on top, game sound ducked under it."""
    sdir = WORK / "secs"
    shutil.rmtree(sdir, ignore_errors=True)
    sdir.mkdir()
    lst = []
    t0, tip_no = 0.0, 0
    voice_track = []
    m_w = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", subprocess.run([FF, "-hide_banner", "-i", str(master)],
                                                                  capture_output=True, text=True).stderr)
    VW = int(m_w.group(1)) if m_w else 1920
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
        if s["title"] and s.get("kind", "tip") == "tip":
            tip_no += 1
            png = sdir / f"card{i:02d}.png"
            card_png(s["title"], tip_no, png, VW)
            cards.append((png, t0 + 0.2, t0 + min(4.5, real - 0.3)))   # top centre: the subtitles are at the bottom
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
    # the footage's own sound without any voices in it (only our narrator talks)
    game = WORK / "game.wav"
    ff("-i", str(body), "-vn", "-ac", "2", "-ar", "44100", str(game))
    t_s = time.time()
    bg = strip_voices(game)
    if bg:
        log(f"took the voices out of the footage sound ({time.time() - t_s:.0f}s) - only the narrator talks now")
    # exact subtitles from the script
    m_ = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", subprocess.run([FF, "-hide_banner", "-i", str(body)],
                                                                   capture_output=True, text=True).stderr)
    w, h = (int(m_.group(1)), int(m_.group(2))) if m_ else (1920, 1080)
    ev = sub_events(plan)
    write_subs(ev, w, h, WORK / "subs.ass", WORK / "subs.srt")
    log(f"subtitles: {len(ev)} captions, word-timed from the narration")
    # final mix + tip titles + subtitles
    inputs = ["-i", str(body), "-i", str(vwav)] + (["-i", str(bg)] if bg else [])
    ga = "2:a" if bg else "0:a"
    k0 = len(inputs) // 2
    flt, last = "", "0:v"
    for k, (png, a, b) in enumerate(cards):
        inputs += ["-i", str(png)]
        flt += f"[{last}][{k + k0}:v]overlay=(W-w)/2:H*0.045:enable='between(t,{a:.2f},{b:.2f})'[c{k}];"
        last = f"c{k}"
    esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    flt += (f"[{last}]ass='{esc(WORK / 'subs.ass')}':fontsdir='{esc(ROOT / 'fonts')}',format=yuv420p[vout];"
            "[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0,volume=1.35,asplit=2[vo1][vo2];"
            f"[{ga}]aresample=48000,volume={0.7 if bg else 0.45}[g];"
            "[g][vo1]sidechaincompress=threshold=0.01:ratio=20:attack=10:release=400[gd];"
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


def _short_text(kicker: str, title: str, path: Path) -> None:
    """Top text of a tip Short: a small pink kicker + the tip in big white caps (black outline), 1080 wide."""
    from PIL import Image, ImageDraw, ImageFont
    words, lines = title.upper().split(), []
    size = 92
    while True:
        f = ImageFont.truetype(FONT, size)
        lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if f.getlength(t) > 960 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = t
        lines.append(cur)
        if len(lines) <= 2 or size <= 60:
            break
        size -= 6
    fk = ImageFont.truetype(FONT, 44)
    sw = max(5, round(size * 0.1))
    h = 70 + len(lines) * int(size * 1.12) + 20
    im = Image.new("RGBA", (1080, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    kw = fk.getlength(kicker)
    d.rounded_rectangle(((1080 - kw) / 2 - 18, 0, (1080 + kw) / 2 + 18, 58), radius=14, fill=(255, 46, 136, 255))
    d.text(((1080 - kw) / 2, 5), kicker, font=fk, fill=(255, 255, 255, 255))
    y = 74
    for ln in lines:
        w = f.getlength(ln)
        d.text(((1080 - w) / 2, y), ln, font=f, fill=(255, 255, 255, 255), stroke_width=sw, stroke_fill=(0, 0, 0, 255))
        y += int(size * 1.12)
    im.save(path)


def make_shorts(plan: dict, body: Path, full: Path, secs: float, game: str, n: int = 3) -> list[dict]:
    """2-3 vertical Shorts cut from the long video's best tips (the last tip - the 'most important' one - and the
    longest others): the gameplay big in the middle, the tip on top, the narrator's voice + game sound from the
    finished video. No subtitles (like the clip page). Each one should link to the full video on YouTube."""
    secs_l = plan["sections"]
    cand = []
    for i, s_ in enumerate(secs_l):
        if s_.get("kind", "tip") != "tip" or not s_.get("title"):
            continue
        a = float(s_.get("out_start", 0))
        b = float(secs_l[i + 1]["out_start"]) if i + 1 < len(secs_l) else secs
        if 8 <= b - a <= 58:
            cand.append((i, a, b))
    if not cand:
        return []
    tipno = {i: k + 1 for k, (i, _, _) in enumerate(c for c in cand)}
    last = cand[-1]
    rest = sorted(cand[:-1], key=lambda c: -(c[2] - c[1]))
    pick = sorted([last] + rest[:max(0, n - 1)], key=lambda c: c[1])
    tag = re.sub(r"\W", "", game)
    out = []
    for k, (i, a, b) in enumerate(pick):
        s_ = secs_l[i]
        png = WORK / f"short{k}.png"
        _short_text(f"{game.upper()} TIP", s_["title"], png)
        end = WORK / f"short{k}_end.png"
        _short_text("FULL GUIDE", "Watch the full video", end)
        dst = WORK / f"short{k}.mp4"
        d = b - a
        flt = ("[0:v]split=2[bg][fg];"
               "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:2,eq=brightness=-0.18[b];"
               "[fg]crop=w='min(iw,ih*1080/1216)':h=ih,scale=1080:1216[f];"
               "[b][f]overlay=0:352[v1];[v1][2:v]overlay=0:150[v2];"
               f"[v2][3:v]overlay=0:1180:enable='gte(t,{max(0.0, d - 2.6):.2f})',format=yuv420p[v]")
        ff("-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(body), "-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(full),
           "-i", str(png), "-i", str(end), "-filter_complex", flt, "-map", "[v]", "-map", "1:a",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
           "-ar", "48000", "-movflags", "+faststart", "-shortest", str(dst))
        title = f"{s_['title']} ({game})"[:90]
        out.append({"path": dst, "title": title, "tip": tipno.get(i, k + 1), "seconds": round(d),
                    "caption": f"{s_['title']} - full guide on my channel #{tag} #{tag}tips #gaming #shorts"})
    return out


def chapters(plan: dict) -> list[dict]:
    out, tip = [], 0
    word = "Mistake" if "mistake" in plan.get("title", "").lower() else "Tip"
    for i, s in enumerate(plan["sections"]):
        if s.get("kind") == "intro":
            name = "Intro"
        elif s.get("kind") == "outro":
            name = "Outro"
        else:
            tip += 1
            name = f"{word} #{tip} - {s['title']}" if s["title"] else f"{word} #{tip}"
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


def release_get(name: str) -> bytes | None:
    """A file from the long videos' release (the saved script of a video, for a change of voice)."""
    import requests
    code, rel = gh("GET", "/releases/tags/longform")
    for a in (rel.get("assets") or []) if code == 200 else []:
        if a.get("name") == name:
            r = requests.get(a["url"], timeout=120, headers={"Authorization": f"Bearer {TOKEN}",
                                                             "Accept": "application/octet-stream"})
            r.raise_for_status()
            return r.content
    return None


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
        old = next((v for v in cur.get("videos", []) if v.get("id") == entry["id"]), None)
        if old and old.get("status") == "done" and "preview" not in entry:
            # a new voice / re-make in progress: keep the finished video's details (title, links, script) meanwhile
            entry = {**old, **{k: v for k, v in entry.items() if k in ("status", "msg")}}
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
    given = None
    if job.get("revoice"):   # same video, new voice: the saved script, no AI needed (Nov 25)
        raw_plan = release_get(f"{job['id']}_plan.json")
        if not raw_plan:
            raise RuntimeError("this video was made before scripts were saved - make it again from your footage")
        given, scenes = json.loads(raw_plan), []
        log(f"new voice ({voice}) on the saved script - no Google AI needed")
    elif job.get("plan_ref"):   # the script was written by Claude in chat (no Google AI needed): use it as is
        ref = str(job["plan_ref"])
        subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", f"+{ref}:refs/remotes/origin/{ref}"], check=True)
        given = json.loads(subprocess.run(["git", "show", f"origin/{ref}:plan.json"], capture_output=True, text=True, check=True).stdout)
        sl = subprocess.run(["git", "show", f"origin/{ref}:scene_log.json"], capture_output=True, text=True)
        scenes = json.loads(sl.stdout) if sl.returncode == 0 and sl.stdout.strip() else []
        for r in scenes:
            for k, d in (("what", ""), ("where", ""), ("ui", ""), ("tip", ""), ("score", 5), ("boring", False)):
                r.setdefault(k, d)
        log(f"using the script written in chat ({ref}) - no Google AI needed")
    elif job.get("fake_ai"):   # test of everything but the AI (voice, edit, upload) when Google's daily limit is used up
        scenes = [{"t": float(t), "what": "test", "where": "", "ui": "", "tip": "", "score": 5, "boring": False}
                  for t in range(0, int(dur), 2)]
    else:
        scenes = watch(master, game)
    if len(scenes) < 5 and not given:
        raise RuntimeError("Google's free AI daily limit is used up for today - tap Try again tomorrow" if DAILY["hit"]
                           else "the AI couldn't watch the footage (Google's free AI is busy) - tap Try again in a bit")
    status(job, "making", "Writing the script")
    plan = None
    for _ in range(3):
        if given:
            plan = clean_plan(given, dur)
            break
        if job.get("fake_ai"):
            q = dur / 5
            plan = clean_plan({"title": "Test: GTA 6 tips", "description": "Test video.", "tags": ["GTA6"],
                               "thumbnail": {"t": q, "text": "TEST TIPS"},
                               "sections": [{"title": "", "start": 0, "end": min(12, q), "speed": 1,
                                             "lines": [{"at": 0, "text": "Here are three GTA six tips you probably missed, and the last one is the best."}]}]
                               + [{"title": f"Tip number {k}", "start": q * k, "end": q * k + q * 0.9, "speed": 1.5 if k == 2 else 1,
                                   "lines": [{"at": q * k + 1, "text": f"This is tip number {k}, shown right here on the screen."},
                                             {"at": q * k + q * 0.4, "text": "Watch how it works, it saves you a lot of time."}]} for k in (1, 2, 3)]
                               + [{"title": "", "start": q * 4, "end": q * 4 + 8, "speed": 1,
                                   "lines": [{"at": q * 4, "text": "Subscribe for more GTA six tips."}]}]}, dur)
            break
        plan = write_script(scenes, dur, game, job.get("topic", ""))
        if plan:
            plan = clean_plan(plan, dur)
            ntip = sum(1 for x in plan["sections"] if x["kind"] == "tip")
            if ntip >= 2:
                break
            log(f"script had only {ntip} tip(s) - asking again")
        plan = None
        time.sleep(30)
    if not plan:
        raise RuntimeError("the AI couldn't write a script right now (Google's free AI is busy)")
    log(f"script: '{plan['title']}' - {len(plan['sections'])} sections, "
        f"{sum(len(s['lines']) for s in plan['sections'])} lines")
    if not job.get("fake_ai") and not given:
        place_sections(plan, scenes, dur)
        check_script(plan, scenes, game, dur)
        place_sections(plan, scenes, dur)
    (WORK / "plan.json").write_text(json.dumps(plan, indent=1))
    status(job, "making", "Recording the voice")
    voice_lines(plan, voice)
    fit(plan, dur)
    tidy_outro(plan, dur)
    (WORK / "plan_fit.json").write_text(json.dumps(plan, indent=1, default=str))
    status(job, "making", "Editing the video")
    full = WORK / f"{job['id']}.mp4"
    secs = render(master, plan, full)
    thumb = WORK / f"{job['id']}.jpg"
    thumbs = thumbnail(master, plan, thumb)
    prev = WORK / f"{job['id']}_preview.mp4"
    preview(full, prev, secs)
    chap = chapters(plan)
    desc = (plan.get("description") or "").strip()
    desc_full = desc + "\n\nTimestamps:\n" + "\n".join(f"{mmss(c['t'])} {c['title']}" for c in chap) + \
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
        for f in (prev, thumb, WORK / "plan.json", WORK / "plan_fit.json", WORK / "scene_log.json", WORK / "subs.srt"):
            if f.exists():
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
    try:   # the script + the footage's commit: "Change voice" on the site re-makes it with no AI at all
        entry["plan"] = release_upload(WORK / "plan.json", f"{job['id']}_plan.json", "application/json")
        entry["commit"] = sha
    except Exception as e:  # noqa: BLE001
        log(f"script not saved: {e}")
    entry["thumb_full"] = release_upload(thumb, f"{job['id']}.jpg", "image/jpeg")
    entry["thumbs"] = [entry["thumb_full"]] + [release_upload(t_, f"{job['id']}_t{k + 2}.jpg", "image/jpeg") for k, t_ in enumerate(thumbs[1:])]
    try:   # AI thumbnails added before (Higgsfield) stay first when a video is re-made
        git("fetch", "-q", "--depth=1", "origin", "+gh-pages:refs/remotes/origin/gh-pages")
        cur_l = json.loads(git("show", "origin/gh-pages:longs.json"))
        prev_e = next((v for v in cur_l.get("videos", []) if v.get("id") == job["id"]), {})
        ai_t = [t for t in prev_e.get("thumbs", []) if "_ai" in t]
        if ai_t:
            entry["thumbs"] = ai_t + entry["thumbs"]
            entry["thumb_full"] = ai_t[0]
    except Exception:  # noqa: BLE001
        pass
    try:   # tip Shorts for the same channel - each links to this video
        shorts = make_shorts(plan, WORK / "body.mp4", full, secs, job.get("game") or "GTA 6")
        entry["shorts"] = [{k: v for k, v in sh.items() if k != "path"} |
                           {"url": release_upload(sh["path"], f"{job['id']}_s{n + 1}.mp4", "video/mp4")}
                           for n, sh in enumerate(shorts)]
        log(f"{len(shorts)} tip Shorts made from this video")
    except Exception as e:  # noqa: BLE001
        log(f"Shorts not made: {e}")
    if (WORK / "subs.srt").exists():   # the same subtitles as a file, for YouTube's own captions (upload it in Studio)
        entry["srt"] = release_upload(WORK / "subs.srt", f"{job['id']}.srt", "application/x-subrip")
    card = thumb
    if "_ai" in entry.get("thumb_full", ""):
        try:
            import requests
            card = WORK / "card_ai.jpg"
            card.write_bytes(requests.get(entry["thumb_full"], timeout=60).content)
        except Exception:  # noqa: BLE001
            card = thumb
    publish_site(entry, {f"v/l/{job['id']}.jpg": card, f"v/l/{job['id']}_preview.mp4": prev})
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
        if job.get("revoice"):   # the video you had stays as it was
            status(job, "done", f"Couldn't change the voice: {str(e)[:150]}")
        else:
            status(job, "failed", str(e)[:200])
        comment(num, f"Couldn't make the long video: {str(e)[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()

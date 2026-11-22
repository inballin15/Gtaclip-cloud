"""Find the viral moments in a long video (our own OpusClip-style clipper). Free, runs on CPU.

How it scores every second of the video (what highlight tools look at):
  * loudness spikes  - yelling, screaming, big reactions (vs the last minute's normal level)
  * hype words       - "no way", "let's go", "oh my god", "bro", laughing ("haha") ...
  * talking speed    - people talk faster when something is happening
  * on-screen action - big changes between frames (crashes, explosions, chases)
Then it builds clips around the best peaks:
  * starts at the beginning of a sentence a few seconds BEFORE the peak (the set-up = the hook)
  * ends at the end of a sentence once the reaction calms down (10-25 s)
  * never cuts mid-word, never overlaps another clip
Each clip gets a hook line: the most exciting sentence in it (clean, no swearing).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np

FF = imageio_ffmpeg.get_ffmpeg_exe()
HYPE = ["no way", "let's go", "lets go", "oh my god", "oh my gosh", "what the", "bro", "yo", "chat", "insane",
        "crazy", "wait", "what", "holy", "nah", "bruh", "lmao", "lol", "dude", "run", "go go", "help", "oh no",
        "are you serious", "did you see", "clip that", "clip it", "how", "why", "scream", "dead", "died", "omg"]
SWEARS = re.compile(r"\b(f+u+c*k+\w*|sh+i+t+\w*|bitch\w*|ass(hole)?|damn|dick|wtf|stfu|crap|bastard|motherf\w*|nigg\w*|fag\w*|retard\w*|pussy|cock)\b", re.I)
_MODEL = None


def _audio(path: Path, sr: int = 16000) -> np.ndarray:
    pcm = subprocess.run([FF, "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(sr), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0


def _motion(path: Path, secs: int) -> np.ndarray:
    """Per-second amount of change on screen (tiny grey frames at 2 fps)."""
    w, h = 64, 36
    raw = subprocess.run([FF, "-nostdin", "-loglevel", "error", "-i", str(path), "-vf", f"fps=2,scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8)
    n = len(fr) // (w * h)
    if n < 2:
        return np.zeros(secs)
    fr = fr[: n * w * h].reshape(n, h, w).astype(np.float32)
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    per = np.zeros(secs)
    for i, v in enumerate(d):
        s = min(secs - 1, (i + 1) // 2)
        per[s] = max(per[s], v)
    return per


def _z(x: np.ndarray, win: int = 60) -> np.ndarray:
    """How unusual each second is compared with the minute around it."""
    out = np.zeros_like(x)
    for i in range(len(x)):
        a, b = max(0, i - win), min(len(x), i + win)
        ref = x[a:b]
        med = np.median(ref)
        mad = np.median(np.abs(ref - med)) + 1e-6
        out[i] = (x[i] - med) / (1.4826 * mad)
    return np.clip(out, -3, 6)


def _smooth(x: np.ndarray, k: int = 3) -> np.ndarray:
    return np.convolve(x, np.ones(k) / k, mode="same") if len(x) >= k else x


def transcribe(audio: np.ndarray):
    global _MODEL
    from faster_whisper import WhisperModel
    if _MODEL is None:
        _MODEL = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _ = _MODEL.transcribe(audio, language="en", vad_filter=True, word_timestamps=True)
    out = []
    for s in segs:
        words = [(w.start, w.end, w.word) for w in (s.words or [])]
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words})
    return out


def clean_line(t: str) -> str:
    t = SWEARS.sub("", t)
    t = re.sub(r"[^\w\s'?!.,$%-]", "", t.replace("\u2019", "'"))
    t = re.sub(r"\s+", " ", t).strip(" -,.")
    return t


def find_moments(path: Path, max_clips: int | None = None, log=print, streamer: str = "") -> list[dict]:
    audio = _audio(path)
    secs = max(1, int(len(audio) / 16000))
    log(f"video is {secs // 60} min {secs % 60} s long — listening and watching for the best moments")
    # 1) loudness per second (dB)
    rms = np.array([np.sqrt(np.mean(audio[i * 16000:(i + 1) * 16000] ** 2) + 1e-9) for i in range(secs)])
    loud = _z(20 * np.log10(rms + 1e-9))
    # 2) what's said, with word times. Long videos: only transcribe the ~30 loudest stretches (fast on 6-hour VODs)
    if secs > 1200:
        sm = _smooth(np.clip(loud, 0, None), 15)
        picks, taken = [], np.zeros(secs, bool)
        for p0 in np.argsort(-sm):
            if len(picks) >= 30 or sm[p0] <= 0:
                break
            a0, b0 = max(0, int(p0) - 45), min(secs, int(p0) + 45)
            if taken[a0:b0].any():
                continue
            taken[a0:b0] = True
            picks.append((a0, b0))
        log(f"long video: listening closely to {len(picks)} promising stretches")
        segs = []
        for a0, b0 in sorted(picks):
            for sg in transcribe(audio[a0 * 16000:b0 * 16000]):
                sg["start"] += a0; sg["end"] += a0
                sg["words"] = [(x + a0, y + a0, w) for (x, y, w) in sg["words"]]
                segs.append(sg)
    else:
        segs = transcribe(audio)
    rate = np.zeros(secs)
    hype = np.zeros(secs)
    for s in segs:
        for (a, b, w) in s["words"]:
            i = min(secs - 1, int(a))
            rate[i] += 1
        low = s["text"].lower()
        k = sum(low.count(h) for h in HYPE) + 2 * len(re.findall(r"\b(ha){2,}|\bhaha", low)) + low.count("!")
        for i in range(int(s["start"]), min(secs, int(s["end"]) + 1)):
            hype[i] += k / max(1.0, s["end"] - s["start"])
    rate_z = _z(_smooth(rate, 3))
    hype = np.clip(hype, 0, 3)
    # 3) action on screen
    try:
        motion = _z(_motion(path, secs))
    except Exception:  # noqa: BLE001
        motion = np.zeros(secs)
    score = _smooth(1.0 * np.clip(loud, 0, None) + 0.9 * hype + 0.5 * np.clip(rate_z, 0, None)
                    + 0.5 * np.clip(motion, 0, None), 5)

    # 4) peaks -> clips
    want = max_clips or int(np.clip(secs / 240, 3, 12))
    order = np.argsort(-score)
    sent_starts = [s["start"] for s in segs] or [0.0]
    sent_ends = [s["end"] for s in segs] or [float(secs)]
    clips: list[dict] = []
    for p in order:
        if len(clips) >= want * 3 or score[p] < 0.6:
            break
        if any(c["start"] - 5 <= p <= c["end"] + 5 for c in clips):
            continue
        # start: a sentence beginning 4-14 s before the peak (the set-up)
        starts = [t for t in sent_starts if p - 9 <= t <= p - 2]
        start = min(starts) if starts else max(0.0, p - 6)
        # end: first sentence end after the reaction calms down, 10-25 s total (top clip pages: median 16 s)
        calm = next((t for t in range(int(p) + 3, min(secs, int(start) + 25)) if score[t] < 0.4), int(min(secs, start + 18)))
        ends = [t for t in sent_ends if calm <= t <= start + 25]
        end = min(ends) + 0.4 if ends else min(float(secs), max(start + 10, calm + 1.5))
        if end - start < 10:
            end = min(float(secs), start + 10)
        if end - start > 25:
            end = start + 25
        if any(not (end + 3 < c["start"] or start - 3 > c["end"]) for c in clips):
            continue
        a, b = int(start), int(end)
        window = np.sort(score[a:b])[::-1]
        val = float(window[:6].mean()) if len(window) else 0.0
        first = next((s["text"] for s in segs if s["start"] >= start - 0.5), "")
        reasons = []
        if loud[a:b].max(initial=0) > 2.5: reasons.append("big reaction")
        if hype[a:b].max(initial=0) > 1: reasons.append("hype words")
        if motion[a:b].max(initial=0) > 2.5: reasons.append("action on screen")
        if rate_z[a:b].max(initial=0) > 2: reasons.append("fast talking")
        if first.strip().endswith(("?", "!")): val += 0.4; reasons.append("strong opening line")
        lines = [s for s in segs if s["start"] >= start - 0.5 and s["end"] <= end + 0.5]
        best = max(lines, key=lambda s: hype[int(s["start"]):int(s["end"]) + 1].sum() + loud[int(s["start"]):int(s["end"]) + 1].max(initial=0), default=None)
        hook = clean_line(best["text"]) if best else ""
        if len(hook) > 70:
            hook = hook[:70].rsplit(" ", 1)[0]
        clips.append({"start": round(start, 2), "end": round(end, 2), "peak": int(p), "score": round(val, 2),
                      "reasons": reasons, "hook": hook,
                      "text": " ".join(s["text"] for s in lines)[:900]})
    if not clips and secs >= 15:   # short video / nothing stands out: still give the best part
        p = int(np.argmax(score)) if len(score) else 0
        start = max(0.0, min(p - 8.0, secs - 15.0)) if secs > 60 else 0.0
        end = min(float(secs), start + 20)
        lines = [s_ for s_ in segs if s_["start"] >= start - 0.5 and s_["end"] <= end + 0.5]
        best = max(lines, key=lambda s_: hype[int(s_["start"]):int(s_["end"]) + 1].sum(), default=None)
        clips.append({"start": round(start, 2), "end": round(end, 2), "peak": p, "score": 0.5,
                      "reasons": ["best part"], "hook": clean_line(best["text"])[:70] if best else "",
                      "text": " ".join(s_["text"] for s_ in lines)[:900]})
    clips = ai_rank(clips, log, streamer)   # smarter picks + hooks when a Gemini key is set (free tier); otherwise unchanged
    clips.sort(key=lambda c: -c["score"])
    return clips[:want]


_MODELS: list[str] | None = None


def gemini_models(key: str, log=print) -> list[str]:
    """Which Gemini models actually answer right now, fastest first. Google retires / overloads model names
    often, so: list the models this key can use, send each flash model a tiny test (8 s max) and keep the ones
    that answer."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    import json as _json
    import time
    import urllib.request
    names = []
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200"
        with urllib.request.urlopen(url, timeout=20) as r:
            for m in _json.loads(r.read()).get("models", []):
                n = m.get("name", "").split("/")[-1]
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                if not n.startswith("gemini") or "flash" not in n or any(
                        x in n for x in ("image", "tts", "audio", "live", "embedding", "vision", "computer", "robotics")):
                    continue
                names.append(n)
    except Exception as e:  # noqa: BLE001
        log(f"AI: couldn't list models ({str(e)[:80]})")
    names = sorted(set(names), key=lambda n: (-float((re.findall(r"(\d+(?:\.\d+)?)", n) or ["0"])[0]), "lite" in n))
    def probe(n):
        body = {"contents": [{"parts": [{"text": 'Return JSON [{"ok": 1}]'}]}],
                "generationConfig": {"responseMimeType": "application/json", "thinkingConfig": {"thinkingBudget": 0}}}
        t = time.time()
        try:
            req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{n}:generateContent?key={key}",
                                         data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                r.read()
            return (time.time() - t, n)
        except Exception:  # noqa: BLE001
            return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:   # test them all at once: ~8 s max instead of 8 s each
        good = [g for g in pool.map(probe, names[:8]) if g]
    _MODELS = [n for _, n in sorted(good)][:3] or ["gemini-3-flash-preview", "gemini-3.1-flash-lite-preview"]
    log(f"AI models answering now: {', '.join(_MODELS)}")
    return _MODELS


HOOK_STYLE = (
    "The on-screen hook copies the formula of the biggest GTA 6 clip pages (1-7M views each), kept SHORT: "
    "[streamer's well-known name] [CAPS emotion: SHOCKED / LOSES IT / FURIOUS / CRYING / STUNNED] "
    "[over / after / seeing] [ONE specific GTA 6 detail]. Or a 'FIRST' / 'just now' news angle. "
    "Real examples: 'Kai Cenat LOSES IT over Rakai stuck on mission 1' / 'IShowSpeed SHOCKED seeing the HULK in GTA 6' / "
    "'Kai FURIOUS that NPCs REMEMBERED him' / 'Lacy realizes you can't steal cars anymore' / "
    "'Clix CRYING after Jason dies'.\n"
    "Rules: SHORT - 25-60 characters, at most 10 words, one line of thought (1-2 lines on screen); cut every word "
    "that isn't needed (no 'during his marathon', 'in this intense moment', 'that he thought it was'); 2-4 CAPS words "
    "on the emotion and the key fact; one specific detail (Lucia, Jason, Vice City, a number, hours played, a first); "
    "exaggerate the reaction, but the event must come from the transcript, title or the streamer's real story "
    "(never invent fake records or crimes); no emojis, no swearing, no quotes, no hashtags.\n")

HOOK_MAX = 64   # automatic hooks longer than this get shortened (your own typed hooks are never touched)
_TAIL = re.compile(r"(?:,| -| during | while | because | despite | even though | as he | as she | after spending "
                   r"| after playing | immediately after | for (?:over )?\d+ hours)", re.I)


def short_hook(h: str, limit: int = HOOK_MAX) -> str:
    """Drop a trailing filler clause (', during his marathon', ' while ...') from an automatic hook if that brings it
    under the limit without losing the point. Anything still too long is rewritten by ai_shorten() later."""
    h = clean_line(h)
    if len(h) <= limit:
        return h
    cuts = [m.start() for m in _TAIL.finditer(h) if 28 <= m.start() <= limit]
    return h[:max(cuts)].strip(" ,-") if cuts else h


def ai_shorten(hooks: dict[str, str], log=print, tiny: bool = False) -> dict[str, str]:
    """One Gemini request: rewrite long hooks to 25-60 characters, same story, same name, same punch."""
    import json as _json
    import os
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not hooks:
        return {}
    size = "VERY SHORT: 18-40 characters, at most 6 words" if tiny else "SHORT: 25-60 characters, at most 10 words"
    prompt = (f"Rewrite each on-screen hook for a TikTok/YouTube Shorts clip so it is {size}. "
              "Keep the streamer's name exactly, keep the key event and 2-3 CAPS words, keep the meaning; cut "
              "filler ('during his marathon', 'in this intense moment', 'that he thought'). It must still read as a "
              "complete thought. Examples: 'Kai Cenat LOSES IT after finding out Rakai is STILL stuck on the FIRST "
              "mission in GTA 6 after 5 HOURS' -> 'Kai Cenat LOSES IT over Rakai stuck on mission 1'; 'Kai Cenat was "
              "SHOCKED that NPCS he FOUGHT earlier REMEMBERED him and tried to JUMP him in GTA 6' -> 'Kai SHOCKED NPCs "
              "REMEMBERED him and tried to JUMP him'.\nReturn JSON {id: short hook}.\n" + _json.dumps(hooks))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    for model in gemini_models(key, log):
        try:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                out = _json.loads(r.read())
            got = _json.loads(out["candidates"][0]["content"]["parts"][0]["text"])
            res = {}
            for k, v in (got.items() if isinstance(got, dict) else []):
                v = clean_line(str(v))
                if k in hooks and 14 <= len(v) <= (48 if tiny else 72):
                    res[k] = v
            return res
        except Exception as e:  # noqa: BLE001
            log(f"hook shortening ({model}): {str(e)[:120]}")
    return {}


TOPIC = {"name": "GTA 6"}   # what we're clipping (set by the pipeline from the dashboard's topic search)


LEARNED = {"guidance": "", "variant": ""}   # set by the pipeline from what the engagement learner found


def hook_style(variant: str | None = None) -> str:
    """The proven hook formula, re-aimed at the topic, plus what our own channel's views taught us,
    plus the style being tested on this clip."""
    import learn
    extra = LEARNED.get("guidance") or ""
    v = variant if variant is not None else (LEARNED.get("variant") or "")
    tiny = v.endswith("|tiny")
    v = v.split("|")[0]
    if tiny:
        extra += "LENGTH TEST for this clip: VERY short hook, 18-40 characters, at most 6 words (e.g. 'Kai FURIOUS at Lucia').\n"
    if v and v != "formula" and v in learn.VARIANTS:
        extra += ("STYLE TEST for this clip (this overrides the formula and CAPS rules above): "
                  + learn.VARIANTS[v] + "\n")
    return _base_style() + extra


def _base_style() -> str:
    name = TOPIC.get("name") or "GTA 6"
    if name == "GTA 6":
        return HOOK_STYLE
    return (HOOK_STYLE.replace("biggest GTA 6 clip pages", "biggest streamer clip pages")
            .replace("[one specific GTA 6 detail]", f"[one specific {name} detail]")
            .replace("(Lucia, Jason, Vice City, a number", "(a character, place, item, a number")
            + f"These clips are about {name}: write about {name} (e.g. end with 'in {name}' when natural). The examples above are "
              f"from GTA 6 only to show the style - never mention GTA unless the clip is GTA.\n")


def ai_rank(clips: list[dict], log=print, streamer: str = "") -> list[dict]:
    """Ask Gemini (one request for all candidates) which moments would actually go viral, and for a better hook.
    No key / quota used up / any error -> keep the signal-based ranking."""
    import json as _json
    import os
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not clips:
        return clips
    items = [{"i": i, "transcript": c["text"][:700], "signals": c["reasons"], "seconds": round(c["end"] - c["start"])}
             for i, c in enumerate(clips)]
    prompt = (
        f"You pick viral short-form clips (TikTok/YouTube Shorts) from {TOPIC.get('name', 'GTA 6')} livestreams, like OpusClip does.\n"
        "For each candidate, judge from the transcript: is it funny, surprising, dramatic or relatable? Does it have a "
        "clear setup and payoff? Does it make sense without context? Would the first 3 seconds stop the scroll?\n"
        + (f"The streamer in every candidate is {streamer}. Always use that exact name in the hook, never the word "
           f"'streamer'.\n" if streamer else "") +
        "Return JSON: a list of {\"i\": number, \"score\": 0-10, \"hook\": \"...\"}. " + hook_style() +
        "Score 0 for anything sexual, hateful, about strippers, or harming animals.\n\nCandidates:\n" + _json.dumps(items))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    for model in gemini_models(key, log):
        try:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=75) as r:
                out = _json.loads(r.read())
            text = out["candidates"][0]["content"]["parts"][0]["text"]
            ranked = _json.loads(text)
            if isinstance(ranked, dict):
                ranked = next((v for v in ranked.values() if isinstance(v, list)), [])
            top = max((c["score"] for c in clips), default=1) or 1
            for r in ranked:
                i = int(r.get("i", -1))
                if 0 <= i < len(clips):
                    c = clips[i]
                    ai = max(0.0, min(10.0, float(r.get("score", 5))))
                    c["ai_score"] = ai
                    c["score"] = round(0.4 * (c["score"] / top) * 10 + 0.6 * ai, 2)
                    h = short_hook(str(r.get("hook", "")))
                    if len(h) >= 12:
                        c["hook"], c["hook_ai"] = h, True
                    if ai <= 1:
                        c["score"] = -1   # AI says not safe / not good
            clips = [c for c in clips if c["score"] >= 0]
            log(f"AI picked the best moments ({model})")
            return clips
        except Exception as e:  # noqa: BLE001
            log(f"AI ranking skipped ({model}): {str(e)[:120]}")
    return clips


def cut(src: Path, start: float, end: float, out: Path) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "13", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)


def ai_score_clips(items: list[dict], log=print, budget_s: int = 60) -> tuple[dict, str]:
    """Viral score (0-100) for clips, like OpusClip's 'virality score'. One Gemini request per batch.
    items: [{id, streamer, title, transcript, seconds}]  ->  ({id: {score, verdict, why, hook}}, status)"""
    import json as _json
    import os
    import urllib.request
    import urllib.error
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {}, "no key"
    import time
    gemini_models(key, log)   # find working models first; this doesn't count against the rating time
    t0 = time.time()
    out, status = {}, "ok"
    for i in range(0, len(items), 10):
        if time.time() - t0 > budget_s:
            log("AI scoring: time budget used, the rest next run")
            break
        batch = [{"id": it["id"], "streamer": it.get("streamer", ""), "twitch_title": it.get("title", "")[:120],
                  "transcript": (it.get("transcript") or "")[:600], "seconds": it.get("seconds")} for it in items[i:i + 10]]
        prompt = (
            f"You rate short {TOPIC.get('name', 'GTA 6')} livestream clips for TikTok/YouTube Shorts like OpusClip's virality score.\n"
            "Score each 0-100 for how likely it is to go viral: a strong hook in the first seconds, a funny/shocking/"
            "dramatic or relatable moment, a clear payoff, makes sense without context, big-name streamer appeal.\n"
            "Give 0-10 for anything sexual, hateful, about strippers/clubs, harming animals, or where nothing happens.\n"
            "Also write the on-screen hook. " + hook_style() +
            "Return JSON list: [{\"id\": \"...\", \"score\": 0-100, \"why\": \"max 12 words\", \"hook\": \"...\"}]\n\n"
            "Clips:\n" + _json.dumps(batch))
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6,
                                     "thinkingConfig": {"thinkingBudget": 0}}}
        done = False
        for model in gemini_models(key, log):
            left = budget_s - (time.time() - t0)
            if left < 8:
                break
            try:
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=min(45, left)) as r:
                    res = _json.loads(r.read())
                rows = _json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
                if isinstance(rows, dict):
                    rows = next((v for v in rows.values() if isinstance(v, list)), [])
                for row in rows:
                    sc = max(0, min(100, int(float(row.get("score", 0)))))
                    out[str(row.get("id"))] = {
                        "score": sc, "why": clean_line(str(row.get("why", "")))[:90],
                        "verdict": "post" if sc >= 70 else "maybe" if sc >= 45 else "skip",
                        "hook": short_hook(str(row.get("hook", ""))), "model": model}
                done = True
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:160]
                status = f"{model}: HTTP {e.code} {msg}"
                log(f"AI scoring: {status}")
                if e.code == 400 and "thinking" in msg.lower():
                    body["generationConfig"].pop("thinkingConfig", None)   # model doesn't take that setting
                if e.code in (401, 403):
                    return out, status    # bad key: no point trying again
            except Exception as e:  # noqa: BLE001
                status = f"{model}: {str(e)[:120]}"
                log(f"AI scoring: {status}")
        if not done:
            break
    if out:
        status = "ok"
    log(f"AI scoring: {len(out)} clips rated ({status})")
    return out, status


def ai_visual_ok(jpegs: list[bytes], log=print, streamer: str = "", context: str = "", variant: str = "") -> tuple[bool | None, str, str]:
    """Look at a few frames of a clip (Gemini vision). False = nudity / sexual content / gore -> don't post.
    None = couldn't check (no key, quota, error)."""
    import base64
    import json as _json
    import os
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not jpegs:
        return None, "no key", ""
    parts = [{"text": f"These are frames (in order) from a {TOPIC.get('name', 'GTA 6')} livestream clip of {streamer or 'a streamer'} "
                      "(their webcam is in the stream) that will be posted on TikTok.\n"
                      f"What they said / stream title: {context[:600] or '(nothing)'}\n"
                      "1) Is there any nudity (bare breasts, genitals, bare buttocks, even in-game), sexual activity, "
                      "strip-club dancing, drug use, or graphic gore? ok=false means it must not be posted.\n"
                      "2) Write the on-screen hook about what ACTUALLY happens in these frames and what was said. "
                      + (f"Use the name {streamer} (never the word 'streamer'). " if streamer else "") + hook_style(variant) +
                      "Answer JSON {\"ok\": true|false, \"why\": \"...\", \"hook\": \"...\"}."}]
    for j in jpegs[:4]:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(j).decode()}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    import time as _t
    for attempt in range(3):          # Google's free tier says "too many requests" in bursts: wait and try again
        busy = False
        for model in gemini_models(key, log):
            try:
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    res = _json.loads(r.read())
                row = _json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
                if isinstance(row, list):
                    row = row[0] if row else {}
                return (bool(row.get("ok", True)), clean_line(str(row.get("why", "")))[:90],
                        short_hook(str(row.get("hook", ""))))
            except Exception as e:  # noqa: BLE001
                busy = busy or "429" in str(e) or "503" in str(e)
                log(f"picture check ({model}): {str(e)[:80]}")
        if not busy:
            break
        _t.sleep(20 * (attempt + 1))
    return None, "check failed", ""


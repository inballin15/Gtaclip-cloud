"""Find the viral moments in a long video (our own OpusClip-style clipper). Free, runs on CPU.

How it scores every second of the video (what highlight tools look at):
  * loudness spikes  - yelling, screaming, big reactions (vs the last minute's normal level)
  * hype words       - "no way", "let's go", "oh my god", "bro", laughing ("haha") ...
  * talking speed    - people talk faster when something is happening
  * on-screen action - big changes between frames (crashes, explosions, chases)
Then it builds clips around the best peaks:
  * starts at the beginning of a sentence a few seconds BEFORE the peak (the set-up = the hook)
  * ends at the end of a sentence once the reaction calms down (15-60 s)
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
    t = re.sub(r"[^\w\s'?!.,-]", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -,.")
    return t


def find_moments(path: Path, max_clips: int | None = None, log=print) -> list[dict]:
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
        starts = [t for t in sent_starts if p - 14 <= t <= p - 3]
        start = min(starts) if starts else max(0.0, p - 8)
        # end: first sentence end after the reaction calms down (score back near normal), 15-60 s total
        calm = next((t for t in range(int(p) + 4, min(secs, int(start) + 60)) if score[t] < 0.4), int(min(secs, start + 35)))
        ends = [t for t in sent_ends if calm <= t <= start + 60]
        end = min(ends) + 0.4 if ends else min(float(secs), max(start + 15, calm + 1.5))
        if end - start < 15:
            end = min(float(secs), start + 15)
        if end - start > 60:
            end = start + 60
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
        end = min(float(secs), start + 60)
        lines = [s_ for s_ in segs if s_["start"] >= start - 0.5 and s_["end"] <= end + 0.5]
        best = max(lines, key=lambda s_: hype[int(s_["start"]):int(s_["end"]) + 1].sum(), default=None)
        clips.append({"start": round(start, 2), "end": round(end, 2), "peak": p, "score": 0.5,
                      "reasons": ["best part"], "hook": clean_line(best["text"])[:70] if best else "",
                      "text": " ".join(s_["text"] for s_ in lines)[:900]})
    clips = ai_rank(clips, log)   # smarter picks + hooks when a Gemini key is set (free tier); otherwise unchanged
    clips.sort(key=lambda c: -c["score"])
    return clips[:want]


def ai_rank(clips: list[dict], log=print) -> list[dict]:
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
        "You pick viral short-form clips (TikTok/YouTube Shorts) from GTA 6 livestreams, like OpusClip does.\n"
        "For each candidate, judge from the transcript: is it funny, surprising, dramatic or relatable? Does it have a "
        "clear setup and payoff? Does it make sense without context? Would the first 3 seconds stop the scroll?\n"
        "Return JSON: a list of {\"i\": number, \"score\": 0-10, \"hook\": \"...\"}. The hook is on-screen text: "
        "max 65 characters, plain words, no emojis, no swearing, no quotes, one or two CAPS words for emphasis, true to "
        "what happens, ends with 'in GTA 6' when natural. Score 0 for anything sexual, hateful, about strippers, or "
        "harming animals.\n\nCandidates:\n" + _json.dumps(items))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        try:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
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
                    h = clean_line(str(r.get("hook", "")))[:70]
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
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)

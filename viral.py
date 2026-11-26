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


NAME_HINT = ("GTA 6, Vice City, Leonida, Lucia, Jason, Cal, Boobie, Dre'Quan, Real Dimez, Raul, Brian, Kai Cenat, "
             "IShowSpeed, Lacy, Clix, YourRAGE, xQc, Ludwig, N3on, Jynxzi, Rakai, Druski, Fanum, Duke Dennis, Agent 00, "
             "Adin Ross, HasanAbi, Caseoh, Cuffem, Stable Ronaldo, Plaqueboymax")


def transcribe(audio: np.ndarray):
    global _MODEL
    from faster_whisper import WhisperModel
    if _MODEL is None:
        _MODEL = WhisperModel("base", device="cpu", compute_type="int8")
    # name hints (FunClip "hotword" idea): streamer, game and character names come out spelled right in the
    # transcript, so hooks get the real specifics ("Lucia", "Rakai", "Druski") instead of guesses
    # (name hints were tried Nov 23 and removed: on quiet audio Whisper "heard" the hint names - "N3on, N3on, N3on")
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


def find_moments(path: Path, max_clips: int | None = None, log=print, streamer: str = "",
                 chat: np.ndarray | None = None) -> list[dict]:
    """chat: Twitch chat messages per second for the same stretch (optional). When chat floods ("LMAO", "NO WAY"),
    that's usually the moment people loved — the strongest single signal top clippers use."""
    audio = _audio(path)
    secs = max(1, int(len(audio) / 16000))
    chat_z = np.zeros(secs)
    if chat is not None and len(chat) and float(np.max(chat)) > 0:
        c = np.zeros(secs)
        c[:min(secs, len(chat))] = chat[:secs]
        c = np.roll(c, -4)   # chat reacts ~4 s after the moment happens
        c[-4:] = 0
        chat_z = np.clip(_z(_smooth(c, 5)), 0, 4)
        log(f"chat: {int(c.sum())} messages, busiest at {int(np.argmax(chat_z)) // 60} min {int(np.argmax(chat_z)) % 60} s")
    log(f"video is {secs // 60} min {secs % 60} s long — listening and watching for the best moments")
    # 1) loudness per second (dB)
    rms = np.array([np.sqrt(np.mean(audio[i * 16000:(i + 1) * 16000] ** 2) + 1e-9) for i in range(secs)])
    loud = _z(20 * np.log10(rms + 1e-9))
    # 2) what's said, with word times. Long videos: only transcribe the ~30 loudest stretches (fast on 6-hour VODs)
    if secs > 1200:
        sm = _smooth(np.clip(loud, 0, None) + 1.2 * chat_z, 15)
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
                    + 0.5 * np.clip(motion, 0, None) + 1.2 * chat_z, 5)

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
        # start: a sentence beginning ~6-14 s before the peak. Clipper study (Nov 22, 128 clips timed): in 100K+ view
        # clips the loudest moment sits around the MIDDLE (~12 s of ~25 s); they open on talk/action, then build up.
        starts = [t for t in sent_starts if p - 14 <= t <= p - 6]
        start = min(starts, key=lambda t: abs(t - (p - 10))) if starts else max(0.0, p - 10)
        start = max(0.0, start - 0.6)   # a breath before the first word (never clip the start of the setup line)
        # end: first sentence end after the reaction calms down, 10-25 s total (top clip pages: median 16 s)
        calm = next((t for t in range(int(p) + 3, min(secs, int(start) + 30)) if score[t] < 0.4), int(min(secs, start + 22)))
        ends = [t for t in sent_ends if calm <= t <= start + 30]
        end = min(float(secs), min(ends) + 1.5) if ends else min(float(secs), max(start + 10, calm + 2.0))
        end = max(end, min(float(secs), p + 5.0))   # the reaction after the big moment always lands
        if end - start < 10:
            end = min(float(secs), start + 10)
        if end - start > 30:
            end = start + 30
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
        return [m for m in _MODELS if m not in _TIRED]
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
        good = [g for g in pool.map(probe, names[:14]) if g]
    # each model has its own free limit: when Google is busy (503) or one model's limit is used up (429), the
    # next one usually still answers - so keep up to 5, with older stable models as the backup
    backup = ["gemini-3-flash-preview", "gemini-3.1-flash-lite-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite",
              "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.0-flash"]
    _MODELS = [n for _, n in sorted(good)][:6] or backup[:4]   # only models that answered the test (Nov 24: an
    log(f"AI models answering now: {', '.join(_MODELS)}")      # untested backup answered "not found" all day)
    return [m for m in _MODELS if m not in _TIRED]


_TIRED: dict = {}   # models that said "too many requests" / "not found" this run - don't ask them again (saves the
                    # free daily allowance for the models that still have some left)


def _tired(model: str, err) -> None:
    t = str(err)
    if "429" in t or "404" in t:
        _TIRED[model] = t[:40]


HOOK_STYLE = (
    "The on-screen hook copies what the biggest streamer clip pages do on their winning videos (clipper study, Nov 22: "
    "974 videos; 100K+ view hooks average ~13 words): ONE story sentence telling what happened - "
    "[streamer's well-known name] [emotional past-tense verb: cried / was devastated / lost it / was shocked / "
    "FREAKED OUT / got upset] [after / finding out / when] [the specific thing that happened]. "
    "Real winners: 'Kai Cenat devastated finding out you can't free roam after GTA 6 ends' (2.4M) / "
    "'Speed picked up and threw a Chameleon at an NPC in GTA 6' (1.6M) / 'Kai was upset GTA 6 driving is too "
    "difficult and says it's not fun' (1.8M) / 'IShowSpeed did NOT expect this HAPPY ENDING in GTA 6' (1.6M) / "
    "'Kai Cenat Failing A Mission Because He Couldn't Hop On A Train' (5.7M).\n"
    "ORDER: who + how they reacted, then the concrete story detail (a character, mission, place, item, number, "
    "a first). The detail makes people stop - it must come from the transcript, title or the streamer's real story "
    "(never invent fake records or crimes). Never write it like a search phrase ('GTA 6 Lacy shoe store gameplay').\n"
    "TRUTH FIRST: the hook must describe what really happens in THIS clip. Start from the Twitch clip title (the "
    "viewer who clipped it wrote what happened) and the transcript; every name, number and event must appear in "
    "one of them - copy numbers exactly. If the transcript is short or unclear, stay close to the title. A boring "
    "true hook beats an exciting false one.\n"
    "Pick the reaction word that fits what you can hear (laughed, raged, panicked, couldn't believe, was confused, "
    "got emotional, roasted, lost it) - 'SHOCKED' at most rarely; don't reuse one word for every clip.\n"
    "Rules: 8-15 words, 45-95 characters, reads like a friend telling you what happened; normal sentence case with "
    "AT MOST ONE CAPS word (or one short CAPS phrase like 'LOST IT') on the strongest emotion, or none at all (e.g. "
    "'Lacy was DEVASTATED after losing 39% of his GTA 6 progress'); on our own channel hooks with 0-1 CAPS words got "
    "~1.8x the views of hooks with more. Use a STRONG reaction verb (lost it, freaked out, "
    "was devastated, couldn't believe, got destroyed, raged, cried, panicked, roasted) - never weak ones (was happy / "
    "curious / excited / confused / struggling). Keep the most striking specific from the transcript or title "
    "(a number, a name, the exact thing: 'scam call', 'dead body', '39%'); never vague words like 'in game', "
    "'during a mission', 'a mission' when the real name or thing is known; say 'in GTA 6' when it's GTA 6. "
    "No filler ('in this intense moment'); no emojis, no swearing, no quotes, no hashtags.\n")

_ACRONYMS = {"GTA", "NPC", "NPCS", "FBI", "CEO", "USA", "PC", "PS5", "PS4", "AI", "IV", "VI", "V", "FPS", "RP", "NBA",
             "NFL", "UFO", "LOL", "OMG", "WTF", "TV", "DJ", "OG", "MVP", "XP", "HP", "IRL", "VR", "RNG", "ID", "UK", "LA",
             "NYC", "KO", "TTS", "NICKMERCS", "CASEOH", "XQC"}


def limit_caps(hook: str, keep: int = 1) -> str:
    """At most one CAPS word / short CAPS phrase ('LOST IT') per hook - our channel's data (Nov 24): hooks with 0-1
    CAPS words got ~1.8x the views. Extra shouted words go back to normal case; acronyms (GTA, NPC) stay."""
    words = (hook or "").split(" ")
    out, runs, prev_caps = [], 0, False
    for w in words:
        core = re.sub(r"[^A-Za-z]", "", w)
        is_caps = len(core) >= 2 and core.isupper() and core not in _ACRONYMS
        if is_caps:
            if not prev_caps:
                runs += 1
            if runs > keep:
                w = w.lower()
        prev_caps = is_caps
        out.append(w)
    if out and words and out[0] != words[0]:   # only re-capitalise a first word we changed
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


def hook_is_true(hook: str, title: str, transcript: str) -> bool:
    """Every number in the hook must really be in the clip's title or transcript (a hook said xQc rated GTA 6 a
    '7.8' when he said 9). Made-up numbers = a made-up hook."""
    src = f"{title} {transcript}".lower()
    hook = re.sub(r"(?i)\bgta\s*(6|vi|5|v)\b", "GTA", hook)   # the game's name isn't a claim
    for n in re.findall(r"\d+(?:\.\d+)?", hook):
        if n not in src and n.rstrip("0").rstrip(".") not in src:
            return False
    return True


def title_hook(streamer: str, title: str) -> str:
    """Fallback hook from the Twitch clip title: viewers who clipped it wrote what actually happened."""
    t = clean_line(re.sub(r"[#@]\S+|https?://\S+", "", title or "")).strip(" -|!?.")
    if len(t.split()) < 3 or len(t) > 95:
        return ""
    if streamer and streamer.split()[0].lower() not in t.lower():
        t = f"{streamer} {t[0].lower() + t[1:]}" if not t[:2].isupper() else f"{streamer}: {t}"
    return t if streamer and t.startswith(streamer) else t[0].upper() + t[1:]


HOOK_MAX = 95   # automatic hooks longer than this get shortened (your own typed hooks are never touched)
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
    size = "SHORT: 25-60 characters, at most 10 words" if tiny else "one story sentence, 45-90 characters, 8-15 words"
    prompt = (f"Rewrite each on-screen hook for a TikTok/YouTube Shorts clip so it is {size}. "
              "Keep the streamer's name exactly, keep the key event and at most ONE CAPS word, keep the meaning; cut "
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
                if k in hooks and 14 <= len(v) <= (68 if tiny else 100):
                    res[k] = v
            return res
        except Exception as e:  # noqa: BLE001
            _tired(model, e)
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
        extra += "LENGTH TEST for this clip: SHORT hook, 25-60 characters, at most 10 words (e.g. 'Kai Cenat LOSES IT over Rakai stuck on mission 1').\n"
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
            _tired(model, e)
            log(f"AI ranking skipped ({model}): {str(e)[:120]}")
    return clips


def cut(src: Path, start: float, end: float, out: Path, vf: str | None = None) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}",
                    *(["-vf", vf] if vf else []),
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
        # a VOD moment's "title" is the whole stream's title, not this moment ("DEATH BATTLE DAY!!!! MORE REACTS...")
        # - hooks built from it had nothing to do with the clip
        batch = [{"id": it["id"], "streamer": it.get("streamer", ""),
                  **({"stream_title_NOT_this_moment": it.get("title", "")[:80]} if str(it["id"]).startswith("vod")
                     else {"twitch_title": it.get("title", "")[:120]}),
                  "transcript": (it.get("transcript") or "")[:600], "seconds": it.get("seconds")} for it in items[i:i + 10]]
        prompt = (
            f"You rate short {TOPIC.get('name', 'GTA 6')} livestream clips for TikTok/YouTube Shorts like OpusClip's virality score.\n"
            "Score each 0-100 for how likely it is to go viral: a strong hook in the first seconds, a funny/shocking/"
            "dramatic or relatable moment, a clear payoff, makes sense without context, big-name streamer appeal.\n"
            "Give 0-10 for anything sexual, hateful, about strippers/clubs, harming animals, or where nothing happens.\n"
            "Also write the on-screen hook. " + hook_style() +
            "If the streamer is REACTING to someone else's video / stream, the hook is about the streamer's reaction "
            "(the streamer is the subject, e.g. 'Jynxzi ROASTED Nick Eh 30 ...'), not about the person in the video.\n"
            "If a clip's streamer is empty, their name is UNKNOWN: never guess one (no names from the examples) - "
            "only use a name that is said in that clip's transcript or title.\n" +
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
                src = {it["id"]: it for it in items}
                for row in rows:
                    sc = max(0, min(100, int(float(row.get("score", 0)))))
                    it = src.get(str(row.get("id"))) or {}
                    hk = short_hook(str(row.get("hook", "")))
                    vod = str(it.get("id", "")).startswith("vod")
                    if hk and not hook_is_true(hk, "" if vod else it.get("title", ""), it.get("transcript", "")):
                        hk = "" if vod else (title_hook(it.get("streamer", ""), it.get("title", "")) or "")
                    if vod and len((it.get("transcript") or "").split()) < 12:
                        sc = min(sc, 40)   # barely any talking: nothing to hook on - don't post it
                    out[str(row.get("id"))] = {
                        "score": sc, "why": clean_line(str(row.get("why", "")))[:90],
                        "verdict": "post" if sc >= 70 else "maybe" if sc >= 45 else "skip",
                        "hook": hk, "model": model}
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
                _tired(model, e)
                status = f"{model}: {str(e)[:120]}"
                log(f"AI scoring: {status}")
        if not done:
            break
    if out:
        status = "ok"
    log(f"AI scoring: {len(out)} clips rated ({status})")
    return out, status


LAST_VISUAL: dict = {}   # extra answers from the last picture check: react, watching, owner_cam


def ai_visual_ok(jpegs: list[bytes], log=print, streamer: str = "", context: str = "", variant: str = "") -> tuple[bool | None, str, str]:
    """Look at a few frames of a clip (Gemini vision). False = nudity / sexual content / gore -> don't post.
    None = couldn't check (no key, quota, error)."""
    import base64
    import json as _json
    import os
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    LAST_VISUAL.clear()
    if not key or not jpegs:
        return None, "no key", ""
    parts = [{"text": f"These are frames (in order) from a {TOPIC.get('name', 'GTA 6')} livestream clip of {streamer or 'a streamer'} "
                      "(their webcam is in the stream) that will be posted on TikTok.\n"
                      f"What they said / stream title: {context[:600] or '(nothing)'}\n"
                      "1) Is there any nudity (bare breasts, genitals, bare buttocks, even in-game), sexual activity, "
                      "strip-club dancing, drug use, or graphic gore? ok=false means it must not be posted.\n"
                      "Also ok=false if the streamer is away: their own camera shows an empty chair / empty room while "
                      "the only person on screen is a guest in a different, smaller webcam window. (A clip of the channel "
                      "owner must show the channel owner.)\n"
                      "Also ok=false if the main (non-webcam) part of the screen is a desktop, OBS / stream software, a chat "
                      "window, a browser page, or a menu-only screen.\n"
                      "REACT: if the streamer is WATCHING someone else's stream / video / TikTok / YouTube (another "
                      "creator's content fills the main screen - often with that creator's own webcam inside it), that is "
                      "fine (ok=true unless the rules above are broken, also inside the watched content): set react=true, "
                      "watching = that creator's name if it is shown or said (else \"\"), and owner_cam = the box "
                      "[ymin, xmin, ymax, xmax] (0-1000) in the FIRST frame of the CHANNEL OWNER's own webcam - the overlay "
                      "on top of everything, usually at an edge or corner - NOT a face that is part of the video being "
                      "watched. With TWO webcams on screen (e.g. one streamer reacting to another streamer's video that has "
                      "its own facecam), the owner's webcam is the one sitting in the SAME spot in every frame, drawn on top "
                      "of the watched video; the other face moves / changes size with the video. The whole webcam, head and "
                      "shoulders, not just the face. Also watched_box = the box [ymin, xmin, ymax, xmax] (0-1000) in the FIRST frame of just the "
                      "content being watched (the video / stream / TikTok itself - not the owner's webcam, chat or the "
                      "stream's side decorations; for a vertical TikTok in the middle, just that tall area). "
                      "NOT a react (react=false): the streamer PLAYING the game themselves - the game's HUD / minimap / "
                      "money and health bars are on screen and the gameplay is theirs - even when a second small webcam "
                      "(a duo partner, a co-streamer, a guest) is also on screen.\n"
                      "Otherwise react=false, owner_cam=null, watched_box=null.\n"
                      f"in_game = true only if the main screen shows {TOPIC.get('name', 'GTA 6')} itself for most of the clip "
                      "(false for real life / IRL, a desktop or browser page, a video of something else, or another game).\n"
                      "2) Write the on-screen hook about what ACTUALLY happens in these frames and what was said. "
                      f"Only say '{TOPIC.get('name', 'GTA 6')}' in the hook if in_game is true. "
                      "For a react: the hook is about the streamer's REACTION to what they are watching (name who they "
                      "are watching if you know it). "
                      + (f"Use the name {streamer} (never the word 'streamer'). " if streamer else "") + hook_style(variant) +
                      "3) question = the FIRST line of the TikTok caption: one short question (max 14 words) about this exact "
                      "moment that viewers will want to answer in the comments - like 'Did you know about these fighting "
                      "combos or were you just pressing square?' or 'Would you have survived that or nah?'. Plain words, no "
                      "hashtags, no emoji, no 'comment below'. "
                      + (f"Only mention {TOPIC.get('name', 'GTA 6')} if in_game is true. ") +
                      "Answer JSON {\"ok\": true|false, \"why\": \"...\", \"hook\": \"...\", \"react\": true|false, "
                      "\"watching\": \"...\", \"owner_cam\": [ymin, xmin, ymax, xmax] or null, "
                      "\"watched_box\": [ymin, xmin, ymax, xmax] or null, \"in_game\": true|false, \"question\": \"...\"}."}]
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
                LAST_VISUAL.clear()   # react details for the caller (react layout - Nov 24)
                LAST_VISUAL.update(react=bool(row.get("react")), watching=clean_line(str(row.get("watching") or ""))[:40],
                                   owner_cam=row.get("owner_cam"), watched_box=row.get("watched_box"),
                                   in_game=row.get("in_game"), question=clean_line(str(row.get("question") or ""))[:110])
                return (bool(row.get("ok", True)), clean_line(str(row.get("why", "")))[:90],
                        short_hook(str(row.get("hook", ""))))
            except Exception as e:  # noqa: BLE001
                _tired(model, e)
                busy = busy or "429" in str(e) or "503" in str(e)
                log(f"picture check ({model}): {str(e)[:80]}")
        if not busy:
            break
        _t.sleep(20 * (attempt + 1))
    return None, "check failed", ""


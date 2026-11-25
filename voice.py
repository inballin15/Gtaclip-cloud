"""New voice on a clip: take out the voice that's talking, keep the game sounds, and read the same lines (or your
own script) in an AI voice you picked. Free and runs on GitHub: Demucs (voice removal) + Kokoro (AI voice).

The site sends a "voice: <clip id>" tap with JSON like {"voice": "am_michael", "script": ""}.
Empty script = the same words they said, each line placed where they said it (so it stays in sync).
The result is saved like a quick text edit (edits branch + instant link), so the site and the poster use it.
`python voice.py --samples` makes the short sample of every voice the site plays.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

VOICES = {   # id: (name on the site, language code) - the site's list matches (Nov 25: more to pick from)
    "am_michael": ("Michael · US male", "a"), "am_puck": ("Puck · hype US male", "a"),
    "am_fenrir": ("Fenrir · deep US male", "a"), "am_echo": ("Echo · calm US male", "a"),
    "am_eric": ("Eric · US male", "a"), "am_liam": ("Liam · young US male", "a"),
    "am_onyx": ("Onyx · low US male", "a"), "am_adam": ("Adam · US male", "a"),
    "bm_george": ("George · British male", "b"), "bm_lewis": ("Lewis · British male", "b"),
    "bm_daniel": ("Daniel · British male", "b"), "bm_fable": ("Fable · British male", "b"),
    "af_heart": ("Heart · US female", "a"), "af_bella": ("Bella · US female", "a"),
    "af_nicole": ("Nicole · soft US female", "a"), "af_sarah": ("Sarah · US female", "a"),
    "af_nova": ("Nova · US female", "a"), "af_sky": ("Sky · US female", "a"),
    "bf_emma": ("Emma · British female", "b"), "bf_isabella": ("Isabella · British female", "b"),
}
SR = 24000
WORK = Path("/tmp/vo")
WORK.mkdir(exist_ok=True)
_PIPES: dict = {}


def tts(text: str, voice: str, speed: float = 1.0) -> np.ndarray:
    from kokoro import KPipeline
    lang = VOICES.get(voice, ("", "a"))[1]
    if lang not in _PIPES:
        _PIPES[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
    parts = [np.asarray(a, dtype=np.float32) for _, _, a in _PIPES[lang](text, voice=voice, speed=speed) if a is not None]
    return np.concatenate(parts) if parts else np.zeros(1, np.float32)


def tts_timed(text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, list]:
    """Like tts(), plus when each word is spoken: [(text, start_s, end_s, space_after)] - for exact subtitles."""
    from kokoro import KPipeline
    lang = VOICES.get(voice, ("", "a"))[1]
    if lang not in _PIPES:
        _PIPES[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
    parts, words, off = [], [], 0.0
    for r in _PIPES[lang](text, voice=voice, speed=speed):
        a = getattr(r, "audio", None)
        if a is None:
            try:
                a = r[2]
            except Exception:  # noqa: BLE001
                a = None
        if a is None:
            continue
        a = np.asarray(a, dtype=np.float32).reshape(-1)
        for t in (getattr(r, "tokens", None) or []):
            st, en = getattr(t, "start_ts", None), getattr(t, "end_ts", None)
            tx = str(getattr(t, "text", "") or "")
            if st is not None and en is not None and tx.strip():
                words.append([tx, off + float(st), off + float(en), str(getattr(t, "whitespace", " ") or "")])
        parts.append(a)
        off += len(a) / SR
    return (np.concatenate(parts) if parts else np.zeros(1, np.float32)), words


def wav_write(path: Path, x: np.ndarray, sr: int = SR) -> None:
    import soundfile as sf
    sf.write(str(path), x, sr)


def samples() -> None:
    """One short sample per voice, saved on the 'voices' release (the site plays them)."""
    import quickedit as qe
    try:
        rel = qe.gh("GET", "/releases/tags/voices")
    except Exception:  # noqa: BLE001
        rel = qe.gh("POST", "/releases", {"tag_name": "voices", "name": "Voice samples", "target_commitish": "main",
                                          "body": "Short samples of the AI voices you can pick on the site.", "prerelease": True})
    have = {a["name"]: a["id"] for a in qe.gh("GET", f"/releases/{rel['id']}/assets?per_page=100")}
    import urllib.request

    def up(path: Path, ctype: str) -> None:
        if path.name in have:
            qe.gh("DELETE", f"/releases/assets/{have[path.name]}")
        req = urllib.request.Request(f"https://uploads.github.com/repos/{qe.REPO}/releases/{rel['id']}/assets?name={path.name}",
                                     data=path.read_bytes(), method="POST",
                                     headers={"Authorization": f"Bearer {qe.TOKEN}", "Content-Type": ctype,
                                              "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=60).read()

    for v in VOICES:
        # the sample reads like a long tips video (that's where you pick a narrator) - "GTA six" so it's said right
        x = tts("Here are nine GTA six tips you need to know. Always put your cash in the bank, "
                "so you don't lose it when you get busted.", v)
        wav = WORK / f"{v}.wav"
        mp3 = WORK / f"{v}.mp3"
        wav_write(wav, x)
        subprocess.run([qe.FF, "-y", "-loglevel", "error", "-i", str(wav), "-b:a", "96k", str(mp3)], check=True)
        up(mp3, "audio/mpeg")
        # the spoken follow line for clips (engage.py mixes it in right after the pop): trimmed, loudness-matched
        raw = WORK / f"follow_{v}_raw.wav"
        wav_write(raw, tts("Don't forget to follow and subscribe for daily clips!", v, 1.05))
        fol = WORK / f"follow_{v}.wav"
        subprocess.run([qe.FF, "-y", "-loglevel", "error", "-i", str(raw), "-af",
                        "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                        "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                        "loudnorm=I=-15:TP=-1.5:LRA=7,aresample=48000", "-ac", "1", "-c:a", "pcm_s16le", str(fol)], check=True)
        up(fol, "audio/wav")
        print("sample + follow line:", v)


def main() -> None:
    import urllib.request
    import quickedit as qe
    from render_hook import render as render_hook
    iss = qe.ISSUE
    m = re.match(r"^\s*voice:\s*(\S+)", iss["title"])
    key = m.group(1) if m else ""
    raw = iss.get("body") or "{}"
    req = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    voice = req.get("voice") if req.get("voice") in VOICES else "am_michael"
    script = (req.get("script") or "").strip()[:3000]
    clips = json.loads(Path("/tmp/state.json").read_text()).get("clips", {})
    cid = next((k for k in clips if k == key or k.startswith(key) or key.startswith(k)), None)
    if not cid:
        qe.say("Couldn't find that clip (it may have expired).")
        return
    c = clips[cid]
    print("STEP 1/5 getting the clip", cid, voice)
    clean = WORK / "clean.mp4"
    qe.site_get(f"{qe.clip_dir(cid)}{cid}_clean.mp4?t={int(time.time())}", clean)
    FF = qe.FF
    mm = re.search(r"Duration: (\d+):(\d+):([\d.]+)", subprocess.run([FF, "-i", str(clean)], capture_output=True, text=True).stderr)
    dur = int(mm.group(1)) * 3600 + int(mm.group(2)) * 60 + float(mm.group(3))
    wav = WORK / "in.wav"
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(clean), "-vn", "-ac", "2", "-ar", "44100", str(wav)], check=True)

    print("STEP 2/5 taking their voice out")
    subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", str(WORK / "sep"), str(wav)], check=True)
    game = WORK / "sep" / "htdemucs" / "in" / "no_vocals.wav"
    vocals = WORK / "sep" / "htdemucs" / "in" / "vocals.wav"

    print("STEP 3/5 reading what they said")
    from faster_whisper import WhisperModel
    segs = [s for s in WhisperModel("base.en", device="cpu", compute_type="int8").transcribe(
        str(vocals), vad_filter=True, language="en")[0] if s.text.strip()]
    lines = [(float(s.start), float(s.end), s.text.strip()) for s in segs]
    if script:   # your own words: spread over the time they were talking (or the whole clip)
        t0 = lines[0][0] if lines else 0.3
        lines = [(t0, dur - 0.2, script)]
    if not lines:
        qe.say("Couldn't hear anyone talking in this clip, so there's nothing to re-voice.")
        return

    print("STEP 4/5 recording the new voice")
    track = np.zeros(int((dur + 1) * SR), np.float32)
    for i, (a, b, text) in enumerate(lines):
        slot = ((lines[i + 1][0] if i + 1 < len(lines) else dur) - a) - 0.05
        x = tts(text, voice)
        need = len(x) / SR
        if need > slot > 0.3:   # say it a bit faster so it fits before the next line
            x = tts(text, voice, speed=min(1.35, need / slot))
        x = x[: int(max(slot, 0.3) * SR)] if len(x) / SR > slot + 0.4 else x
        s0 = int(a * SR)
        seg = x[: max(0, len(track) - s0)]
        track[s0:s0 + len(seg)] += seg
    vwav = WORK / "voice.wav"
    wav_write(vwav, np.clip(track, -1, 1))

    print("STEP 5/5 putting it together")
    newclean = WORK / "clean_voice.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(clean), "-i", str(game), "-i", str(vwav), "-filter_complex",
                    "[1:a]volume=0.85[g];[2:a]aresample=44100,volume=1.6[v];[g][v]amix=inputs=2:duration=first:normalize=0,"
                    "loudnorm=I=-14:TP=-1.5:LRA=11[a]", "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-ar", "48000", "-movflags", "+faststart", str(newclean)], check=True)
    edits = json.loads(Path("/tmp/edits/edits.json").read_text()) if Path("/tmp/edits/edits.json").exists() else {}
    prev = edits.get(cid, {})
    cur = prev if prev.get("at", 0) > (c.get("rendered") or 0) else {}
    ul, sty = c.get("user_layout") or {}, c.get("style") or {}
    hook = "" if c.get("no_hook") and not cur.get("hook") else (cur.get("hook") or c.get("hook") or "")
    stamp = int(time.time())
    out_name = f"v/e/{cid}_{stamp}.mp4"
    out = Path("/tmp/edits") / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    if hook:
        style = cur.get("hook_style") or ul.get("hook_style") or sty.get("box") or "white"
        scale = float(cur.get("hook_scale", ul.get("hook_scale", 1.0)))
        hy = cur.get("hook_y", ul.get("hook_y"))
        lay = "cam" if c.get("cam_only") else (c.get("layout_used") or c.get("layout") or "split")
        T = int(ul.get("top_h") or qe.CAM_PX.get(sty.get("cam", ""), qe.TOP_H))
        oy = (f"H*{hy:.3f}-h/2" if hy is not None else "(H-h)/2" if lay == "full" else "'max(60,340-h-26)'" if lay == "react" else "H*0.2-h/2" if lay == "fill"
              else "H*0.6" if lay.startswith("irl") else "H*0.12" if lay == "cam" else f"{T}-h*0.2")
        png = render_hook(hook, str(WORK / "hook.png"), style, scale)
        import engage   # hook + the follow pop-up (+ progress bar if this clip tests it), like every posted video
        engage.hooked(FF, newclean, png, out, lay, hy, T, WORK, pbar=sty.get("pbar") == "on")
    else:
        import shutil
        shutil.copy(newclean, out)
    rec = {**{k: cur[k] for k in ("hook", "caption", "hook_style", "hook_y", "hook_scale") if k in cur},
           "at": stamp, "video": out_name, "voice": voice}
    try:
        rec["url"] = qe.upload_asset(out)
    except Exception as e:  # noqa: BLE001
        print("instant link failed:", e)
    edits[cid] = rec
    Path("/tmp/edits/edits.json").write_text(json.dumps(edits, indent=1, ensure_ascii=False))
    Path("/tmp/qe_done").write_text(cid)
    Path("/tmp/qe_entry.json").write_text(json.dumps({"id": cid, "rec": rec}))
    import shutil
    shutil.copy(out, "/tmp/qe_video.mp4")
    for path, f in ((out_name, out), (f"{qe.clip_dir(cid)}{cid}_clean.mp4", newclean)):   # the no-caption copy gets the new voice too
        if f.stat().st_size > 45e6:   # too big for the quick way: the clip maker copies it onto the site on its next run
            print("big file - the clip maker puts it on the site:", path)
            continue
        try:
            qe.put_on_site(path, f)
        except Exception as e:  # noqa: BLE001
            print("site copy failed:", path, e)


if __name__ == "__main__":
    if "--samples" in sys.argv:
        samples()
        sys.exit(0)
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        try:
            import quickedit as qe
            qe.say(f"New voice failed ({str(e)[:150]}). Try again in a few minutes.")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)

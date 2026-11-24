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

VOICES = {   # id: (name on the site, language code)
    "am_michael": ("Michael · US male", "a"), "am_fenrir": ("Fenrir · deep US male", "a"),
    "am_puck": ("Puck · hype US male", "a"), "am_echo": ("Echo · calm US male", "a"),
    "af_heart": ("Heart · US female", "a"), "af_bella": ("Bella · US female", "a"),
    "bm_george": ("George · British male", "b"), "bf_emma": ("Emma · British female", "b"),
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
    for v in VOICES:
        x = tts("GTA six is actually crazy. Watch what happens when he tries this.", v)
        wav = WORK / f"{v}.wav"
        mp3 = WORK / f"{v}.mp3"
        wav_write(wav, x)
        subprocess.run([qe.FF, "-y", "-loglevel", "error", "-i", str(wav), "-b:a", "96k", str(mp3)], check=True)
        if mp3.name in have:
            qe.gh("DELETE", f"/releases/assets/{have[mp3.name]}")
        import urllib.request
        req = urllib.request.Request(f"https://uploads.github.com/repos/{qe.REPO}/releases/{rel['id']}/assets?name={mp3.name}",
                                     data=mp3.read_bytes(), method="POST",
                                     headers={"Authorization": f"Bearer {qe.TOKEN}", "Content-Type": "audio/mpeg",
                                              "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=60).read()
        print("sample:", v)


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
    urllib.request.urlretrieve(qe.PAGES + f"{qe.clip_dir(cid)}{cid}_clean.mp4?t={int(time.time())}", clean)
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
        oy = (f"H*{hy:.3f}-h/2" if hy is not None else "(H-h)/2" if lay == "full" else "H*0.2-h/2" if lay == "fill"
              else "H*0.6" if lay.startswith("irl") else "H*0.12" if lay == "cam" else f"{T}-h*0.2")
        png = render_hook(hook, str(WORK / "hook.png"), style, scale)
        subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(newclean), "-i", png, "-filter_complex",
                        f"[0:v][1:v]overlay=(W-w)/2:{oy},format=yuv420p[out]", "-map", "[out]", "-map", "0:a",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-maxrate", "12000k", "-bufsize", "24M",
                        "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
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

"""Voice packs: everything your phone needs to swap a clip's voice in under a second, made ahead of time.

For every clip in the queue (best first) it saves on the site (site-big release, served at v/vp/...):
  <id>_<clean_v>_game.m4a       the clip's sound with the talking taken out (Demucs) - game sounds stay
  <id>_<clean_v>_lines.json     what was said and when (Whisper)
  <id>_<clean_v>_<voice>.m4a    those lines read by each of the 20 AI voices, at the same moments (Kokoro)
<clean_v> changes when the clip is re-made, so an old pack is never used on a new video.
The phone then mixes game + the voice you tapped (+ pop / follow line) and copies the picture as is: no re-make.
Runs on GitHub every 20 min (voicepack.yml); a clip takes about a minute; packs of clips that left the queue go.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

import quickedit as qe
from voice import SR, VOICES, WORK, tts, wav_write

PER_RUN = int(os.environ.get("VP_PER_RUN", "14"))
BUDGET_S = int(os.environ.get("VP_BUDGET_S", "1500"))
SITE = Path(os.environ.get("VP_SITE", "/tmp/site"))   # the gh-pages checkout (queue.json, mine.json)
FF = qe.FF


def lines_of(vocals: Path, dur: float) -> list:
    from faster_whisper import WhisperModel
    global _WM
    if "_WM" not in globals():
        _WM = WhisperModel("base.en", device="cpu", compute_type="int8")
    segs = [s for s in _WM.transcribe(str(vocals), vad_filter=True, language="en")[0] if s.text.strip()]
    return [[round(float(s.start), 2), round(float(s.end), 2), s.text.strip()] for s in segs if s.start < dur]


def voice_track(lines: list, voice: str, dur: float) -> np.ndarray:
    """The same placing as voice.py: each line where it was said, a bit faster if it runs into the next one."""
    track = np.zeros(int((dur + 1) * SR), np.float32)
    for i, (a, b, text) in enumerate(lines):
        slot = ((lines[i + 1][0] if i + 1 < len(lines) else dur) - a) - 0.05
        x = tts(text, voice)
        need = len(x) / SR
        if need > slot > 0.3:
            x = tts(text, voice, speed=min(1.35, need / slot))
        x = x[: int(max(slot, 0.3) * SR)] if len(x) / SR > slot + 0.4 else x
        s0 = int(a * SR)
        seg = x[: max(0, len(track) - s0)]
        track[s0:s0 + len(seg)] += seg
    return np.clip(track[: int(dur * SR) + SR // 4], -1, 1)


def main() -> None:
    t_start = time.time()
    rel = qe.gh("GET", "/releases/tags/site-big")
    have = {}
    for page in range(1, 60):
        got = qe.gh("GET", f"/releases/{rel['id']}/assets?per_page=100&page={page}") or []
        have.update({a["name"]: a["id"] for a in got})
        if len(got) < 100:
            break

    def up(path: Path, name: str, ctype: str) -> None:
        if name in have:
            qe.gh("DELETE", f"/releases/assets/{have[name]}")
        req = urllib.request.Request(f"https://uploads.github.com/repos/{qe.REPO}/releases/{rel['id']}/assets?name={name}",
                                     data=path.read_bytes(), method="POST",
                                     headers={"Authorization": f"Bearer {qe.TOKEN}", "Content-Type": ctype,
                                              "Accept": "application/vnd.github+json"})
        for i in range(3):
            try:
                urllib.request.urlopen(req, timeout=90).read()
                have[name] = -1
                return
            except Exception as e:  # noqa: BLE001
                if i == 2:
                    raise
                print("upload retry", name, e)
                time.sleep(3)

    ready = []
    for f, mine in (("queue.json", False), ("mine.json", True)):
        try:
            ready += [(r, mine) for r in json.loads((SITE / f).read_text()).get("ready", [])]
        except Exception as e:  # noqa: BLE001
            print("no", f, e)
    want_prefixes = set()
    todo = []
    for r, mine in ready:
        cv = r.get("clean_v")
        if not cv or not r.get("clean"):
            continue
        pre = f"v__vp__{r['id']}_{cv}_"
        want_prefixes.add(pre)
        if pre + "lines.json" in have:
            continue
        todo.append((r, mine))
    # clean-up: packs of clips that left the queue / were re-made
    for name, aid in list(have.items()):
        if name.startswith("v__vp__") and not any(name.startswith(p) for p in want_prefixes) and aid != -1:
            try:
                qe.gh("DELETE", f"/releases/assets/{aid}")
                have.pop(name, None)
            except Exception as e:  # noqa: BLE001
                print("couldn't remove", name, e)
    print(f"{len(todo)} clips need a voice pack ({len(ready)} in the queue)")
    done = 0
    for r, mine in todo[:PER_RUN]:
        if time.time() - t_start > BUDGET_S:
            print("time's up for this run - the rest next run")
            break
        cid, cv = r["id"], r["clean_v"]
        t0 = time.time()
        d = WORK / "vp"
        subprocess.run(["rm", "-rf", str(d)])
        d.mkdir(parents=True)
        try:
            clean = d / "clean.mp4"
            qe.site_get(r["clean"].split("?")[0] + f"?v={cv}", clean)
            mm = subprocess.run([FF, "-i", str(clean)], capture_output=True, text=True).stderr
            import re
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", mm)
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            wav = d / "in.wav"
            subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(clean), "-vn", "-ac", "2", "-ar", "44100", str(wav)], check=True)
            subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", str(d / "sep"), str(wav)],
                           check=True, capture_output=True)
            game = d / "sep" / "htdemucs" / "in" / "no_vocals.wav"
            vocals = d / "sep" / "htdemucs" / "in" / "vocals.wav"
            lines = lines_of(vocals, dur)
            pre = f"v__vp__{cid}_{cv}_"
            gm = d / "game.m4a"
            subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(game), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(gm)], check=True)
            for v in VOICES if lines else []:
                x = voice_track(lines, v, dur)
                vw = d / f"{v}.wav"
                wav_write(vw, x)
                vm = d / f"{v}.m4a"
                subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(vw), "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-ar", "48000", str(vm)],
                               check=True)
                up(vm, pre + f"{v}.m4a", "audio/mp4")
            up(gm, pre + "game.m4a", "audio/mp4")
            lj = d / "lines.json"   # written LAST: its presence means the pack is complete
            lj.write_text(json.dumps({"id": cid, "clean_v": cv, "dur": round(dur, 2), "lines": lines,
                                      "voices": list(VOICES) if lines else []}))
            up(lj, pre + "lines.json", "application/json")
            done += 1
            print(f"voice pack {cid}: {len(lines)} lines, {time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            print(f"voice pack failed {cid}: {str(e)[:200]}")
    print(f"made {done} voice packs in {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()

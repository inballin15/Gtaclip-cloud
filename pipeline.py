#!/usr/bin/env python3
"""
gtaclip pipeline — everything runs free on GitHub Actions (no Cloudinary, no OpusClip).

Each run:
  1. loads state (state branch: state.json) and what the posting runs reported (posted branch)
  2. applies dashboard actions (GitHub issue "skip: <id>" / "edit: <id>")
  3. on scheduled/manual runs: finds new GTA 6 Twitch clips, downloads them, transcribes them,
     finds the streamer's webcam (face that stays put), and skips clips without one
  4. renders for each queued clip:
       v/<id>.mp4        phone-friendly original (for watching on the site)
       v/<id>.jpg        poster frame
       v/<id>_final.mp4  the exact post: webcam top half, gameplay bottom half,
                         TikTok-style caption box (white rounded line boxes, color emoji)
  5. writes site/queue.json and publishes the site + videos to GitHub Pages
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import imageio_ffmpeg

from fetch_clips import CFG, download, log, pick_clips, transcript, twitch_headers
from render_hook import render as render_hook
from render_hook import strip_emoji

ROOT = Path(__file__).resolve().parent
FF = imageio_ffmpeg.get_ffmpeg_exe()
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT / "_state"))
POSTED_DIR = Path(os.environ.get("POSTED_DIR", ROOT / "_posted"))
SITE = Path(os.environ.get("SITE_OUT", ROOT / "_site"))
VID = SITE / "v"
RAW = Path(os.environ.get("RAW_DIR", ROOT / "_raw"))  # cached raw downloads (actions cache)
PAGES = "https://inballin15.github.io/Gtaclip-cloud"
KEEP_NEW_HOURS = 30
CAPTION_CUT = 0.22   # cut the bottom 22% of the gameplay (streamers' burned-in auto captions live there)
LAYOUT_V = "v3-textonly-capcut"
KEEP_POSTED_HOURS = 72
NOW = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- state
def load_state() -> dict:
    p = STATE_DIR / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"clips": {}}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(st, indent=1, ensure_ascii=False))


def apply_posted(st: dict) -> None:
    """Posting runs push posted/<id>.json and hooks/<id>.json to the 'posted' branch."""
    for f in sorted((POSTED_DIR / "posted").glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        c = st["clips"].get(f.stem)
        if c and c["status"] != "used":
            c.update(status="used", tiktok=rec.get("tiktok", ""), youtube=rec.get("youtube", ""),
                     posted_at=rec.get("posted_at") or iso(NOW))
            log(f"posted: {f.stem}")
    for f in sorted((POSTED_DIR / "skips").glob("*.json")):
        c = st["clips"].get(f.stem)
        if c and c["status"] == "new":
            c.update(status="skip", skip_reason="flagged by posting run")
    for f in sorted((POSTED_DIR / "hooks").glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        c = st["clips"].get(f.stem)
        if c and not c.get("hook_by_user") and rec.get("hook") and not c.get("hook"):
            c["hook"] = strip_emoji(rec["hook"])[:140]


def apply_issue(st: dict) -> str:
    """Dashboard 'Don't post' / 'Edit' requests (GitHub issues by the repo owner)."""
    title = os.environ.get("ISSUE_TITLE", "")
    body = os.environ.get("ISSUE_BODY", "").replace("\r\n", "\n")
    m = re.match(r"^\s*(skip|edit):\s*(?:gtaclip/)?(?:tw_[a-z0-9_]+?_)?([A-Za-z0-9_\-]+)\s*$", title)
    if not m:
        return ""
    action, key = m.groups()
    cid = next((k for k in st["clips"] if k == key or k.startswith(key) or key.startswith(k)), None)
    if not cid:
        return f"Couldn't find clip {key}."
    c = st["clips"][cid]
    if action == "skip":
        c["status"] = "skip"
        c["skip_reason"] = "removed by Nicholas"
        return "Done — this clip won't be posted."
    hm = re.search(r"^hook:[ \t]*(.*)$", body, re.M)
    cm = re.search(r"^caption:[ \t]*(.*)\Z", body, re.M | re.S)
    if hm and hm.group(1).strip():
        c["hook"] = strip_emoji(hm.group(1))[:140]
        c["hook_by_user"] = True
    if cm:
        c["caption"] = cm.group(1).strip()[:2000]
    return "Done — the next post will use your hook and caption."


# ---------------------------------------------------------------- webcam
_CASCS = [cv2.CascadeClassifier(cv2.data.haarcascades + n) for n in
          ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml", "haarcascade_profileface.xml")]


def _faces(frame, neighbors=5):
    g = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    out = []
    for c in _CASCS:
        for f in c.detectMultiScale(g, 1.08, neighbors, minSize=(30, 30)):
            out.append(tuple(int(v) for v in f))
        flipped = cv2.flip(g, 1) if c is _CASCS[2] else None  # profiles facing the other way
        if flipped is not None:
            W = g.shape[1]
            for (x, y, w, h) in c.detectMultiScale(flipped, 1.08, neighbors, minSize=(30, 30)):
                out.append((W - x - w, y, w, h))
    return out


def box_has_face(path: Path, duration: float, cam: str) -> bool:
    x, y, w, h = [float(v) for v in cam.split(",")]
    for fr in _frames(path, [max(0.5, duration * p) for p in (0.2, 0.5, 0.8)]):
        H, W = fr.shape[:2]
        crop = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
        if crop.size and _faces(cv2.resize(crop, None, fx=2, fy=2), neighbors=4):
            return True
    return False


def _frames(path: Path, secs):
    cap = cv2.VideoCapture(str(path))
    out = []
    for s in secs:
        cap.set(cv2.CAP_PROP_POS_MSEC, s * 1000)
        ok, fr = cap.read()
        if ok:
            out.append(fr)
    cap.release()
    return out


def find_cam(path: Path, duration: float) -> str:
    """A face that stays in the same place across frames = the webcam. Returns x,y,w,h fractions
    of a 1080:960 box around it (shoulders included), or '' if none."""
    secs = [max(0.5, duration * p) for p in (0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92)]
    frames = _frames(path, secs)
    if not frames:
        return ""
    H, W = frames[0].shape[:2]
    per = [_faces(f) for f in frames]
    best, best_hits = None, 0
    for i, faces in enumerate(per):
        for (x, y, w, h) in faces:
            cx, cy = x + w / 2, y + h / 2
            hits = sum(1 for j, o in enumerate(per) if j != i and any(
                abs(ox + ow / 2 - cx) < 0.5 * w and abs(oy + oh / 2 - cy) < 0.5 * h and 0.6 < ow / w < 1.6
                for (ox, oy, ow, oh) in o))
            if hits >= 2 and (hits > best_hits or (hits == best_hits and w * h > best[2] * best[3])):
                best, best_hits = (x, y, w, h), hits
    if not best:
        return ""
    x, y, w, h = best
    aspect = 1080 / 960
    bw = min(W, max(w * 3.6, 300.0))
    bh = bw / aspect
    if bh > H:
        bh, bw = float(H), H * aspect
    cx, cy = x + w / 2, y + h * 0.75
    bx = min(max(0.0, cx - bw / 2), W - bw)
    by = min(max(0.0, cy - bh / 2), H - bh)
    return f"{bx / W:.4f},{by / H:.4f},{bw / W:.4f},{bh / H:.4f}"


def duration_of(path: Path) -> float:
    cap = cv2.VideoCapture(str(path))
    n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS) or 30
    cap.release()
    return n / fps if n else 30.0


# ---------------------------------------------------------------- rendering
def ff(*args) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True)


def render_preview(raw: Path, cid: str) -> None:
    ff("-i", str(raw), "-vf", "scale=-2:540", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
       "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(VID / f"{cid}.mp4"))
    ff("-ss", "2", "-i", str(raw), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(VID / f"{cid}.jpg"))


def render_final(raw: Path, cid: str, cam: str, hook: str, tmp: Path) -> None:
    x, y, w, h = [float(v) for v in cam.split(",")]
    png = render_hook(hook, str(tmp / f"{cid}_hook.png"))
    flt = (
        f"[0:v]split=2[a][b];"
        f"[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale=1080:960:force_original_aspect_ratio=increase,"
        f"crop=1080:960,setsar=1[top];"
        f"[b]crop=iw:ih*{1 - CAPTION_CUT}:0:0,scale=-2:960,crop=1080:960,setsar=1[bot];"
        f"[top][bot]vstack=inputs=2[st];"
        f"[st][1:v]overlay=(W-w)/2:(H-h)/2,format=yuv420p[out]"
    )
    ff("-i", str(raw), "-i", png, "-filter_complex", flt, "-map", "[out]", "-map", "0:a?",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
       "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(VID / f"{cid}_final.mp4"))


# ---------------------------------------------------------------- main
def main() -> None:
    st = load_state()
    clips = st["clips"]
    apply_posted(st)
    issue_msg = apply_issue(st)
    if issue_msg:
        Path("/tmp/issue_reply.txt").write_text(issue_msg)
        log(issue_msg)

    SITE.mkdir(parents=True, exist_ok=True)
    VID.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())

    for c in clips.values():
        if c.get("hook") and strip_emoji(c["hook"]) != c["hook"]:
            c["hook"] = strip_emoji(c["hook"])

    # retry clips that failed the webcam check with the older detector
    for cid, c in clips.items():
        if c.get("status") == "skip" and c.get("skip_reason") == "no webcam found" and c.get("cam_v", 1) < 2:
            raw = RAW / f"{cid}.mp4"
            c["cam_v"] = 2
            if not raw.exists():
                try:
                    shutil.move(str(download(c["twitch"], Path(tempfile.mkdtemp()))), raw)
                except Exception as e:  # noqa: BLE001
                    log(f"retry download failed {cid}: {e}")
                    continue
            dur = duration_of(raw)
            cam = find_cam(raw, dur)
            usual = ",".join(str(v) for v in (CFG["streamers"].get(c["login"], {}).get("facecam") or []))
            if not cam and usual and box_has_face(raw, dur, usual):
                cam = usual
            if cam:
                c.update(status="new", cam=cam)
                c.pop("skip_reason", None)
                log(f"webcam found on retry: {c['streamer']} '{c['title']}' {cam}")

    # 3. new clips (not on issue-triggered runs, to keep those fast)
    if os.environ.get("FETCH", "1") == "1":
        try:
            picks = pick_clips(twitch_headers(), set(clips))
        except Exception as e:  # noqa: BLE001
            log(f"twitch failed: {e}")
            picks = []
        for c in picks:
            cid = c["id"]
            try:
                f = download(c["url"], tmp)
                raw = RAW / f"{cid}.mp4"
                shutil.move(str(f), raw)
                dur = duration_of(raw)
                text = transcript(raw)
                s = CFG["streamers"][c["login"]]
                rec = {
                    "status": "new", "streamer": c["name"], "login": c["login"], "tag": s.get("tag", ""),
                    "campaign": s.get("campaign", "") or "", "title": c["title"], "views": c["view_count"],
                    "created": c["created_at"], "found": iso(NOW), "twitch": c["url"], "transcript": text,
                    "seconds": round(dur),
                }
                bad = [w.lower() for w in CFG["discovery"]["skip_title_words"]]
                if any(w in text.lower() for w in ("penis", "dick", "pussy", "nigg", "fag", "retard", " cock")):
                    rec.update(status="skip", skip_reason="sexual remark or slur in audio")
                elif any(w in c["title"].lower() for w in bad):
                    rec.update(status="skip", skip_reason="title filter")
                else:
                    cam = find_cam(raw, dur)
                    usual = ",".join(str(v) for v in (s.get("facecam") or []))
                    if not cam and usual and box_has_face(raw, dur, usual):
                        cam = usual
                    if not cam:
                        rec.update(status="skip", skip_reason="no webcam found")
                    else:
                        rec["cam"] = cam
                clips[cid] = rec
                log(f"{c['name']} '{c['title']}' -> {rec['status']} {rec.get('cam', rec.get('skip_reason', ''))}")
            except Exception as e:  # noqa: BLE001
                log(f"failed {c['url']}: {e}")

    # 4. drop old stuff, render what's queued
    for cid, c in list(clips.items()):
        age_ref = c.get("posted_at") or c.get("found") or iso(NOW)
        age_h = (NOW - parse(age_ref)).total_seconds() / 3600
        if (c["status"] == "new" and age_h > KEEP_NEW_HOURS) or (c["status"] != "new" and age_h > KEEP_POSTED_HOURS):
            c["status"] = "expired" if c["status"] == "new" else c["status"]
        if c["status"] != "new":
            for suf in (".mp4", "_final.mp4", ".jpg"):
                (VID / f"{cid}{suf}").unlink(missing_ok=True)
            (RAW / f"{cid}.mp4").unlink(missing_ok=True)
            continue
        raw = RAW / f"{cid}.mp4"
        if not raw.exists():
            log(f"raw missing for {cid}; re-downloading")
            try:
                f = download(c["twitch"], tmp)
                shutil.move(str(f), raw)
            except Exception as e:  # noqa: BLE001
                log(f"re-download failed {cid}: {e}")
                c["status"] = "expired"
                continue
        try:
            if not (VID / f"{cid}.mp4").exists():
                render_preview(raw, cid)
            if c.get("hook"):
                hh = hashlib.sha1((c["hook"] + c["cam"] + LAYOUT_V).encode()).hexdigest()[:10]
                if c.get("final_hash") != hh or not (VID / f"{cid}_final.mp4").exists():
                    render_final(raw, cid, c["cam"], c["hook"], tmp)
                    c["final_hash"] = hh
                    c["final_v"] = hh
                    log(f"rendered final {cid}")
        except Exception as e:  # noqa: BLE001
            log(f"render failed {cid}: {e}")

    # 5. queue.json
    ready = [dict(id=cid, **{k: v for k, v in c.items() if k not in ("final_hash",)})
             for cid, c in clips.items() if c["status"] == "new"]
    for r in ready:
        r["video"] = f"v/{r['id']}.mp4"
        r["poster"] = f"v/{r['id']}.jpg"
        r["final"] = f"v/{r['id']}_final.mp4?v={r['final_v']}" if r.get("final_v") and r.get("hook") else ""
        r["final_abs"] = f"{PAGES}/v/{r['id']}_final.mp4?v={r['final_v']}" if r["final"] else ""
        r["clipped"] = r.get("created")
    ready.sort(key=lambda r: r.get("created") or "", reverse=True)
    ready.sort(key=lambda r: not r.get("campaign"))
    posted = []
    try:
        posted = json.loads((ROOT / "posts_history.json").read_text())
    except Exception:  # noqa: BLE001
        pass
    for cid, c in clips.items():
        if c["status"] == "used" and (c.get("tiktok") or c.get("youtube")):
            posted.append({"hook": c.get("hook", ""), "streamer": c["streamer"], "campaign": c.get("campaign", ""),
                           "posted": c.get("posted_at"), "tiktok": c.get("tiktok", ""), "youtube": c.get("youtube", "")})
    posted.sort(key=lambda p: p.get("posted") or "", reverse=True)
    status = {}
    try:
        log_text = Path("/tmp/pipeline.log").read_text() if Path("/tmp/pipeline.log").exists() else ""
        for m in re.finditer(r"\] (.+?): (\d+) fresh GTA 6 clips", log_text):
            status[m.group(1)] = int(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    (SITE / "queue.json").write_text(json.dumps({
        "updated": iso(NOW), "ready": ready, "posted": posted[:40],
        "fetcher": {"last_run_utc": NOW.strftime("%H:%M:%S"), "fresh_by_streamer": status},
    }, indent=1, ensure_ascii=False))
    save_state(st)
    log(f"queue: {len(ready)} ready ({sum(1 for r in ready if r['final'])} with final video)")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)

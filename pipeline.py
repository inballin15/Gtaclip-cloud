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
import numpy as np
import imageio_ffmpeg

from fetch_clips import CFG, LIVE, download, log, pick_clips, transcript, twitch_headers
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
KEEP_UPLOAD_HOURS = 48   # clips made from your own uploads stay 2 days
KEEP_NEW_HOURS = 9       # a clip is only posted while it's fresh: under 9 hours since it was clipped on Twitch
REPLACE_QUEUE_EACH_RUN = False   # off: clips stay until they're 9 h old (max 30 in the queue)
MAX_RENDERS = 12         # videos made per run (2 at a time); the rest next run
QUEUE_MAX = 30           # finished videos kept ready (11 posts a day)
CAPTION_CUT = 0.0    # no cutting: full gameplay (user choice, Nov 19)
LAYOUT_V = "v3-textonly-capcut"
TOP_H = 672   # webcam strip height (1/3-ish of 1920), gameplay gets the rest
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
        if c:
            if c["status"] != "used":
                log(f"posted: {f.stem}")
            c.update(status="used", tiktok=rec.get("tiktok", "") or c.get("tiktok", ""),
                     youtube=rec.get("youtube", "") or c.get("youtube", ""),
                     posted_at=c.get("posted_at") or rec.get("posted_at") or iso(NOW))
    # posts that failed on every platform come back to the queue
    try:
        returned = json.loads((POSTED_DIR / "control.json").read_text()).get("returned", [])
    except Exception:  # noqa: BLE001
        returned = []
    for cid in returned:
        c = st["clips"].get(cid)
        if c and c["status"] == "used" and not (POSTED_DIR / "posted" / f"{cid}.json").exists():
            c["status"] = "new"
            for k in ("posted_at", "tiktok", "youtube"):
                c.pop(k, None)
            log(f"back in queue (upload failed): {cid}")
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
        if c and not c.get("hook_by_user") and rec.get("hook") and (not c.get("hook") or c.get("hook_auto")):
            c["hook"] = strip_emoji(rec["hook"])[:140]
            c.pop("hook_auto", None)


GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "inballin15/Gtaclip-cloud")


def _gh(method: str, path: str, body=None):
    import requests
    r = requests.request(method, f"https://api.github.com{path}", json=body, timeout=30,
                         headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def apply_issues(st: dict) -> None:
    """Dashboard 'Don't post' / 'Edit' taps. Handles EVERY open one, so a tap is never lost
    (GitHub drops queued runs when several taps arrive together)."""
    if not GH_TOKEN:
        return
    owner = REPO.split("/")[0]
    code, items = _gh("GET", f"/repos/{REPO}/issues?state=open&creator={owner}&per_page=100")
    if code != 200 or not isinstance(items, list):
        log(f"couldn't read dashboard requests ({code})")
        return
    for it in sorted(items, key=lambda i: i["created_at"]):
        title = it["title"].strip()
        body = (it.get("body") or "").replace("\r\n", "\n")
        m = re.match(r"^\s*(skip|edit|approve|unapprove):\s*(?:gtaclip/)?(?:tw_[a-z0-9_]+?_)?([A-Za-z0-9_\-]+)\s*$", title, re.I)
        if not m:
            continue
        action, key = m.group(1).lower(), m.group(2)
        cid = next((k for k in st["clips"] if k == key or k.startswith(key) or key.startswith(k)), None)
        if not cid:
            msg = "Couldn't find that clip (it may have expired)."
        elif action == "skip":
            st["clips"][cid].update(status="skip", skip_reason="removed by Nicholas")
            msg = "Done — this clip won't be posted."
        elif action == "edit":
            c = st["clips"][cid]
            hm = re.search(r"^hook:[ \t]*(.*)$", body, re.M)
            cm = re.search(r"^caption:[ \t]*(.*)\Z", body, re.M | re.S)
            if hm and hm.group(1).strip():
                c["hook"] = strip_emoji(hm.group(1))[:140]
                c["hook_by_user"] = True
            if cm:
                c["caption"] = cm.group(1).strip()[:2000]
            msg = "Done — the video was re-made with your hook and caption."
        else:
            msg = "Not needed anymore — use Start/Stop on the dashboard."
        log(f"{action} {cid}: {msg}")
        _gh("POST", f"/repos/{REPO}/issues/{it['number']}/comments", {"body": msg})
        _gh("PATCH", f"/repos/{REPO}/issues/{it['number']}", {"state": "closed"})


SWEARS = re.compile(r"\b(f+u+c*k+\w*|sh+i+t+\w*|bitch\w*|ass(hole)?|damn|dick|hell|wtf|stfu|crap|bastard|motherf\w*)\b", re.I)


def clean_hook(t: str) -> str:
    t = strip_emoji(t)
    t = SWEARS.sub("", t)
    t = re.sub(r"https?://\S+|[#@]\w+", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -|:,.")
    return t


JUNK_TITLE = re.compile(r"^(lol|lmao|w|l|yo|wow|omg|bro|gta ?6?|clip|meme|\W*)$", re.I)


def auto_hook(c: dict) -> tuple[str, str]:
    """Hook for a new clip without AI. Returns (hook, kind): kind "title" = cleaned-up Twitch title
    (good enough to post), "placeholder" = waits for a written hook."""
    name = c.get("streamer", "")
    t = clean_hook(c.get("title", ""))
    t = re.sub(r"(\w)\1{2,}", r"\1", t)                      # MADDDDD -> MAD, noooo -> no
    t = re.sub(r"\b(gta ?(6|vi)|#?gta6)\b", "GTA 6", t, flags=re.I)
    t = re.sub(r"\s*\|.*$", "", t).strip(" -!?.")              # stream-title junk after a pipe
    words = t.split()
    if len(words) >= 3 and len(t) >= 14 and not JUNK_TITLE.match(t) and not re.search(r"live|24/7|!sub|!comm", t, re.I):
        if t.isupper() and len(words) > 3:
            t = t.capitalize()
        hook = t[0].upper() + t[1:]
        if name.lower().split()[0] not in hook.lower() and not re.search(r"\b(he|she|they|his|her)\b", hook, re.I):
            hook = f"{name}: {hook}"
        if "gta" not in hook.lower():
            hook += " in GTA 6"
        return hook[:100], "title"
    return f"{name} can't believe this in GTA 6", "placeholder"


def mix_order(ready: list) -> list:
    """Post-now first, then take turns: a paid clip (Clix/Lacy, rotating), then two from other streamers (rotating),
    never the same streamer twice in a row when it can be avoided. Newest first within each streamer."""
    now = sorted([r for r in ready if r.get("post_now")], key=lambda r: r["post_now"])
    rest = sorted([r for r in ready if not r.get("post_now")], key=lambda r: r.get("created") or "", reverse=True)
    by: dict = {}
    for r in rest:
        by.setdefault(r["streamer"], []).append(r)
    paid = [n for n in by if by[n][0].get("campaign")]
    free = [n for n in by if n not in paid]
    out, last, k = [], None, 0
    while any(by.values()):
        want_paid = k % 3 == 0          # pattern: paid, other, other, paid, other, other ...
        pool = [n for n in (paid if want_paid else free) if by[n]] or [n for n in by if by[n]]
        pool.sort(key=lambda n: n == last)   # stable: keeps the rotation order
        n = pool[0]
        out.append(by[n].pop(0))
        last, k = n, k + 1
        # rotate the group so the next pick in that group is a different streamer
        grp = paid if n in paid else free
        if n in grp:
            grp.remove(n); grp.append(n)
    return now + out


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
    """The streamer's usual webcam spot really shows a clear face in most of the clip (not hidden, blurred or
    pixelated - e.g. a 'be right back' cam)."""
    x, y, w, h = [float(v) for v in cam.split(",")]
    frames = _frames(path, [max(0.5, duration * p) for p in (0.15, 0.35, 0.5, 0.65, 0.85)])
    hits = 0
    for fr in frames:
        H, W = fr.shape[:2]
        crop = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
        if not crop.size:
            continue
        sharp = cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        big = cv2.resize(crop, None, fx=2, fy=2)
        faces = [f for f in _faces(big, neighbors=5) if f[2] >= 0.12 * big.shape[1]]
        if faces and sharp > 40:
            hits += 1
    return hits >= max(3, len(frames) - 2)


def _frames(path: Path, secs):
    cap = cv2.VideoCapture(str(path))
    out = []
    for s in secs:
        cap.set(cv2.CAP_PROP_POS_MSEC, s * 1000)
        ok, fr = cap.read()
        if ok:
            if fr.shape[1] > 1280:   # work on a 1280-wide copy: same boxes (they're fractions), ~2-3x faster
                fr = cv2.resize(fr, (1280, int(fr.shape[0] * 1280 / fr.shape[1])), interpolation=cv2.INTER_AREA)
            out.append(fr)
    cap.release()
    return out


CAM_V = 6   # bump to re-check every queued clip with a newer webcam detector


def _best_face(frames):
    """A face that stays in the same place across frames (= the streamer's webcam), or None."""
    H, W = frames[0].shape[:2]
    per = [_faces(f) for f in frames]
    best, best_hits = None, 0
    for i, faces in enumerate(per):
        for (x, y, w, h) in faces:
            cx, cy = x + w / 2, y + h / 2
            hits = sum(1 for j, o in enumerate(per) if j != i and any(
                abs(ox + ow / 2 - cx) < 0.5 * w and abs(oy + oh / 2 - cy) < 0.5 * h and 0.6 < ow / w < 1.6
                for (ox, oy, ow, oh) in o))
            fx, fy = cx / W, cy / H
            in_middle = 0.28 < fx < 0.72 and 0.25 < fy < 0.75   # webcams sit at the edges, not mid-screen
            if hits >= 3 and not in_middle and (hits > best_hits or (hits == best_hits and w * h > best[2] * best[3])):
                best, best_hits = (x, y, w, h), hits
    return best


def _static_edges(frames):
    """Edges that stay put while the game moves = the webcam overlay's border."""
    gx, gy = [], []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx.append(np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)))
        gy.append(np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)))
    # 30th percentile across frames: strong in (almost) every frame
    return np.percentile(np.stack(gx), 30, axis=0), np.percentile(np.stack(gy), 30, axis=0)


def _overlay_rect(frames, face):
    """Trace the webcam overlay's real border around the face. Returns (x, y, w, h) in pixels or None."""
    H, W = frames[0].shape[:2]
    x, y, w, h = face
    vx, hy = _static_edges(frames)
    thr = 60.0
    r0, r1 = int(max(0, y - 0.6 * h)), int(min(H, y + 1.8 * h))       # rows the side borders must cross
    c0, c1 = int(max(0, x - 0.8 * w)), int(min(W, x + 1.8 * w))       # cols the top/bottom borders must cross

    def col_hit(cx):
        cx0, cx1 = max(0, cx - 1), min(W, cx + 2)
        return float(np.mean(np.max(vx[r0:r1, cx0:cx1], axis=1) > thr)) if r1 > r0 else 0.0

    def row_hit(ry):
        ry0, ry1 = max(0, ry - 1), min(H, ry + 2)
        return float(np.mean(np.max(hy[ry0:ry1, c0:c1], axis=0) > thr)) if c1 > c0 else 0.0

    def scan(rng, fn, edge):
        for v in rng:
            if fn(v) > 0.75:
                return v, True
        return edge, False

    left, fl = scan(range(int(x - 0.25 * w), int(max(-1, x - 5 * w)), -1), col_hit, 0)
    right, fr = scan(range(int(x + 1.25 * w), int(min(W, x + 6 * w))), col_hit, W)
    top, ft = scan(range(int(y - 0.35 * h), int(max(-1, y - 3 * h)), -1), row_hit, 0)
    bottom, fb = scan(range(int(y + 1.6 * h), int(min(H, y + 6 * h))), row_hit, H)
    # sides not found must be the screen edge (webcam pushed into a corner)
    if not fl and x - 5 * w > 0: return None
    if not fr and x + 6 * w < W: return None
    if not ft and y - 3 * h > 0: return None
    if not fb and y + 6 * h < H: return None
    rw, rh = right - left, bottom - top
    if rw < 1.8 * w or rh < 1.8 * h or rw > 0.6 * W or rh > 0.75 * H:
        return None
    pad = 3  # stay inside the border line
    return (left + pad, top + pad, rw - 2 * pad, rh - 2 * pad)


def _fit(rect, face, W, H):
    """Largest 1080:960 window inside the overlay, centred on the face (face ~40% from the top)."""
    rx, ry, rw, rh = rect
    fx, fy = face[0] + face[2] / 2, face[1] + face[3] / 2
    A = 1080 / 960
    if rw / rh > A:
        bh, bw = rh, rh * A
        bx = min(max(rx, fx - bw / 2), rx + rw - bw)
        by = ry
    else:
        bw, bh = rw, rw / A
        bx = rx
        by = min(max(ry, fy - 0.4 * bh), ry + rh - bh)
    return f"{bx / W:.4f},{by / H:.4f},{bw / W:.4f},{bh / H:.4f}"


def _guess_box(face, W, H):
    x, y, w, h = face
    aspect = 1080 / 960
    bw = min(W, max(w * 2.8, 0.135 * W))   # cut-out webcams (no border): head and shoulders, less game
    bh = bw / aspect
    if bh > H:
        bh, bw = float(H), H * aspect
    cx, cy = x + w / 2, y + h * 0.85
    bx = min(max(0.0, cx - bw / 2), W - bw)
    by = min(max(0.0, cy - bh / 2), H - bh)
    return f"{bx / W:.4f},{by / H:.4f},{bw / W:.4f},{bh / H:.4f}"


def _trim_black(frames, box: str) -> str:
    """Cut off black bars inside a webcam box (letterboxing / empty overlay space)."""
    H, W = frames[0].shape[:2]
    x, y, w, h = [float(v) for v in box.split(",")]
    X0, Y0, X1, Y1 = int(x * W), int(y * H), int((x + w) * W), int((y + h) * H)
    g = np.max(np.stack([cv2.cvtColor(f[Y0:Y1, X0:X1], cv2.COLOR_BGR2GRAY) for f in frames]), axis=0)  # bright in ANY frame
    rows = np.where(g.mean(axis=1) > 14)[0]
    cols = np.where(g.mean(axis=0) > 14)[0]
    if len(rows) < 10 or len(cols) < 10:
        return box
    ny0, ny1, nx0, nx1 = Y0 + rows[0], Y0 + rows[-1], X0 + cols[0], X0 + cols[-1]
    if (ny1 - ny0) > 0.92 * (Y1 - Y0) and (nx1 - nx0) > 0.92 * (X1 - X0):
        return box
    nw, nh = nx1 - nx0, ny1 - ny0
    A = 1080 / 960
    if nw / nh > A:
        bw, bh = nh * A, nh
        cx = nx0 + nw / 2
        for f in frames:   # centre on the face, not the middle of the box
            fs = _faces(f[ny0:ny1, nx0:nx1])
            if len(fs):
                fx, fy, fw, fh = max(fs, key=lambda r: r[2] * r[3])
                cx = nx0 + fx + fw / 2
                break
        bx = min(max(nx0, cx - bw / 2), nx1 - bw)
        by = ny0
    else:
        bw, bh = nw, nw / A
        bx = nx0
        by = ny0
    return f"{bx / W:.4f},{by / H:.4f},{bw / W:.4f},{bh / H:.4f}"


def _face_in_box(frames, box: str, need: int = 3) -> bool:
    """A guessed box only counts if a face really shows inside it in most frames."""
    x, y, w, h = [float(v) for v in box.split(",")]
    hits = 0
    for fr in frames:
        H, W = fr.shape[:2]
        crop = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
        if crop.size and _faces(cv2.resize(crop, None, fx=2, fy=2), neighbors=5):
            hits += 1
    return hits >= need


def find_cam_info(path: Path, duration: float, learned: list | None = None) -> tuple[str, str, list | None]:
    cam, q, frac = _find_cam_info(path, duration, learned)
    if not cam or q == "guess":
        return "", "", None   # no traced border and no known spot -> skip the clip rather than risk a wrong face
    frames = _frames(path, [max(0.5, duration * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)])
    if frames:
        cam = _trim_black(frames, cam)
    return cam, q, frac


def _find_cam_info(path: Path, duration: float, learned: list | None = None) -> tuple[str, str, list | None]:
    """Returns (cam box, quality, overlay rect as fractions). quality: 'edge' = traced the webcam border,
    'learned' = this streamer's known webcam spot, 'guess' = box around the face."""
    secs = [max(0.5, duration * p) for p in (0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92)]
    frames = _frames(path, secs)
    if not frames:
        return "", "", None
    H, W = frames[0].shape[:2]
    face = _best_face(frames)
    if not face:
        return "", "", None
    rect = _overlay_rect(frames, face)
    if rect:
        frac = [rect[0] / W, rect[1] / H, rect[2] / W, rect[3] / H]
        return _fit(rect, face, W, H), "edge", frac
    fx, fy = (face[0] + face[2] / 2) / W, (face[1] + face[3] / 2) / H
    for lr in learned or []:   # the streamer's webcam spot from earlier clips
        if lr[0] < fx < lr[0] + lr[2] and lr[1] < fy < lr[1] + lr[3]:
            px = (lr[0] * W, lr[1] * H, lr[2] * W, lr[3] * H)
            return _fit(px, face, W, H), "learned", lr
    return _guess_box(face, W, H), "guess", None


def find_cam(path: Path, duration: float) -> str:
    return find_cam_info(path, duration)[0]


def learn_cam(st: dict, login: str, frac: list) -> None:
    """Remember where this streamer's webcam sits (up to 4 layouts)."""
    L = st.setdefault("learned_cams", {}).setdefault(login, [])
    for i, o in enumerate(L):
        if all(abs(a - b) < 0.03 for a, b in zip(o, frac)):
            L[i] = [round((a + b) / 2, 4) for a, b in zip(o, frac)]
            return
    L.insert(0, [round(v, 4) for v in frac])
    del L[4:]


def cam_sheet(items: list, out: Path) -> None:
    """Contact sheet: every queued clip's frame with its webcam box drawn on (for checking by eye)."""
    tiles = []
    for cid, c, raw in items:
        fr = _frames(raw, [2.0])
        if not fr:
            continue
        im = cv2.resize(fr[0], (480, 270))
        x, y, w, h = [float(v) for v in c["cam"].split(",")]
        col = {"edge": (80, 220, 80), "learned": (220, 200, 40), "guess": (60, 60, 255)}.get(c.get("cam_q"), (60, 60, 255))
        cv2.rectangle(im, (int(x * 480), int(y * 270)), (int((x + w) * 480), int((y + h) * 270)), col, 3)
        cv2.rectangle(im, (0, 0), (480, 20), (0, 0, 0), -1)
        cv2.putText(im, f"{len(tiles)} {c['streamer']} {c.get('cam_q', '?')} {cid[:16]}", (4, 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        tiles.append(im)
    if not tiles:
        return
    cols = 4
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(str(out), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 70])


def duration_of(path: Path) -> float:
    cap = cv2.VideoCapture(str(path))
    n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS) or 30
    cap.release()
    return n / fps if n else 30.0


# ---------------------------------------------------------------- rendering
def ff(*args) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True)


def render_preview(raw: Path, cid: str) -> None:
    """Just the thumbnail; the site plays the final video (no second copy = half the storage)."""
    ff("-ss", "2", "-i", str(raw), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(VID / f"{cid}.jpg"))


CLIP_MAX_S = 22    # the top GTA 6 clip pages post 10-20 s videos (median 16 s); longer clips get trimmed
CLIP_KEEP_S = 18   # length of the trimmed part


def best_part(raw: Path, cid: str, tmp: Path, c: dict) -> Path:
    """Long clip -> the ~18 s around its biggest moment (loudest few seconds, with a short set-up before it)."""
    import viral
    try:
        a = viral._audio(raw)
        secs = len(a) // 16000
        if secs <= CLIP_MAX_S:
            return raw
        rms = np.array([np.sqrt(np.mean(a[i * 16000:(i + 1) * 16000] ** 2) + 1e-9) for i in range(secs)])
        db = 20 * np.log10(rms + 1e-9)
        sm = np.convolve(db, np.ones(3) / 3, mode="same")
        p = int(np.argmax(sm[1:-1])) + 1
        start = float(max(0, min(p - 7, secs - CLIP_KEEP_S)))
        end = float(min(secs, start + CLIP_KEEP_S))
        if secs - end < 3:          # don't leave a tiny leftover at the end
            end = float(secs)
        out = tmp / f"{cid}_best.mp4"
        if not out.exists():
            viral.cut(raw, start, end, out)
        c["trim"] = [start, end]
        log(f"trimmed {cid}: {secs}s -> {start:.0f}-{end:.0f}s")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"trim skipped {cid}: {e}")
        return raw


def cam_without_chat(path: Path, cam: str) -> str:
    """Some streamers (Kai) have chat scrolling over the bottom of their webcam. Find where that text band starts
    and end the webcam box above it, so no chat shows between the face and the gameplay."""
    try:
        x, y, w, h = [float(v) for v in cam.split(",")]
        cap = cv2.VideoCapture(str(path))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        dens = []
        for fr in _frames(path, [max(0.5, dur * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]):
            H, W = fr.shape[:2]
            c = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
            if c.size:
                e = cv2.Canny(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), 80, 200)
                dens.append(e.mean(axis=1) / 255)
        if len(dens) < 3:
            return cam
        n = min(len(d) for d in dens)
        d = np.convolve(np.mean([d[:n] for d in dens], axis=0), np.ones(5) / 5, mode="same")
        base = float(np.median(d[int(n * 0.3):int(n * 0.55)]))
        k = max(3, int(n * 0.04))
        for r in range(int(n * 0.55), n - k):
            if d[r:r + k].mean() > max(2.2 * base, 0.06):
                while r > n * 0.5 and d[r - 1] > 1.4 * base:   # walk up to where the chat really starts
                    r -= 1
                frac = max(0.5, (r - n * 0.04) / n)
                return f"{x:.4f},{y:.4f},{w:.4f},{h * frac:.4f}"
        return cam
    except Exception:  # noqa: BLE001
        return cam


def purple_caption_cut(path: Path, duration: float, cam: str) -> float:
    """Streamers like Lacy/Kai run a live caption overlay (white words, purple highlight) near the bottom.
    If it's there and low, return how much to zoom off the bottom (0.08-0.14) to push it out; if it sits higher,
    return -top (a strip from 'top' down ~14% gets blurred instead of zooming more); otherwise 0."""
    try:
        if not duration:
            cap = cv2.VideoCapture(str(path))
            duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
            cap.release()
        frames = _frames(path, [max(0.5, duration * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)])
        if not frames:
            return 0.0
        H, W = frames[0].shape[:2]
        cx, cy, cw, ch = ([float(v) for v in cam.split(",")] if cam else [0, 0, 0, 0])
        y0, x0, x1 = int(H * 0.6), int(W * 0.22), int(W * 0.78)   # only the part of the gameplay that shows
        tops = []
        for fr in frames:
            hsv = cv2.cvtColor(fr[y0:, x0:x1], cv2.COLOR_BGR2HSV)
            m = cv2.inRange(hsv, (122, 110, 110), (158, 255, 255))
            if cw:   # ignore the webcam's own purple border/lighting
                a0, a1 = max(0, int(cy * H) - y0), max(0, int((cy + ch) * H) - y0)
                b0, b1 = max(0, int(cx * W) - x0), max(0, int((cx + cw) * W) - x0)
                m[a0:a1, b0:b1] = 0
            rows = np.where(m.sum(axis=1) / 255 > 0.015 * (x1 - x0))[0]
            if len(rows) and (rows[-1] - rows[0]) < 0.12 * H:   # a thin band = text, not a purple scene
                tops.append((rows[0] + y0) / H)
        if len(tops) < 2 or max(tops) - min(tops) > 0.06:
            return 0.0
        need = 1 - min(tops) + 0.05      # the white words sit just above the purple highlight
        if need <= 0.14:
            return round(max(0.08, need), 3)
        return -round(min(tops) - 0.06, 3)   # captions sit high: negative = blur a strip starting at this height
    except Exception:  # noqa: BLE001
        return 0.0


def render_final(raw: Path, cid: str, cam: str, hook: str | None, tmp: Path, out: str = "_final",
                 layout: str = "split", cut: float = 0.0) -> None:
    """Split screen (webcam top, gameplay bottom). hook=None makes the clean copy with no caption box.
    layout="full": no webcam in the video -> the whole frame centred on a blurred background."""
    x, y, w, h = [float(v) for v in cam.split(",")]
    if layout == "full":
        base = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "boxblur=20:2,setsar=1[bg];[b]scale=1080:-2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        base = (
            f"[0:v]split=2[a][b];"
            # webcam: a wide strip, ~1/3 of the screen (cam boxes taller than that lose a little top/bottom,
            # cut so the face stays in the upper-middle)
            f"[a]crop=w=iw*{w}:h='min(ih*{h},iw*{w}*{TOP_H}/1080)':x=iw*{x}:y='ih*{y}+(ih*{h}-oh)*0.1',"
            # small webcams get blown up ~4-5x: light denoise first, sharp (lanczos) scaling, then sharpen
            f"hqdn3d=1.5:1.5:3:3,scale=1080:{TOP_H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop=1080:{TOP_H},unsharp=5:5:1.0:5:5:0.0,setsar=1[top];"
            + (f"[b]split=2[b1][b2];[b2]crop=iw:ih*0.15:0:ih*{-cut},boxblur=luma_radius=40:luma_power=3:chroma_radius=18:chroma_power=3[bl];"
               f"[b1][bl]overlay=0:main_h*{-cut}[bb];" if cut < 0 else "") +
            f"[{'bb' if cut < 0 else 'b'}]crop=iw*{1 - max(CAPTION_CUT, cut, 0)}:ih*{1 - max(CAPTION_CUT, cut, 0)}:(iw-ow)/2:0,scale=-2:{1920 - TOP_H}:flags=lanczos,crop=1080:{1920 - TOP_H},"
            f"unsharp=3:3:0.4:3:3:0.0,setsar=1[bot];"
            f"[top][bot]vstack=inputs=2"
        )
    if hook:
        png = render_hook(hook, str(tmp / f"{cid}_hook.png"))
        inputs = ["-i", str(raw), "-i", png]
        oy = "(H-h)/2" if layout == "full" else f"{TOP_H}-h*0.2"   # hook box sits on the webcam/gameplay line
        flt = base + f"[st];[st][1:v]overlay=(W-w)/2:{oy},format=yuv420p[out]"
    else:
        inputs = ["-i", str(raw)]
        flt = base + ",format=yuv420p[out]"
    ff(*inputs, "-filter_complex", flt, "-map", "[out]", "-map", "0:a?",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "5000k", "-bufsize", "10M", "-r", "30",
       "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(VID / f"{cid}{out}.mp4"))


# ---------------------------------------------------------------- links (YouTube / Twitch VOD)
def _secs(t) -> float | None:
    if t in (None, ""):
        return None
    if isinstance(t, (int, float)):
        return float(t)
    p = [float(x) for x in str(t).strip().split(":")]
    v = 0.0
    for x in p:
        v = v * 60 + x
    return v


def fetch_link_audio(url: str, section, tmp: Path) -> tuple[Path, float, str]:
    """Download just the audio of a link (small + fast) to find the moments. Returns (file, offset, title)."""
    import yt_dlp
    from yt_dlp.utils import download_range_func
    a = b = None
    if section:
        parts = re.split(r"\s*[-–to]+\s*", str(section).strip())
        a, b = _secs(parts[0]), _secs(parts[1]) if len(parts) > 1 else None
    opts = {"format": "bestaudio/best", "outtmpl": str(tmp / "linkaudio.%(ext)s"), "quiet": True,
            "noprogress": True, "noplaylist": True, "ffmpeg_location": FF}
    if a is not None:
        opts["download_ranges"] = download_range_func(None, [(a, b if b else a + 4 * 3600)])
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
        f = Path(y.prepare_filename(info))
    if not f.exists():
        f = next(tmp.glob("linkaudio.*"))
    log(f"link audio: {info.get('title')} ({int(info.get('duration') or 0) // 60} min){f' section {section}' if section else ''}")
    return f, float(a or 0), (info.get("title") or "Link")[:60]


def fetch_link_section(url: str, start: float, end: float, raw: Path, tmp: Path) -> None:
    """Download only the video section of a chosen moment, then normalise it to MP4."""
    import yt_dlp
    from yt_dlp.utils import download_range_func
    import viral
    out = tmp / f"sec_{int(start)}"
    opts = {"format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "outtmpl": str(out) + ".%(ext)s",
            "quiet": True, "noprogress": True, "noplaylist": True, "merge_output_format": "mp4",
            "download_ranges": download_range_func(None, [(max(0.0, start - 1), end + 1)]),
            "force_keyframes_at_cuts": True, "ffmpeg_location": FF}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([url])
    f = next(tmp.glob(f"sec_{int(start)}.*"))
    viral.cut(f, 0, (end - start) + 2, raw)
    f.unlink(missing_ok=True)


# ---------------------------------------------------------------- your own uploads -> clips
def process_uploads(st: dict, tmp: Path) -> None:
    """Dashboard upload: the video arrives in pieces on a branch 'upload-<id>' + an issue 'upload: <id>'.
    Find the viral moments, cut them, and put them in the queue like any other clip."""
    if not GH_TOKEN:
        return
    owner = REPO.split("/")[0]
    code, items = _gh("GET", f"/repos/{REPO}/issues?state=open&creator={owner}&per_page=50")
    if code != 200 or not isinstance(items, list):
        return
    for it in [i for i in items if i["title"].lower().startswith("upload:")]:
        num = it["number"]
        try:
            body = (it.get("body") or "{}").strip()
            job = json.JSONDecoder().raw_decode(body[body.index("{"):])[0]   # ignore anything after the JSON
            ref, parts = job.get("ref", ""), job.get("parts", [])
            if not job.get("url") and not (ref and parts):
                raise ValueError("no video")
        except Exception:  # noqa: BLE001
            _gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": "Couldn't read this upload — please try again."})
            _gh("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
            continue
        _gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": "Got your video — finding the best moments now (usually 5–20 min)."})
        src = tmp / f"upload_{job.get('id', num)}.mp4"
        link = job.get("url") or ""
        offset = 0.0
        try:
            import viral
            if link:
                src, offset, title = fetch_link_audio(link, job.get("section"), tmp)
                job.setdefault("name", title)
                if not job.get("name") or job["name"] == "Link":
                    job["name"] = title
            else:
                subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", f"{ref}:refs/remotes/origin/{ref}"], check=True)
                with open(src, "wb") as fh:
                    for part in parts:
                        fh.write(subprocess.run(["git", "show", f"origin/{ref}:{part}"], capture_output=True, check=True).stdout)
            moments = viral.find_moments(src, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"upload {num} failed: {e}")
            hint = ("YouTube blocked the download from GitHub's servers — save the video and use Upload instead."
                    if "youtu" in link and re.search(r"sign in|bot|confirm|403|429", str(e), re.I)
                    else "Try a shorter part (add a time range) or upload the file instead." if link
                    else "Try a shorter video or another format (MP4).")
            _gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": f"Sorry — that video couldn't be processed ({str(e)[:150]}). {hint}"})
            _gh("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
            continue
        login = (job.get("login") or "").lower()
        s_cfg = CFG["streamers"].get(login, {})
        name = s_cfg.get("name") or job.get("streamer") or "Nicholas"
        made = []
        for i, m in enumerate(moments):
            cid = f"up{job.get('id', num)}_{i + 1}"
            raw = RAW / f"{cid}.mp4"
            try:
                import viral
                if link:
                    fetch_link_section(link, offset + m["start"], offset + m["end"], raw, tmp)
                else:
                    viral.cut(src, m["start"], m["end"], raw)
                dur = duration_of(raw)
                cam, q, frac = find_cam_info(raw, dur, st.get("learned_cams", {}).get(login))
                rec = {"status": "new", "source": "upload", "streamer": name, "login": login,
                       "tag": s_cfg.get("tag") or ("#" + re.sub(r"\W", "", name)), "campaign": s_cfg.get("campaign", ""),
                       "title": f"{job.get('name', 'Your video')} @ {int(m['start'] // 60)}:{int(m['start'] % 60):02d}",
                       "created": iso(NOW), "found": iso(NOW), "twitch": link, "transcript": m["text"],
                       "seconds": round(dur), "moment": {k: m[k] for k in ("start", "end", "score", "reasons")},
                       "cam_v": CAM_V}
                if m.get("hook_ai"):
                    rec.update(hook=m["hook"], hook_kind="ai")
                elif m.get("hook"):
                    rec.update(hook=m["hook"], hook_auto=True, hook_kind="transcript")
                if m.get("ai_score") is not None:
                    rec["moment"]["ai_score"] = m["ai_score"]
                    sc = int(m["ai_score"] * 10)
                    rec.update(viral=sc, viral_verdict="post" if sc >= 70 else "maybe" if sc >= 45 else "skip",
                               viral_why=", ".join(m.get("reasons", []))[:90])
                if cam and q in ("edge", "learned"):
                    rec.update(cam=cam, cam_q=q, layout="split")
                else:
                    rec.update(cam="0,0,1,1", cam_q="none", layout="full")
                st["clips"][cid] = rec
                made.append(f"- {int(m['start'] // 60)}:{int(m['start'] % 60):02d}–{int(m['end'] // 60)}:{int(m['end'] % 60):02d} "
                            f"({', '.join(m['reasons']) or 'good moment'}) — {m.get('hook') or ''}")
            except Exception as e:  # noqa: BLE001
                log(f"cutting clip {cid} failed: {e}")
        msg = (f"Made {len(made)} clips from **{job.get('name', 'your video')}** — they're in the Queue (tagged 'Your video'):\n"
               + "\n".join(made)) if made else "No strong moments found in this video."
        _gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": msg})
        _gh("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
        if ref:
            _gh("DELETE", f"/repos/{REPO}/git/refs/heads/{ref}")
        log(f"upload {num}: {len(made)} clips")


def import_vod_clips(st: dict) -> None:
    """Moments the VOD miner found (branches 'vodclip-*') -> queue clips."""
    try:
        refs = subprocess.run(["git", "ls-remote", "origin", "refs/heads/vodclip-*"], capture_output=True, text=True,
                              check=True).stdout.split()
    except Exception as e:  # noqa: BLE001
        log(f"vod import: {e}")
        return
    bad_words = ("penis", "dick", "pussy", "nigg", "fag", "retard", " cock", "strip club", "stripper", "lap dance",
                 "titties", "booty", "thong", "bikini")
    title_bad = [w.lower() for w in CFG["discovery"]["skip_title_words"]]
    for ref in [r for r in refs if r.startswith("refs/heads/vodclip-")]:
        name = ref.split("/", 2)[2]
        d = Path(tempfile.mkdtemp())
        try:
            subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", f"{ref}:refs/remotes/origin/{name}"], check=True)
            tar = subprocess.run(["git", "archive", f"origin/{name}"], capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(d)], input=tar, check=True)
        except Exception as e:  # noqa: BLE001
            log(f"vod import {name} failed: {e}")
            continue
        n = 0
        for mf in sorted(d.glob("*.json")):
            try:
                m = json.loads(mf.read_text())
                cid = m["id"]
                if cid in st["clips"] or not (d / f"{cid}.mp4").exists():
                    continue
                raw = RAW / f"{cid}.mp4"
                shutil.move(str(d / f"{cid}.mp4"), raw)
                dur = duration_of(raw)
                rec = {"status": "new", "source": "vod", "streamer": m["streamer"], "login": m["login"], "tag": m["tag"],
                       "campaign": m.get("campaign", ""), "title": m["title"], "views": 0, "created": m["created"],
                       "found": iso(NOW), "twitch": m["twitch"], "transcript": m.get("transcript", ""),
                       "seconds": round(dur), "moment": m.get("moment", {}), "cam_v": CAM_V}
                txt = (m.get("transcript") or "").lower()
                if any(w in txt for w in bad_words) or any(w in m["title"].lower() for w in title_bad):
                    rec.update(status="skip", skip_reason="content filter")
                else:
                    cam, q, frac = find_cam_info(raw, dur, st.get("learned_cams", {}).get(m["login"]))
                    usual = ",".join(str(v) for v in (CFG["streamers"].get(m["login"], {}).get("facecam") or []))
                    if not cam and usual and box_has_face(raw, dur, usual):
                        cam, q = usual, "learned"
                    if not cam:
                        rec.update(status="skip", skip_reason="no webcam found")
                    else:
                        rec.update(cam=cam, cam_q=q)
                        if q == "edge" and frac:
                            learn_cam(st, m["login"], frac)
                    if m.get("hook_ai") and m.get("hook"):
                        rec.update(hook=m["hook"], hook_kind="ai")
                    ai = (m.get("moment") or {}).get("ai_score")
                    if ai is not None:
                        sc = int(ai * 10)
                        rec.update(viral=sc, viral_verdict="post" if sc >= 70 else "maybe" if sc >= 45 else "skip",
                                   viral_why=", ".join((m.get("moment") or {}).get("reasons") or [])[:90])
                st["clips"][cid] = rec
                n += 1
                log(f"VOD moment {m['streamer']} -> {rec['status']} {rec.get('cam', rec.get('skip_reason', ''))}")
            except Exception as e:  # noqa: BLE001
                log(f"vod clip import failed: {e}")
        _gh("DELETE", f"/repos/{REPO}/git/refs/heads/{name}")
        shutil.rmtree(d, ignore_errors=True)
        log(f"vod import {name}: {n} clips")


def publish_site() -> None:
    """Put the current site folder live now (mid-run), same way the workflow does at the end."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    sh = r"""
set -e
git fetch -q origin main || true
git show origin/main:site/index.html > /tmp/_idx.html && cp /tmp/_idx.html "$SITE/index.html" || true
git show origin/main:site/app.html > /tmp/_app.html && cp /tmp/_app.html "$SITE/app.html" || true
b=$(git hash-object "$SITE/app.html" | cut -c1-10); sed -i "s/__BUILD__/$b/" "$SITE/app.html"
python3 -c "import json,sys;p=sys.argv[2]+'/queue.json';d=json.load(open(p));d['site_build']=sys.argv[1];json.dump(d,open(p,'w'),indent=1,ensure_ascii=False)" "$b" "$SITE"
touch "$SITE/.nojekyll"
export GIT_INDEX_FILE=/tmp/idx-mid; rm -f "$GIT_INDEX_FILE"
git --work-tree="$SITE" add -A
tree=$(git write-tree); unset GIT_INDEX_FILE
c=$(git -c user.name=gtaclip-bot -c user.email=gtaclip-bot@users.noreply.github.com commit-tree "$tree" -m "gh-pages (mid-run)")
git push -qf origin "$c:refs/heads/gh-pages"
"""
    try:
        subprocess.run(["bash", "-c", sh], check=True, cwd=str(ROOT), env={**os.environ, "SITE": str(SITE)}, timeout=240)
        log("site updated (mid-run)")
    except Exception as e:  # noqa: BLE001
        log(f"mid-run publish skipped: {e}")


# ---------------------------------------------------------------- main
def main() -> None:
    st = load_state()
    clips = st["clips"]
    apply_posted(st)
    apply_issues(st)
    control = {}
    try:
        control = json.loads((POSTED_DIR / "control.json").read_text())
    except Exception:  # noqa: BLE001
        pass
    for c in clips.values():
        c.pop("post_now", None)
    for p in control.get("postnow", []):
        if p.get("id") in clips:
            clips[p["id"]]["post_now"] = p.get("at") or iso(NOW)

    SITE.mkdir(parents=True, exist_ok=True)
    VID.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())

    for c in clips.values():  # campaigns can be added later (Kai, YourRAGE): keep clips in step
        camp = (CFG["streamers"].get(c.get("login", ""), {}) or {}).get("campaign", "")
        if camp and c.get("status") == "new" and c.get("campaign") != camp:
            c["campaign"] = camp
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

    process_uploads(st, tmp)
    import_vod_clips(st)

    # re-check every queued clip's webcam box with the current detector (edge-traced boxes)
    for cid, c in clips.items():
        if c.get("status") != "new" or c.get("cam_v", 1) >= CAM_V or c.get("layout") == "full":
            continue
        if c.get("cam_v", 1) in (3, 4, 5) and c.get("cam_q") in ("edge", "learned"):  # v6 only changes guessed boxes
            c["cam_v"] = CAM_V
            continue
        raw = RAW / f"{cid}.mp4"
        if not raw.exists():
            continue
        c["cam_v"] = CAM_V
        try:
            cam, q, frac = find_cam_info(raw, duration_of(raw), st.get("learned_cams", {}).get(c.get("login", "")))
        except Exception as e:  # noqa: BLE001
            log(f"cam re-check failed {cid}: {e}")
            continue
        if not cam:
            c.update(status="skip", skip_reason="no webcam found (re-check)")
            log(f"re-check: no webcam -> skip {c['streamer']} {cid}")
            continue
        if q == "edge" and frac:
            learn_cam(st, c.get("login", ""), frac)
        if cam != c.get("cam"):
            log(f"re-check: new webcam box {c['streamer']} {cid} {q} {cam}")
        c.update(cam=cam, cam_q=q)

    # 3. new clips (not on issue-triggered runs, to keep those fast)
    picks = []
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
                s = CFG["streamers"].get(c["login"]) or {"name": c["name"], "tag": c.get("tag", "")}
                rec = {
                    "status": "new", "streamer": c["name"], "login": c["login"], "tag": s.get("tag", ""),
                    "campaign": s.get("campaign", "") or "", "title": c["title"], "views": c["view_count"],
                    "created": c["created_at"], "found": iso(NOW), "twitch": c["url"], "transcript": text,
                    "seconds": round(dur),
                }
                bad = [w.lower() for w in CFG["discovery"]["skip_title_words"]]
                if any(w in text.lower() for w in ("penis", "dick", "pussy", "nigg", "fag", "retard", " cock", "strip club",
                                                   "stripper", "lap dance", "titties", "booty", "thong", "bikini")):
                    rec.update(status="skip", skip_reason="sexual remark or slur in audio")
                elif any(w in c["title"].lower() for w in bad):
                    rec.update(status="skip", skip_reason="title filter")
                else:
                    cam, q, frac = find_cam_info(raw, dur, st.get("learned_cams", {}).get(c["login"]))
                    usual = ",".join(str(v) for v in (s.get("facecam") or []))
                    if not cam and usual and box_has_face(raw, dur, usual):
                        cam, q = usual, "learned"
                    if not cam:
                        rec.update(status="skip", skip_reason="no webcam found")
                    else:
                        rec.update(cam=cam, cam_q=q, cam_v=CAM_V)
                        if q == "edge" and frac:
                            learn_cam(st, c["login"], frac)
                clips[cid] = rec
                log(f"{c['name']} '{c['title']}' -> {rec['status']} {rec.get('cam', rec.get('skip_reason', ''))}")
            except Exception as e:  # noqa: BLE001
                log(f"failed {c['url']}: {e}")

    def write_queue() -> None:
        ready = [dict(id=cid, **{k: v for k, v in c.items() if k not in ("final_hash",)})
                 for cid, c in clips.items() if c["status"] == "new"]
        for r in ready:
            r["video"] = f"v/{r['id']}_final.mp4"
            r["poster"] = f"v/{r['id']}.jpg"
            _hh = hashlib.sha1((r.get("hook", "") + r.get("cam", "") + LAYOUT_V + r.get("layout", "")).encode()).hexdigest()[:10]
            r["final"] = (f"v/{r['id']}_final.mp4?v={r['final_v']}"
                          if r.get("final_v") and r.get("hook") and r.get("final_v") == _hh
                          and (VID / f"{r['id']}_final.mp4").exists() else "")
            r["final_abs"] = f"{PAGES}/v/{r['id']}_final.mp4?v={r['final_v']}" if r["final"] else ""
            r["clean"] = (f"v/{r['id']}_clean.mp4?v={r['clean_v']}"
                          if r.get("clean_v") and (VID / f"{r['id']}_clean.mp4").exists() else "")
            r["clipped"] = r.get("created")
        ready = mix_order(ready)
        for r in ready:
            r["approved"] = bool(r.get("approved"))
            r["post_now"] = bool(r.get("post_now"))
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
        _app = (ROOT / "site" / "app.html").read_bytes()
        build = hashlib.sha1(b"blob %d\0" % len(_app) + _app).hexdigest()[:10]   # same id as `git hash-object`
        (SITE / "queue.json").write_text(json.dumps({
            "site_build": build,
            "updated": iso(NOW),
            # 'paused' stays true for the old Claude posting tasks (they must not post anymore);
            # the real Start/Stop lives in live.json on the 'posted' branch, run by poster.py
            "paused": True, "posting_on": not control.get("paused", True), "ready": ready, "posted": posted[:40],
            "fetcher": {"last_run_utc": NOW.strftime("%H:%M:%S"), "last_run": iso(NOW), "fresh_by_streamer": status,
                        "picked": len(picks),
                        "added": sum(1 for p in picks if clips.get(p["id"], {}).get("status") == "new"),
                        "found_24h": sum(1 for c in clips.values() if c.get("found", "") >= iso(NOW - timedelta(hours=24))),
                        "kept_24h": sum(1 for c in clips.values() if c.get("found", "") >= iso(NOW - timedelta(hours=24))
                                        and c["status"] in ("new", "used")),
                        "every_min": 10},
            "live": LIVE,
            "ai": {"status": st.get("ai_status", "no key"), "checked": st.get("ai_checked", ""),
                   "rated": sum(1 for c in clips.values() if c["status"] == "new" and "viral" in c)},
        }, indent=1, ensure_ascii=False))


    # one-time: bring back fresh clips that the old replace-every-run rule / cap threw out
    if st.get("redo_all_v", 0) < 1:   # one time: re-make every queued clip (chat/purple fixes + picture check)
        st["redo_all_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                c.pop("final_hash", None); c.pop("clean_v", None)
    if st.get("redo_kai_lacy_v", 0) < 1:   # one time: re-make queued Kai/Lacy clips (chat over webcam, purple captions)
        st["redo_kai_lacy_v"] = 1
        for c in clips.values():
            if c["status"] == "new" and c.get("login") in ("kaicenat", "lacy"):
                c.pop("final_v", None); c.pop("clean_v", None)
    if st.get("restored_v", 0) < 1:
        st["restored_v"] = 1
        n_back = 0
        for cid, c in clips.items():
            if c["status"] == "expired" and c.get("skip_reason") in ("replaced by newer clips", "queue full") \
                    and c.get("cam") and NOW - parse(c.get("created") or iso(NOW)) < timedelta(hours=KEEP_NEW_HOURS):
                c["status"] = "new"
                c.pop("skip_reason", None)
                n_back += 1
        log(f"brought back {n_back} fresh clips")

    # 3c. every clip-finder run replaces the queue with its fresh finds (if it found any).
    #     Kept: your uploads, Post-now picks, clips you edited, and VOD moments from the last hour.
    if picks and REPLACE_QUEUE_EACH_RUN:
        fresh_ids = {p["id"] for p in picks if clips.get(p["id"], {}).get("status") == "new"}
        if fresh_ids:
            n_old = 0
            for cid, c in clips.items():
                if c["status"] != "new" or cid in fresh_ids:
                    continue
                if c.get("source") == "upload" or c.get("post_now") or c.get("hook_by_user"):
                    continue
                if c.get("source") == "vod" and NOW - parse(c.get("found") or iso(NOW)) < timedelta(hours=1):
                    continue
                c.update(status="expired", skip_reason="replaced by newer clips")
                n_old += 1
            log(f"queue refreshed: {len(fresh_ids)} new clips replace {n_old} older ones")

    # 4a. keep the queue to the best QUEUE_MAX clips (about 3 days of posts) so the site stays small
    fresh = [dict(id=k, **v) for k, v in clips.items() if v["status"] == "new"]
    keep = {r["id"] for r in mix_order(fresh)[:QUEUE_MAX]}
    for r in fresh:
        if r["id"] not in keep and not r.get("post_now") and not r.get("hook_by_user") and r.get("source") != "upload":
            clips[r["id"]].update(status="expired", skip_reason="queue full")
    if len(fresh) > QUEUE_MAX:
        log(f"queue full: kept {QUEUE_MAX} of {len(fresh)} clips")

    # 3b. viral score for every queued clip BEFORE the videos are made (45 s cap, so Gemini can't hold them up) (Gemini, like OpusClip) + a better hook; throttled to save the free quota
    ai_status = st.get("ai_status", "")
    try:
        import viral
        if st.get("rehook_v", 0) < 1:   # one time: clips that kept a plain auto hook get rated again for a proper hook
            st["rehook_v"] = 1
            for v in clips.values():
                if v["status"] == "new" and v.get("hook_kind") not in ("ai", "user") and not v.get("hook_by_user"):
                    v.pop("viral", None)
        todo = [dict(id=k, **v) for k, v in clips.items() if v["status"] == "new" and "viral" not in v
                and (v.get("transcript") or v.get("title"))]
        backoff = st.get("ai_backoff2", "")
        oldest = min((parse(r.get("found") or iso(NOW)) for r in todo), default=NOW)
        if control.get("ai_off"):
            todo = []
            st["ai_status"] = "turned off in Settings"
        if todo and (not backoff or parse(backoff) < NOW) and (len(todo) >= 3 or NOW - oldest > timedelta(minutes=25)
                                                             or st.get("ai_status", "ok") != "ok"):
            res, ai_status = viral.ai_score_clips(todo[:20], log, budget_s=45)
            for cid, r in res.items():
                c = clips.get(cid)
                if not c:
                    continue
                c.update(viral=r["score"], viral_why=r["why"], viral_verdict=r["verdict"])
                if r["score"] <= 10 and not c.get("hook_by_user") and not c.get("post_now"):
                    c.update(status="skip", skip_reason=f"AI: {r['why'] or 'not postable'}")
                    log(f"AI removed {cid}: {r['why']}")
                    continue
                if r["hook"] and len(r["hook"]) >= 12 and (c.get("hook_auto") or not c.get("hook")) and not c.get("hook_by_user"):
                    c.update(hook=r["hook"], hook_kind="ai")
                    c.pop("hook_auto", None)
            if not res and re.search(r"429|quota|exhaust|rate", ai_status, re.I):
                st["ai_backoff2"] = iso(NOW + timedelta(minutes=60))   # free daily/minute limit: try again in an hour
            st["ai_status"] = ai_status
            st["ai_checked"] = iso(NOW)
    except Exception as e:  # noqa: BLE001
        log(f"AI scoring failed: {e}")

    # 4. drop old stuff, get queued clips ready (thumbnail, hook); videos are made in 4b
    jobs = []
    for cid, c in list(clips.items()):
        age_ref = c.get("posted_at") or c.get("found") or iso(NOW)
        age_h = (NOW - parse(age_ref)).total_seconds() / 3600
        clip_age_h = (NOW - parse(c.get("created") or c.get("found") or iso(NOW))).total_seconds() / 3600
        keep_h = KEEP_UPLOAD_HOURS if c.get("source") == "upload" else KEEP_NEW_HOURS
        if (c["status"] == "new" and clip_age_h > keep_h) or (c["status"] != "new" and age_h > KEEP_POSTED_HOURS):
            c["status"] = "expired" if c["status"] == "new" else c["status"]
        if c["status"] == "used" and age_h < 24:
            continue  # keep the posted video online a day (Buffer downloads it from here)
        if c["status"] != "new":
            for suf in (".mp4", "_final.mp4", "_clean.mp4", ".jpg"):
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
            (VID / f"{cid}.mp4").unlink(missing_ok=True)  # old preview copies
            if not (VID / f"{cid}.jpg").exists():
                render_preview(raw, cid)
            if not c.get("hook"):
                c["hook"], kind = auto_hook(c)
                c["hook_auto"] = True
                c["hook_kind"] = kind
                log(f"auto hook {cid}: {c['hook']}")
            if c.get("hook"):
                hh = hashlib.sha1((c["hook"] + c["cam"] + LAYOUT_V + c.get("layout", "")).encode()).hexdigest()[:10]
                ch = hashlib.sha1((c["cam"] + LAYOUT_V + "clean" + c.get("layout", "")).encode()).hexdigest()[:10]
                need_f = c.get("final_hash") != hh or not (VID / f"{cid}_final.mp4").exists()
                need_c = c.get("clean_v") != ch or not (VID / f"{cid}_clean.mp4").exists()
                if need_f or need_c:
                    jobs.append((cid, raw, hh, ch, need_f, need_c))
        except Exception as e:  # noqa: BLE001
            log(f"prep failed {cid}: {e}")

    write_queue()          # the queue (with thumbnails) shows up right away; videos follow
    publish_site()

    # 4b. make the videos: best clips first, 2 at a time, at most MAX_RENDERS per run (the rest next run),
    #     and put finished ones on the site every few videos so nothing waits for the whole run
    order = {r["id"]: i for i, r in enumerate(mix_order([dict(id=k, **v) for k, v in clips.items() if v["status"] == "new"]))}
    jobs.sort(key=lambda j: (not clips[j[0]].get("post_now"), order.get(j[0], 999)))
    if len(jobs) > MAX_RENDERS:
        log(f"making {MAX_RENDERS} of {len(jobs)} videos now; the rest next run")
    jobs = jobs[:MAX_RENDERS]

    def make(job):
        cid, raw, hh, ch, need_f, need_c = job
        c = clips[cid]
        lay = c.get("layout", "split")
        raw = best_part(raw, cid, tmp, c)
        if c.get("pic_ok") is None and not control.get("ai_off"):   # look at the picture: no nudity / gore
            try:
                import viral
                fr = _frames(raw, [1.5, 6, 11, 16])
                jp = [cv2.imencode(".jpg", cv2.resize(f, (768, int(f.shape[0] * 768 / f.shape[1]))),
                                   [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes() for f in fr]
                ok, why = viral.ai_visual_ok(jp, log)
                if ok is not None:
                    c["pic_ok"] = ok
                if ok is False and not c.get("post_now"):
                    c.update(status="skip", skip_reason=f"AI picture check: {why or 'nudity/sexual content'}")
                    log(f"picture check removed {cid}: {why}")
                    return job
            except Exception as e:  # noqa: BLE001
                log(f"picture check skipped {cid}: {e}")
        cut = 0.0
        cam = c.get("cam", "")
        if lay == "split" and cam:
            cam2 = cam_without_chat(raw, cam)
            if cam2 != cam:
                log(f"chat over the webcam in {cid}: webcam box ends above it")
                cam = cam2
        if lay == "split":
            cut = purple_caption_cut(raw, 0, c.get("cam", ""))
            if cut:
                log(f"purple captions in {cid}: " + (f"zoomed gameplay {cut:.0%} off the bottom" if cut > 0
                                                       else f"blurred a strip at {-cut:.0%} height"))
            c["cap_cut"] = cut
        if need_f:
            render_final(raw, cid, cam, c["hook"], tmp, layout=lay, cut=cut)
        if need_c:
            render_final(raw, cid, cam, None, tmp, out="_clean", layout=lay, cut=cut)
        return job

    from concurrent.futures import ThreadPoolExecutor, as_completed
    done = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(make, j) for j in jobs]
        for fut in as_completed(futs):
            try:
                cid, raw, hh, ch, need_f, need_c = fut.result()
                c = clips[cid]
                if need_f:
                    c["final_hash"] = c["final_v"] = hh
                if need_c:
                    c["clean_v"] = ch
                done += 1
                log(f"made video {done}/{len(jobs)}: {c['streamer']} {cid}")
                if done % 4 == 0 and done < len(jobs):
                    write_queue()
                    publish_site()
            except Exception as e:  # noqa: BLE001
                log(f"render failed: {e}")

    try:
        cam_sheet([(cid, c, RAW / f"{cid}.mp4") for cid, c in clips.items()
                   if c["status"] == "new" and c.get("cam") and (RAW / f"{cid}.mp4").exists()], SITE / "camsheet.jpg")
    except Exception as e:  # noqa: BLE001
        log(f"cam sheet failed: {e}")

    # 4d. AI wrote new hooks -> re-make those videos now, in this same run
    redo = []
    for cid, c in clips.items():
        if c["status"] != "new" or not c.get("hook") or not c.get("cam"):
            continue
        hh = hashlib.sha1((c["hook"] + c["cam"] + LAYOUT_V + c.get("layout", "")).encode()).hexdigest()[:10]
        raw = RAW / f"{cid}.mp4"
        if c.get("final_hash") and c["final_hash"] != hh and raw.exists():
            redo.append((cid, raw, hh, c.get("clean_v"), True, False))
    if redo:
        log(f"re-making {len(redo[:8])} videos with the AI hooks")
        with ThreadPoolExecutor(max_workers=2) as pool:
            for fut in as_completed([pool.submit(make, j) for j in redo[:8]]):
                try:
                    cid, raw, hh, ch, need_f, need_c = fut.result()
                    clips[cid]["final_hash"] = clips[cid]["final_v"] = hh
                except Exception as e:  # noqa: BLE001
                    log(f"re-make failed: {e}")

    # 5. queue.json
    write_queue()
    save_state(st)
    _q = json.loads((SITE / "queue.json").read_text())
    log(f"queue: {len(_q['ready'])} ready ({sum(1 for r in _q['ready'] if r['final'])} with final video)")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)

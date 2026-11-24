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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import imageio_ffmpeg

import fetch_clips as fetch_clips_mod
import learn
LEARNED_INS: dict = {}
from fetch_clips import CFG, LIVE, download, helix, log, pick_clips, transcript, twitch_headers
from render_hook import render as render_hook
from render_hook import strip_emoji

ROOT = Path(__file__).resolve().parent
FF = imageio_ffmpeg.get_ffmpeg_exe()
# yt-dlp only recognises a binary literally named "ffmpeg" (imageio's is "ffmpeg-linux-x86_64-v…"), so give it one.
# Without this, partial downloads (YouTube VODs like YourRAGE's) failed with "ffmpeg is not installed".
FF_DIR = Path(tempfile.gettempdir()) / "gtaclip-ffmpeg"
try:
    FF_DIR.mkdir(exist_ok=True)
    if not (FF_DIR / "ffmpeg").exists():
        (FF_DIR / "ffmpeg").symlink_to(FF)
    FF_YTDLP = str(FF_DIR)
    os.environ["PATH"] = f"{FF_DIR}{os.pathsep}{os.environ.get('PATH', '')}"   # yt-dlp's section check looks on PATH
except Exception:  # noqa: BLE001
    FF_YTDLP = FF
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT / "_state"))
POSTED_DIR = Path(os.environ.get("POSTED_DIR", ROOT / "_posted"))
SITE = Path(os.environ.get("SITE_OUT", ROOT / "_site"))
VID = SITE / "v"
RAW = Path(os.environ.get("RAW_DIR", ROOT / "_raw"))  # cached raw downloads (actions cache)
PAGES = "https://inballin15.github.io/Gtaclip-cloud"
KEEP_UPLOAD_HOURS = 48   # clips made from your own uploads stay 2 days
NOCAM_MIN_VIEWS = 200   # no-webcam clips are only kept (full-screen gameplay) when 200+ people already watched them
KEEP_NEW_HOURS = 36      # clips stay postable for 36 h after they were clipped (Nov 23: quality over newest)
REPLACE_QUEUE_EACH_RUN = False   # (the old all-at-once swap stays off - see REPLACE_MIN_NEW)
REPLACE_MIN_NEW = 5   # once 5+ finished clips under REPLACE_AGE_H old are ready, older stream clips leave the queue
REPLACE_AGE_H = 36   # same as the keep limit: quality decides what posts, age only retires clips at 36 h
MAX_RENDERS = 12         # videos made per run (2 at a time); the rest next run
CAMSRC = Path("_camsrc")   # webcam sources waiting for the AI upscaler (branch camsrc)
CAMHD = Path("_camhd")     # AI-upscaled webcam strips (branch camhd)


def hd_key(cid: str, c: dict) -> str:
    """File name for a clip's webcam in the upscaler branches (a new suffix = a fresh upscale from a new source)."""
    return cid + c.get("hd_sfx", "")
SITE_MAX_MB = 650        # smaller site = GitHub Pages publishes faster (edits show up sooner); 1 GB is the hard limit
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


DEFAULT_TOPIC = {"name": "GTA 6", "game_ids": [CFG["game_id"]], "keywords": [], "tags": "#GTA6 #GTAVI #gaming"}


def _tags_for(name: str) -> str:
    w = re.sub(r"[^A-Za-z0-9]", "", name)
    return f"#{w} #gaming #streamer" if w else "#gaming #streamer"


def set_topic(st: dict, req: dict) -> str:
    """Change what the clip finder looks for. req: {game_id, name} (picked from trending) | {query} | {any: true}."""
    try:
        if req.get("reset") or (req.get("query") or "").strip().lower() in ("gta 6", "gta6", "gta vi", "grand theft auto vi"):
            st["topic"] = dict(DEFAULT_TOPIC)
        elif req.get("any"):
            st["topic"] = {"name": "any game", "game_ids": [], "keywords": [], "tags": "#gaming #streamer"}
        elif req.get("game_id"):
            nm = req.get("name") or req["game_id"]
            st["topic"] = {"name": nm, "game_ids": [str(req["game_id"])], "keywords": [], "tags": _tags_for(nm)}
        elif req.get("kind") == "keyword":
            q = (req.get("query") or "").strip()[:60]
            if not q:
                return "Nothing to search for."
            words = [w for w in re.findall(r"[a-z0-9']+", q.lower()) if len(w) >= 3] or [q.lower()]
            st["topic"] = {"name": q, "game_ids": [], "keywords": words, "query": q, "tags": _tags_for(q)}
        else:
            q = (req.get("query") or "").strip()[:60]
            if not q:
                return "Nothing to search for."
            cats = helix(twitch_headers(), "search/categories", query=q, first=5).get("data") or []
            norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())   # noqa: E731
            hit = next((c for c in cats if norm(c["name"]) == norm(q)), None) or \
                next((c for c in cats if norm(q) in norm(c["name"]) or norm(c["name"]) in norm(q)), None)
            if hit:
                st["topic"] = {"name": hit["name"], "game_ids": [hit["id"]], "keywords": [], "tags": _tags_for(hit["name"])}
            else:   # not a game: live channels talking about it (real-world news, events, anything)
                words = [w for w in re.findall(r"[a-z0-9']+", q.lower()) if len(w) >= 3] or [q.lower()]
                st["topic"] = {"name": q, "game_ids": [], "keywords": words, "query": q, "tags": _tags_for(q)}
        st["topic"]["set_at"] = iso(NOW)
        t = st["topic"]
        return (f"Now clipping: {t['name']}" + (" (Twitch category)" if t["game_ids"] else " (live streams about it)" if t["keywords"]
                                                  else " (any game your streamers play)") + ". New clips start with the next run.")
    except Exception as e:  # noqa: BLE001
        return f"Couldn't change the topic ({e})."


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
        tm = re.match(r"^\s*topic:\s*(.*)$", title, re.I)
        if tm:
            try:
                req = json.loads(body[body.index("{"):body.rindex("}") + 1]) if "{" in body else {"query": tm.group(1)}
            except Exception:  # noqa: BLE001
                req = {"query": tm.group(1)}
            msg = set_topic(st, req)
            log(f"topic: {msg}")
            _gh("POST", f"/repos/{REPO}/issues/{it['number']}/comments", {"body": msg})
            _gh("PATCH", f"/repos/{REPO}/issues/{it['number']}", {"state": "closed"})
            continue
        m = re.match(r"^\s*(skip|edit|layout|approve|unapprove):\s*(?:gtaclip/)?(?:tw_[a-z0-9_]+?_)?([A-Za-z0-9_\-]+)\s*$", title, re.I)
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
                c.pop("no_hook", None)
                c["hook_by_user"] = True
            if cm:
                c["caption"] = cm.group(1).strip()[:2000]
            msg = "Done — the video was re-made with your hook and caption."
        elif action == "layout":
            c = st["clips"][cid]
            try:
                ul = json.loads(body[body.index("{"):body.rindex("}") + 1])
                keep = {}
                if ul.get("mode") in ("split", "full", "irl"):
                    keep["mode"] = ul["mode"]
                if isinstance(ul.get("cam"), str) and re.match(r"^[\d.]+,[\d.]+,[\d.]+,[\d.]+$", ul["cam"]):
                    keep["cam"] = ul["cam"]
                for k, lo, hi in (("hook_y", 0.05, 0.95), ("zoom", 1.0, 2.5), ("pan", -1.0, 1.0), ("irl_x", 0.0, 1.0),
                                  ("hook_scale", 0.6, 1.6), ("speed", 1.0, 2.0), ("top_h", 420, 1100)):
                    if isinstance(ul.get(k), (int, float)):
                        keep[k] = max(lo, min(hi, float(ul[k])))
                if ul.get("hook_style") in ("white", "black", "outline"):
                    keep["hook_style"] = ul["hook_style"]
                for k in ("subs", "punch"):   # studio switches: word subtitles / punch-in zoom
                    if ul.get(k) in ("on", "off"):
                        keep[k] = ul[k]
                tr = ul.get("trim")
                if isinstance(tr, list) and len(tr) == 2 and all(isinstance(v, (int, float)) for v in tr) and tr[1] - tr[0] >= 3:
                    keep["trim"] = [max(0.0, float(tr[0])), float(tr[1])]
                if isinstance(ul.get("hook"), str) and ul["hook"].strip():
                    c["hook"] = strip_emoji(ul["hook"])[:140]
                    c.pop("no_hook", None)
                    c["hook_by_user"] = True
                if isinstance(ul.get("caption"), str):
                    c["caption"] = ul["caption"].strip()[:2000]
                c["user_layout"] = keep
                c.pop("final_hash", None); c.pop("clean_v", None)
                msg = "Done — the video is being re-made with your layout (next run, a few minutes)."
            except Exception as e:  # noqa: BLE001
                msg = f"Couldn't read that layout ({e})."
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
        tn = fetch_clips_mod.TOPIC.get("name") or "GTA 6"
        if tn.lower() not in hook.lower() and "gta" not in hook.lower():
            hook += f" in {tn}"
        return hook[:100], "title"
    return f"{name} can't believe this in {fetch_clips_mod.TOPIC.get('name') or 'GTA 6'}", "placeholder"


def clip_quality(c: dict) -> float:
    """Quality first (Nov 23): AI score + Twitch views (how many people already found it funny) + a small nudge for
    newer clips. Same idea as the poster's ranking, so the queue keeps the clips that will actually be posted."""
    import math
    v = c.get("viral")
    v = 55.0 if v is None else float(v)
    views = c.get("views") or 0
    vp = min(26.0, 6 * math.log10(1 + views)) if views else 12.0
    try:
        age = (NOW - datetime.fromisoformat((c.get("created") or c.get("found") or "").replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        age = 6.0
    return v + vp + (12 if age < 3 else 6 if age < 8 else 0) - 0.5 * age


def mix_order(ready: list) -> list:
    """Post-now first, then take turns: a paid clip (Clix/Lacy, rotating), then two from other streamers (rotating),
    never the same streamer twice in a row when it can be avoided. Best clips first within each streamer."""
    now = sorted([r for r in ready if r.get("post_now")], key=lambda r: r["post_now"])
    rest = sorted([r for r in ready if not r.get("post_now")], key=clip_quality, reverse=True)   # best first within each streamer
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
    # the old (Haar) detectors miss faces under headphones, durags, hats or at an angle — so webcams like
    # YourRAGE's and ohnePixel's were thrown out as "no webcam found". Add the modern AI detector's faces too.
    try:
        out += [(int(x), int(y), int(w), int(h)) for (x, y, w, h, sc) in _yunet_faces(frame, 0.7) if w >= 24]
    except Exception:  # noqa: BLE001
        pass
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


def has_audio(path: Path) -> bool:
    r = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return "Audio:" in r.stderr


# ---------------------------------------------------------------- rendering
def ff(*args) -> None:
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True)


def render_preview(raw: Path, cid: str) -> None:
    """Just the thumbnail; the site plays the final video (no second copy = half the storage)."""
    ff("-ss", "2", "-i", str(raw), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(VID / f"{cid}.jpg"))


CLIP_MAX_S = 22    # the top GTA 6 clip pages post 10-20 s videos (median 16 s); longer clips get trimmed
CLIP_KEEP_S = 16   # top clip pages' median length   # length of the trimmed part


def cut_dead_air(src: Path, cid: str, tmp: Path, min_gap: float = 0.8) -> Path:
    """Auto-Editor's method with its tested defaults: anything quieter than 4% of the clip's loudest sound is
    "dead air"; gaps of at least `min_gap` s are cut, keeping 0.2 s of padding on each side so cuts don't feel
    abrupt. Short pauses (timing, reactions) are left alone."""
    import viral
    try:
        a = viral._audio(src)
        if len(a) < 16000 * 4:
            return src
        win = 800   # 0.05 s
        lv = np.array([float(np.max(np.abs(a[i:i + win]))) for i in range(0, len(a) - win, win)])
        quiet = lv < 0.04 * (float(np.percentile(lv, 99)) or 1)
        dur = len(lv) * 0.05
        gaps, i = [], 0
        while i < len(quiet):
            if quiet[i]:
                j = i
                while j < len(quiet) and quiet[j]:
                    j += 1
                if (j - i) * 0.05 >= min_gap:
                    gaps.append((i * 0.05 + 0.2, j * 0.05 - 0.2))
                i = j
            else:
                i += 1
        if not gaps or sum(b - a for a, b in gaps) < 0.4:
            return src
        keep, t = [], 0.0
        for g0, g1 in gaps:
            if g0 > t:
                keep.append((t, g0))
            t = g1
        if t < dur:
            keep.append((t, dur))
        keep = [(a, b) for a, b in keep if b - a > 0.1]
        sel = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in keep)
        out = tmp / f"{cid}_tight.mp4"
        ff("-i", str(src), "-vf", f"select='{sel}',setpts=N/FRAME_RATE/TB", "-af", f"aselect='{sel}',asetpts=N/SR/TB",
           "-c:v", "libx264", "-preset", "fast", "-crf", "13", "-c:a", "aac", "-b:a", "160k", str(out))
        log(f"cut {sum(b - a for a, b in gaps):.1f}s of dead air from {cid} ({len(gaps)} gap(s))")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"dead-air trim skipped {cid}: {e}")
        return src


def best_part(raw: Path, cid: str, tmp: Path, c: dict) -> Path:
    """Pick the part of the clip to keep, with the big (loudest) moment placed where top pages put it.
    Clipper study (Nov 22, 128 clips timed by their audio): 100K+ view clips open on talk/action within the first
    second, build up, and hit the loudest moment around the MIDDLE (median ~12 s of ~25 s) - only 15% peak in the
    first 3 s. So by default ("build") the clip starts ~45% of its length before the moment, on someone starting to
    talk; the learner keeps testing the old "fast" start (~2 s before) against it."""
    import viral
    try:
        a = viral._audio(raw)
        secs = len(a) // 16000
        sty = c.get("style") or {}
        keep_s = 16 if sty.get("len") in ("s16", "short") else 22
        if secs < 6:
            return raw
        rms = np.array([np.sqrt(np.mean(a[i * 16000:(i + 1) * 16000] ** 2) + 1e-9) for i in range(secs)])
        db = 20 * np.log10(rms + 1e-9)
        sm = np.convolve(db, np.ones(3) / 3, mode="same")
        p = int(np.argmax(sm[1:-1])) + 1
        if sty.get("start") == "fast":
            start = float(max(0, p - 2))
        else:
            want = p - round(keep_s * 0.45)
            med = float(np.median(db))
            # start on someone talking: the second (near the target) where the sound rises, and isn't silence
            cand = [t for t in range(max(1, want - 3), max(1, min(p - 3, want + 3) + 1)) if db[t] >= med - 3]
            start = float(max(cand, key=lambda t: (db[t] - db[t - 1]) - 0.4 * abs(t - want)) if cand else max(0, want))
            start = max(0.0, start)
        end = float(min(secs, max(start + keep_s, p + 4)))
        if end - start < 8:                       # moment near the end: keep at least 8 s
            start = float(max(0, end - 8))
        if end - start > 30:
            end = start + 30
        if secs - end < 3:                        # don't leave a tiny leftover at the end
            end = float(secs)
        if start < 1.5 and secs - end < 3:        # the whole clip already is the right part
            return raw
        out = tmp / f"{cid}_best.mp4"
        if not out.exists():
            viral.cut(raw, start, end, out)
        c["trim"] = [start, end]
        log(f"trimmed {cid}: {secs}s -> {start:.0f}-{end:.0f}s (moment at {p}s, {sty.get('start', 'build')} start)")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"trim skipped {cid}: {e}")
        return raw


_YUNET = None


def _yunet_faces(img, min_score=0.75):
    """Modern AI face detector (OpenCV YuNet). Returns [(x, y, w, h, score)] in pixels."""
    global _YUNET
    if _YUNET is None:
        _YUNET = cv2.FaceDetectorYN.create(str(Path(__file__).parent / "models" / "face_yunet.onnx"), "", (320, 320),
                                           min_score, 0.3, 5000)
    h, w = img.shape[:2]
    _YUNET.setInputSize((w, h))
    _, f = _YUNET.detect(img)
    return [] if f is None else [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[-1])) for r in f]


def irl_focus(path: Path, cam: str) -> float | None:
    """No gameplay on screen (streamer on a room camera / watching another stream): a real person's face holds
    still in the big picture outside the webcam box. Returns where to centre a full-screen shot (0-1 across), else None."""
    cap = cv2.VideoCapture(str(path))
    dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
    cap.release()
    frames = _frames(path, [max(0.5, dur * p) for p in (0.06, 0.2, 0.35, 0.5, 0.65, 0.8, 0.94)])
    if not frames:
        return None
    H, W = frames[0].shape[:2]
    try:
        cx0, cy0, cw, chh = [float(v) for v in cam.split(",")]
    except Exception:  # noqa: BLE001
        cx0 = cy0 = cw = chh = 0.0
    dets = []
    for f in frames:
        out = []
        for (x, y, w, h, sc) in _yunet_faces(f, 0.8):
            mx, my = (x + w / 2) / W, (y + h / 2) / H
            if cx0 - 0.02 <= mx <= cx0 + cw + 0.02 and cy0 - 0.02 <= my <= cy0 + chh + 0.02:
                continue                       # that's the webcam overlay itself
            if w >= 0.045 * W:                 # a real-size person in the main picture
                out.append((x, y, w, h))
        dets.append(out)
    best, best_n = None, 0
    for fl in dets:
        for (x, y, w, h) in fl:
            cx, cy = x + w / 2, y + h / 2
            hits = [m[0] for m in ([g for g in fl2 if abs(g[0] + g[2] / 2 - cx) < 0.06 * W and abs(g[1] + g[3] / 2 - cy) < 0.08 * H]
                                   for fl2 in dets) if m]
            if len(hits) > best_n or (len(hits) == best_n and best and w > best[0][2]):
                best_n, best = len(hits), hits
    if best_n < 5:                             # game cutscene faces rarely hold the same spot this long
        return None
    return float(np.median([g[0] + g[2] / 2 for g in best])) / W


def face_cam(path: Path, box: str) -> str | None:
    """Frame the webcam strip around the streamer's real face.
    The streamer's face is the one that stays in the same spot through the clip (NPC faces in the game move).
    Returns 'x,y,w,h' (fractions) shaped like the top strip, or None when nobody is on camera."""
    cap = cv2.VideoCapture(str(path))
    dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
    cap.release()
    frames = _frames(path, [max(0.5, dur * p) for p in (0.06, 0.2, 0.35, 0.5, 0.65, 0.8, 0.94)])
    if not frames:
        return None
    H, W = frames[0].shape[:2]
    dets = [_yunet_faces(f) for f in frames]
    # group faces that sit in the same place with a similar size across moments
    best, best_n = None, 0
    for fi, fl in enumerate(dets):
        for (x, y, w, h, sc) in fl:
            cx, cy = x + w / 2, y + h / 2
            hits = []
            for fl2 in dets:
                m = [f for f in fl2 if abs(f[0] + f[2] / 2 - cx) < 0.05 * W and abs(f[1] + f[3] / 2 - cy) < 0.07 * H
                     and 0.65 < f[2] / w < 1.5]
                if m:
                    hits.append(m[0])
            if len(hits) > best_n:
                best_n, best = len(hits), hits
    if best_n < 4:          # nobody steady on camera (away / camera off / only game characters)
        return None
    fx = float(np.median([f[0] + f[2] / 2 for f in best]))
    fy = float(np.median([f[1] + f[3] / 2 for f in best]))
    fw = float(np.median([f[2] for f in best]))
    A = 1080 / TOP_H        # strip shape (wide)
    # the webcam overlay we found earlier limits the frame, if the face is inside it
    bx0, by0, bx1, by1 = 0.0, 0.0, float(W), float(H)
    try:
        x, y, w, h = [float(v) for v in box.split(",")]
        if x * W <= fx <= (x + w) * W and y * H <= fy <= (y + h) * H:
            bx0, by0, bx1, by1 = x * W, y * H, (x + w) * W, (y + h) * H
    except Exception:  # noqa: BLE001
        pass
    if (bx1 - bx0) < W * 0.95:                         # webcam overlay known: show ALL of it (least zoom = sharpest)
        ww = min(bx1 - bx0, (by1 - by0) * A)
        ww = max(ww, min(fw * 3.0, bx1 - bx0))
    else:                                              # no overlay found: head, shoulders and some room
        ww = fw * 5.0
    ww = min(ww, W)
    hh = ww / A
    x0 = min(max(fx - ww / 2, bx0), bx1 - ww)
    y0 = min(max(fy - hh * 0.36, by0), by1 - hh)       # face high enough that the hook box never covers it
    x0, y0 = max(0.0, x0), max(0.0, y0)
    return f"{x0 / W:.4f},{y0 / H:.4f},{min(ww, W - x0) / W:.4f},{min(hh, H - y0) / H:.4f}"


def cam_has_face(path: Path, cam: str) -> bool:
    """Someone is actually in the webcam box in at least 2 of 5 moments (not an empty chair / wall / BRB screen)."""
    try:
        x, y, w, h = [float(v) for v in cam.split(",")]
        cap = cv2.VideoCapture(str(path))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        hits = 0
        for fr in _frames(path, [max(0.5, dur * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]):
            H, W = fr.shape[:2]
            crop = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
            if crop.size:
                big = cv2.resize(crop, None, fx=2, fy=2)
                if [f for f in _faces(big, neighbors=4) if f[2] >= 0.1 * big.shape[1]]:
                    hits += 1
        return hits >= 2
    except Exception:  # noqa: BLE001
        return True   # can't tell -> don't throw the clip away


def cam_without_chat(path: Path, cam: str, chat_layout: bool = False, known: dict | None = None) -> str:
    """cam_without_chat_known + a last check for chat_on_cam streamers: chat over a bright game shows no dark panel
    and no known box matches, but a webcam is 16:9 (box height == width on a 16:9 stream). A box clearly taller
    than that is webcam + chat strip -> end it at the webcam's 16:9 bottom, never through the face."""
    out = cam_without_chat_known(path, cam, chat_layout, known)
    if out != cam or not chat_layout:
        return out
    try:
        x, y, w, h = [float(v) for v in cam.split(",")]
        if h <= w * 1.15:
            return cam
        frac = (w / h) * 0.98
        cap = cv2.VideoCapture(str(path))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        need = 0.0
        for fr in _frames(path, [max(0.5, dur * p) for p in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9)]):
            H, W = fr.shape[:2]
            c = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
            fs = _yunet_faces(cv2.resize(c, None, fx=2, fy=2), 0.6)
            if fs:
                f = max(fs, key=lambda f: f[4])
                need = max(need, (f[1] + f[3] * 1.12) / (c.shape[0] * 2))
        if need > frac + 0.02:
            return cam
        return f"{x:.4f},{y:.4f},{w:.4f},{h * max(0.5, frac, need):.4f}"
    except Exception:  # noqa: BLE001
        return cam


def cam_without_chat_known(path: Path, cam: str, chat_layout: bool = False, known: dict | None = None) -> str:
    """Some streamers (Kai) have chat scrolling over the bottom of their webcam. Find where that text band starts
    and end the webcam box above it, so no chat shows between the face and the gameplay.
    known = {box: "x,y,w,h", at: 0.59}: where the chat panel starts in that streamer's usual webcam box; used when
    the panel can't be seen (chat over a bright game menu), never through the face."""
    out = _cam_without_chat(path, cam, chat_layout)
    if out != cam or not (chat_layout and known and known.get("box") and known.get("at")):
        return out
    try:
        kb = [float(v) for v in str(known["box"]).split(",")]
        x, y, w, h = [float(v) for v in cam.split(",")]
        # same kind of box (webcam + chat panel under it) - his layout moves around a little, so match the SIZE,
        # not the exact spot (Nov 24: a 0.198 x 0.314 box at y 0.42 kept his chat in the crop)
        if abs(w - kb[2]) > 0.03 or abs(h - kb[3]) > 0.045:
            return cam
        cap = cv2.VideoCapture(str(path))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        need = 0.0
        for fr in _frames(path, [max(0.5, dur * p) for p in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9)]):
            H, W = fr.shape[:2]
            c = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
            fs = _yunet_faces(cv2.resize(c, None, fx=2, fy=2), 0.6)
            if fs:
                f = max(fs, key=lambda f: f[4])
                need = max(need, (f[1] + f[3] * 1.12) / (c.shape[0] * 2))
        at = float(known["at"])
        if need > at + 0.04:
            return cam
        return f"{x:.4f},{y:.4f},{w:.4f},{h * max(0.5, at, need):.4f}"
    except Exception:  # noqa: BLE001
        return cam


def _cam_without_chat(path: Path, cam: str, chat_layout: bool = False) -> str:
    try:
        x, y, w, h = [float(v) for v in cam.split(",")]
        cap = cv2.VideoCapture(str(path))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        dens, lums, meds = [], [], []
        for fr in _frames(path, [max(0.5, dur * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]):
            H, W = fr.shape[:2]
            c = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
            if c.size:
                g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
                e = cv2.Canny(g, 80, 200)
                dens.append(e.mean(axis=1) / 255)
                lums.append(g.mean(axis=1))
                meds.append(np.median(g, axis=1))
        if len(dens) < 3:
            return cam
        n = min(len(d) for d in dens)
        if chat_layout:
            # chat sits on a dark panel over the bottom of the webcam: find where the rows turn dark and stay dark.
            # Look only below the streamer's chin (a dark shirt/hoodie must not count as the panel) and use each
            # row's median brightness (chat letters are a few bright pixels on the dark panel)
            need = 0.0
            for fr in _frames(path, [max(0.5, dur * p) for p in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9)]):
                H, W = fr.shape[:2]
                c = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
                fs = _yunet_faces(cv2.resize(c, None, fx=2, fy=2), 0.6)
                if fs:
                    f = max(fs, key=lambda f: f[4])
                    need = max(need, (f[1] + f[3] * 1.12) / (c.shape[0] * 2))
            L = np.convolve(np.mean([m[:n] for m in meds], axis=0), np.ones(5) / 5, mode="same")
            k2 = max(3, int(n * 0.05))
            for r in range(int(n * max(0.45, need - 0.02)), int(n * 0.92)):
                above, below, rest = L[max(0, r - 3 * k2):r].mean(), L[r:r + k2].mean(), L[r:n - 2].mean()
                if below < 0.78 * above and rest < 0.62 * L[:r].mean():
                    lums = r / n
                    break
            else:
                lums = None
            if lums and need <= lums + 0.02:
                frac = max(0.5, need, lums - 0.06)   # a little above the panel: its first chat line sits on the edge
                return f"{x:.4f},{y:.4f},{w:.4f},{h * frac:.4f}"
            if lums:
                return cam
        d = np.convolve(np.mean([d[:n] for d in dens], axis=0), np.ones(5) / 5, mode="same")
        base = float(np.median(d[int(n * 0.3):int(n * 0.55)]))
        k = max(3, int(n * 0.04))
        # streamers known to run chat over their webcam (Kai): sensitive check (chat text on sharp 1080p footage
        # is only ~0.02-0.05), but the band has to run all the way down to the bottom of the box
        thr = max(3 * base, 0.018) if chat_layout else max(2.2 * base, 0.06)
        for r in range(int(n * 0.55), n - k):
            if d[r:r + k].mean() > thr and (not chat_layout or (d[r:] > thr * 0.5).mean() > 0.5):
                while r > n * 0.5 and d[r - 1] > 1.4 * base:   # walk up to where the chat really starts
                    r -= 1
                start = r / n
                frac = max(0.5, start - 0.08)
                need = 0.0      # lowest chin seen: the cut may move down to just below it, never through the face
                for fr in _frames(path, [max(0.5, dur * p) for p in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9)]):
                    H, W = fr.shape[:2]
                    c = fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
                    big = cv2.resize(c, None, fx=2, fy=2)
                    fs = _yunet_faces(big, 0.6)
                    if fs:   # only the streamer's own face (the surest one) — chat emotes can look like tiny faces
                        fx_, fy_, fw_, fh_, _sc = max(fs, key=lambda f: f[4])
                        need = max(need, (fy_ + fh_ * 1.12) / big.shape[0])
                if need > start + 0.02:      # the face itself reaches into the chat band: leave it alone
                    return cam
                frac = max(frac, need)
                return f"{x:.4f},{y:.4f},{w:.4f},{h * frac:.4f}"
        return cam
    except Exception:  # noqa: BLE001
        return cam


def _caption_line_top(fr, cam: str) -> float | None:
    """Burned-in caption text (Twitch live captions, GTA's own subtitles) = bold white letters with a black outline,
    low in the frame. Returns where that text line starts (fraction of the frame height) or None."""
    H, W = fr.shape[:2]
    y0, x0, x1 = int(H * 0.6), int(W * 0.08), int(W * 0.92)
    hsv = cv2.cvtColor(fr[y0:, x0:x1], cv2.COLOR_BGR2HSV)
    white = cv2.inRange(hsv, (0, 0, 185), (180, 60, 255))     # white / light-grey letters
    black = cv2.inRange(hsv, (0, 0, 0), (180, 255, 45))
    edge = cv2.bitwise_and(white, cv2.dilate(black, np.ones((5, 5), np.uint8)))   # letters touching a black outline
    if cam:   # never count the webcam overlay
        cx, cy, cw, ch = [float(v) for v in cam.split(",")]
        a0, a1 = max(0, int(cy * H) - y0), max(0, int((cy + ch) * H) - y0)
        b0, b1 = max(0, int(cx * W) - x0), max(0, int((cx + cw) * W) - x0)
        edge[a0:a1, b0:b1] = 0
    r = np.convolve(edge.mean(axis=1) / 255, np.ones(5) / 5, mode="same")
    rows = np.where(r > 0.012)[0]
    return (rows[0] + y0) / H if len(rows) >= 6 else None


def gameplay_is_black(path: Path, cam: str) -> bool:
    """True when the stream shows no game: everything outside the webcam (and chat corner) is basically black,
    e.g. between games, capture off, BRB screen. A split clip of that would have a black bottom half."""
    try:
        cap = cv2.VideoCapture(str(path))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        dark = 0
        frames = _frames(path, [max(0.3, dur * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)])
        for fr in frames:
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
            H, W = g.shape
            mask = np.ones_like(g, dtype=bool)
            if cam:
                x, y, w, h = [float(v) for v in cam.split(",")]
                mask[max(0, int((y - 0.05) * H)):min(H, int((y + h + 0.35) * H)), max(0, int((x - 0.02) * W)):min(W, int((x + w + 0.05) * W))] = False
            px = g[mask]
            if px.size and (px < 28).mean() > 0.9:      # 90%+ of the game area is near-black
                dark += 1
        return len(frames) >= 3 and dark >= max(3, len(frames) - 1)
    except Exception:  # noqa: BLE001
        return False


def purple_caption_cut(path: Path, duration: float, cam: str) -> float:
    """Captions burned into the stream (Twitch live captions, GTA subtitles) near the bottom: return the smallest
    bottom crop (fraction) that removes them, or 0 if the clip has none. Only the caption line and a hair more."""
    try:
        if not duration:
            cap = cv2.VideoCapture(str(path))
            duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
            cap.release()
        frames = _frames(path, [max(0.3, duration * (i + 0.5) / 12) for i in range(12)])   # captions come and go
        tops = [t for t in (_caption_line_top(f, cam) for f in frames) if t is not None and t >= 0.8]
        if len(tops) < 3:
            return 0.0
        top = float(np.percentile(tops, 15))   # the highest the caption line usually reaches
        return round(min(0.2, 1 - top + 0.012), 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _cam_fit(raw: Path, w: float, h: float, T: int, max_up: float) -> tuple[int, int]:
    """Size of the whole webcam box inside the 1080 x T strip: as large as fits, but never more than max_up x its real
    pixel size (stretching further only adds blur). Even numbers for the encoder."""
    cap = cv2.VideoCapture(str(raw))
    W, H = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920, cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080
    cap.release()
    bw, bh = max(2.0, w * W), max(2.0, h * H)
    k = min(1080 / bw, T / bh, max_up)
    return int(bw * k) // 2 * 2, int(bh * k) // 2 * 2


def _cam_fg_top(raw: Path, w: float, h: float) -> int:
    """Top edge of the full-width webcam in the cam-only layout (it sits centred on the 1080x1920 frame)."""
    cap = cv2.VideoCapture(str(raw))
    W, H = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920, cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080
    cap.release()
    fg_h = min(1920, 1080 * (h * H) / max(1, w * W))
    return int((1920 - fg_h) / 2)


def render_final(raw: Path, cid: str, cam: str, hook: str | None, tmp: Path, out: str = "_final",
                 layout: str = "split", cut: float = 0.0, cam_video: Path | None = None,
                 hook_y: float | None = None, zoom: float = 1.0, pan: float = 0.0,
                 hook_style: str = "white", hook_scale: float = 1.0, top_h: int | None = None,
                 subs: list | None = None, punch_at: float | None = None) -> None:
    """Split screen (webcam top, gameplay bottom). hook=None makes the clean copy with no caption box.
    layout="full": no webcam in the video -> the whole frame centred on a blurred background."""
    T = int(top_h or TOP_H)   # webcam strip height (editor can make the webcam bigger or smaller)
    x, y, w, h = [float(v) for v in cam.split(",")]
    if layout.startswith("irl"):
        fx = float(layout.split(":")[1]) if ":" in layout else 0.5
        base = (f"[0:v]crop=w=ih*9/16/{zoom:.3f}:h=ih/{zoom:.3f}:x='min(max(iw*{fx}-ow/2,0),iw-ow)':y='(ih-oh)/2',"
                f"scale=1080:1920:flags=lanczos,unsharp=3:3:0.4:3:3:0.0,setsar=1")
    elif layout == "cam":
        src = (f"[{2 if hook else 1}:v]setsar=1" if cam_video else
               f"[0:v]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},hqdn3d=1.5:1.5:3:3,setsar=1")
        base = (src + ",split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "boxblur=30:2,eq=brightness=-0.12,setsar=1[bg];[b]scale=1080:-2:flags=lanczos,"
                + ("" if cam_video else "unsharp=5:5:0.8:5:5:0.0,") +
                "setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    elif layout == "fill":   # your clips with no webcam: the gameplay itself fills the 9:16 screen (slide = crop spot)
        base = (f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:1920:x='(iw-ow)/2*(1+{pan:.3f})':y='(ih-oh)/2',unsharp=3:3:0.4:3:3:0.0,setsar=1")
    elif layout == "full":
        base = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "boxblur=20:2,setsar=1[bg];[b]scale=1080:-2,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        # webcam: the WHOLE webcam box (nothing cropped off), only as big as its pixels allow so it stays sharp
        # (max ~3.6x with the AI-upscaled webcam, 2.8x without), centred on a blurred copy of itself
        fw, fh = _cam_fit(raw, w, h, T, 3.6 if cam_video else 2.8)
        punch = ""
        if punch_at is not None:   # "punch into the reaction": webcam zooms in 10% on the big moment, then back
            a, b = punch_at - 0.15, punch_at + 2.0
            punch = (f",fps=30,zoompan=z='1+0.10*max(0,min(1,min((it-{a:.2f})/0.2,({b:.2f}-it)/0.4)))'"
                     f":x='iw/2-(iw/zoom/2)':y='ih/2.4-(ih/zoom/2.4)':d=1:s={fw}x{fh}:fps=30")
        base = (
            f"[0:v]split=2[a][b];"
            f"[a]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},hqdn3d=1.2:1.2:3:3,setsar=1,split=2[ca][cb];"
            f"[ca]scale=1080:{T}:force_original_aspect_ratio=increase,crop=1080:{T},boxblur=24:2,eq=brightness=-0.14,setsar=1[cbg];"
            f"[cb]scale={fw}:{fh}:flags=lanczos,unsharp=5:5:0.7:5:5:0.0,setsar=1{punch}[cfg];"
            f"[cbg][cfg]overlay=(W-w)/2:(H-h)/2[top];"
            + (f"[b]split=2[b1][b2];[b2]crop=iw:ih*0.15:0:ih*{-cut},boxblur=luma_radius=40:luma_power=3:chroma_radius=18:chroma_power=3[bl];"
               f"[b1][bl]overlay=0:main_h*{-cut}[bb];" if cut < 0 else "") +
            f"[{'bb' if cut < 0 else 'b'}]crop=iw*{1 - max(CAPTION_CUT, cut, 0)}:ih*{1 - max(CAPTION_CUT, cut, 0)}:(iw-ow)/2:0,"
            + (f"crop=iw/{zoom:.3f}:ih/{zoom:.3f}:(iw-ow)/2*(1+{pan:.3f}):(ih-oh)/2," if zoom > 1.001 else "") +
            f"scale=-2:{1920 - T}:flags=lanczos,crop=1080:{1920 - T}:x='(iw-ow)/2*(1+{pan:.3f})':y=0,"
            f"unsharp=3:3:0.4:3:3:0.0,setsar=1[bot];"
            f"[top][bot]vstack=inputs=2"
        )
    extra = ["-i", str(cam_video)] if (cam_video and layout == "cam") else []
    if cam_video and layout == "split":   # AI-upscaled webcam (made on its own server) replaces the plain one
        ci = 2 if hook else 1
        a_end = base.index("[top];") + len("[top];")
        base = (f"[0:v]copy[b];[{ci}:v]setsar=1,split=2[ca][cb];"
                f"[ca]scale=1080:{T}:force_original_aspect_ratio=increase,crop=1080:{T},boxblur=24:2,eq=brightness=-0.14,setsar=1[cbg];"
                f"[cb]scale={fw}:{fh}:flags=lanczos,setsar=1{punch}[cfg];[cbg][cfg]overlay=(W-w)/2:(H-h)/2[top];" + base[a_end:])
        extra = ["-i", str(cam_video)]
    if hook:
        png = render_hook(hook, str(tmp / f"{cid}_hook.png"), hook_style, hook_scale)
        inputs = ["-i", str(raw), "-i", png] + extra
        oy = (f"H*{hook_y:.3f}-h/2" if hook_y is not None                              # your own spot (editor)
              else "(H-h)/2" if layout == "full" else "H*0.2-h/2" if layout == "fill" else "H*0.6" if layout.startswith("irl")   # irl: under the faces
              else f"'max(150,{_cam_fg_top(raw, w, h)}-h-28)'" if layout == "cam"           # cam: just above the webcam
              else f"{T}-h*0.2")                                                   # split: on the line
    else:
        inputs = ["-i", str(raw)] + extra
    flt = base + "[v0]"
    last = "v0"
    # word-by-word subtitles (WhisperX-style): 1-3 words at a time, big white outlined letters
    if subs:
        from render_hook import render_sub, sub_chunks
        sy = (f"{T}+({1920 - T})*0.62-h/2" if layout == "split" else "H*0.74-h/2")
        for i, (a, b, txt) in enumerate(sub_chunks(subs)[:60]):
            k = len(inputs) // 2
            inputs += ["-i", render_sub(txt, str(tmp / f"{cid}_sub{i}.png"))]
            flt += f";[{last}][{k}:v]overlay=(W-w)/2:{sy}:enable='between(t,{a:.2f},{b:.2f})'[s{i}]"
            last = f"s{i}"
    if hook:
        flt += f";[{last}][1:v]overlay=(W-w)/2:{oy}[vh]"
        last = "vh"
    flt += f";[{last}]format=yuv420p[out]"
    # clean, even sound (FFmpeg loudnorm to TikTok/YouTube's -14 LUFS): no more too-quiet or blaring clips
    ff(*inputs, "-filter_complex", flt, "-map", "[out]", "-map", "0:a?",
       "-c:v", "libx264", "-preset", "medium", *enc_args(out), "-r", "30",
       *(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000"] if has_audio(raw) else []),
       "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(VID / f"{cid}{out}.mp4"))


def enc_args(out: str) -> list:
    """Video quality for saved clips. Posted copy: CRF 20, max 6 Mbps - TikTok re-compresses every upload to ~2-3 Mbps,
    so this looks the same on a phone (the old CRF 17 / 12 Mbps made ~27 MB files and the site only fit ~10 clips).
    The no-caption copy is only a working copy for edits / saving without the hook: a bit lighter."""
    if out == "_clean":
        return ["-crf", "22", "-maxrate", "4500k", "-bufsize", "9M"]
    return ["-crf", "20", "-maxrate", "6000k", "-bufsize", "12M"]   # tested Nov 24 on a Kai clip: 26 -> 17 MB, looks identical


def shrink_site_videos(st: dict) -> None:
    """One time (Nov 24): re-save the videos already on the site at the new sizes - same look, about half the space."""
    if st.get("shrink_v", 0) >= 1:
        return
    done, saved = 0, 0.0
    for f in sorted(VID.rglob("*.mp4")):
        try:
            mb = f.stat().st_size / 1e6
            secs = max(1.0, duration_of(f))
            if mb / secs < 0.8:   # already light (about 6 Mbps or less)
                continue
            out = f.with_suffix(".shrink.mp4")
            ff("-i", str(f), "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
               *enc_args("_clean" if f.stem.endswith("_clean") else "_final"), "-c:a", "copy", "-movflags", "+faststart", str(out))
            if out.exists() and out.stat().st_size < f.stat().st_size:
                saved += mb - out.stat().st_size / 1e6
                out.replace(f); done += 1
            else:
                out.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            log(f"shrink skipped {f.name}: {e}")
    st["shrink_v"] = 1
    log(f"re-saved {done} site videos smaller: {saved:.0f} MB freed")
    for cid, c in st.get("clips", {}).items():   # real length of clips made before final_s existed
        f = VID / f"{cid}_final.mp4"
        if c.get("status") == "new" and not c.get("final_s") and f.exists():
            try:
                c["final_s"] = round(duration_of(f))
            except Exception:  # noqa: BLE001
                pass


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
            "noprogress": True, "noplaylist": True, "ffmpeg_location": FF_YTDLP}
    if a is not None:
        opts["download_ranges"] = download_range_func(None, [(a, b if b else a + 4 * 3600)])
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
        f = Path(y.prepare_filename(info))
    if not f.exists():
        f = next(tmp.glob("linkaudio.*"))
    log(f"link audio: {info.get('title')} ({int(info.get('duration') or 0) // 60} min){f' section {section}' if section else ''}")
    return f, float(a or 0), (info.get("title") or "Link")[:60]


def fetch_link_section(url: str, start: float, end: float, raw: Path, tmp: Path) -> int:
    """Download only the video section of a chosen moment at full source quality.
    (Before: yt-dlp's section download re-saved the 1080p60 VOD as old low-bitrate MPEG-4, which made every
    VOD clip blocky. Now ffmpeg reads the stream itself and saves a high-quality H.264 copy.)"""
    import yt_dlp
    info = {}
    for attempt in range(3):     # Twitch sometimes leaves the 1080p "Source" out for a live stream: ask again
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "noplaylist": True,
                               "format": "best[vcodec!=none]/bestvideo+bestaudio/best",   # best offered (1440p too)
                               "format_sort": ["res", "fps", "tbr"]}) as y:
            info = y.extract_info(url, download=False)
        hh = info.get("height") or max([f.get("height") or 0 for f in info.get("requested_formats") or []] or [0])
        if hh >= 1080 or "twitch.tv" not in url:
            break
        log(f"[download] only {hh}p offered this time, asking Twitch again ({attempt + 1}/3)")
        time.sleep(6)
    fmts = info.get("requested_formats") or [info]
    args = []
    for f in fmts:
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        args += (["-headers", hdr] if hdr else []) + ["-ss", f"{max(0.0, start - 1):.2f}", "-i", f["url"]]
    maps = ["-map", "0:v:0", "-map", ("1:a:0" if len(fmts) > 1 else "0:a:0?")]
    ff(*args, *maps, "-t", f"{(end - start) + 2:.2f}", "-c:v", "libx264", "-preset", "fast", "-crf", "13",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(raw))
    h = info.get("height") or (fmts[0].get("height") if fmts else None)
    log(f"[download] VOD section {info.get('format_id')} {info.get('width')}x{h} -> H.264 full quality")
    return int(h or 0)


# ---------------------------------------------------------------- your own uploads -> clips
PART_MIN, PART_MAX_S = 25.0, 30.0   # your pasted links/uploads: 25-30 s parts, each ending on a pause
PART_MAX = 32                        # at most 32 parts (about 15 minutes) per link


FULL_MAX_S = 180.0   # one full video per link, up to 3 minutes (longest YouTube Short)


def full_length(src: Path) -> float:
    """True length, also for audio-only downloads (links), where the frame count would be wrong."""
    try:
        out = subprocess.run([FF, "-i", str(src)], capture_output=True, text=True).stderr
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        pass
    return duration_of(src)


def split_parts(src: Path) -> tuple[list[tuple[float, float]], int]:
    """Cut a whole video into parts in order: each 25-30 s, ending at the quietest moment in that window (a pause,
    not mid-word). A last bit under 10 s joins the part before. Returns (spans, minutes covered if cut short)."""
    import numpy as np
    d0 = duration_of(src)
    try:
        pcm = subprocess.run([FF, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                             capture_output=True, check=True, timeout=600).stdout
        x = np.frombuffer(pcm, np.int16).astype(np.float32)
        win = 400   # 50 ms
        rms = np.sqrt(np.convolve((x[: len(x) // win * win].reshape(-1, win) ** 2).mean(1), np.ones(4) / 4, "same"))
        if len(x) > 8000:
            d0 = len(x) / 8000.0   # true length (links download audio only; the frame count would be wrong)
    except Exception:  # noqa: BLE001
        rms = None
    spans, a = [], 0.0
    while d0 - a > 0.5 and len(spans) < PART_MAX:
        if d0 - a - PART_MIN < 10:      # what's left can't leave a real part behind: it all goes in this one
            spans.append((round(a, 2), round(d0, 2))); a = d0; break
        lo, hi = a + PART_MIN, min(a + PART_MAX_S, d0 - 10)   # never leave a scrap under 10 s at the end
        b = hi
        if rms is not None and len(rms):
            i0, i1 = int(lo * 20), min(len(rms), int(hi * 20) + 1)
            if i1 > i0:
                b = (i0 + int(np.argmin(rms[i0:i1]))) / 20.0
        b = min(max(b, a + PART_MIN), d0)
        spans.append((round(a, 2), round(b, 2))); a = b
    return spans, (int(a // 60) if d0 - a > 0.5 else 0)


def refetch_upload(c: dict, raw: Path, tmp: Path) -> None:
    """Get one of your pasted clips again (to re-make it after an edit): same link, same start and end."""
    link = c.get("src_link") or ""
    if not link:
        raise RuntimeError("no link saved (uploaded file)")
    a, b = float(c["moment"]["start"]), float(c["moment"]["end"])
    if re.search(r"tiktok\.com/", link):
        import yt_dlp
        d = Path(tempfile.mkdtemp(dir=tmp))
        with yt_dlp.YoutubeDL({"format": "bv*+ba/b", "outtmpl": str(d / "tt.%(ext)s"), "quiet": True, "noplaylist": True,
                               "merge_output_format": "mp4", "ffmpeg_location": FF_YTDLP}) as y:
            y.extract_info(link, download=True)
        import viral
        viral.cut(next(d.glob("tt.*")), a, b, raw)
    else:
        fetch_link_section(link, a, b, raw, tmp)
    log(f"got your clip again: {raw.stem}")


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
        _gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": "Got your clip — adding it to the queue (a few minutes)." if job.get("asis")
                                                              else "Got your video — making it into one full video (usually 5–15 min)."})
        src = tmp / f"upload_{job.get('id', num)}.mp4"
        link = job.get("url") or ""
        offset = 0.0
        try:
            import viral
            tiktok = bool(re.search(r"tiktok\.com/", link))
            if tiktok:   # TikTok links: grab the whole (short) video once, then cut it here like an upload
                import yt_dlp
                with yt_dlp.YoutubeDL({"format": "bv*+ba/b", "outtmpl": str(tmp / "tiktok.%(ext)s"), "quiet": True,
                                       "noplaylist": True, "merge_output_format": "mp4", "ffmpeg_location": FF_YTDLP}) as y:
                    info = y.extract_info(link, download=True)
                src = next(tmp.glob("tiktok.*"))
                job.setdefault("name", (info.get("title") or info.get("description") or "TikTok")[:60])
                if not job.get("name") or job["name"] == "Link":
                    job["name"] = (info.get("title") or "TikTok")[:60]
                link = ""   # cut from the downloaded file, not the (short-lived) TikTok stream
                if duration_of(src) <= 75:   # already a short: use the whole thing, re-captioned in our style
                    job["asis"] = True
            elif link:
                src, offset, title = fetch_link_audio(link, job.get("section"), tmp)
                job.setdefault("name", title)
                if not job.get("name") or job["name"] == "Link":
                    job["name"] = title
            else:
                subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", f"{ref}:refs/remotes/origin/{ref}"], check=True)
                with open(src, "wb") as fh:
                    for part in parts:
                        fh.write(subprocess.run(["git", "show", f"origin/{ref}:{part}"], capture_output=True, check=True).stdout)
            if job.get("asis"):   # a finished clip (e.g. from Higgsfield's YouTube clipper): use the whole thing
                d0 = duration_of(src)
                moments = [{"start": 0.0, "end": d0, "score": 1.0, "reasons": ["finished clip"],
                            "text": transcript(src) if d0 < 180 else "", "hook": (job.get("hook") or "").strip()}]
                if moments[0]["hook"]:
                    moments[0]["hook_ai"] = True
            else:   # your pasted link / upload: ONE full video, start to finish (no parts - posting many back to back looks spammy)
                d0 = full_length(src)
                end = min(d0, FULL_MAX_S)
                moments = [{"start": 0.0, "end": end, "score": 1.0, "reasons": ["full video"], "text": "", "full": True}]
                if d0 > FULL_MAX_S + 1:
                    job["cut_short"] = int(FULL_MAX_S // 60)
        except Exception as e:  # noqa: BLE001
            log(f"upload {num} failed: {e}")
            hint = ("YouTube blocked the download from GitHub's servers — save the video and use Upload instead."
                    if "youtu" in link and re.search(r"sign in|bot|confirm|403|429", str(e), re.I)
                    else "TikTok blocked the download from GitHub's servers — save the video in TikTok and use Upload instead."
                    if "tiktok" in (job.get("url") or "")
                    else "Try a shorter part (add a time range) or upload the file instead." if link
                    else "Try a shorter video or another format (MP4).")
            _gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": f"Sorry — that video couldn't be processed ({str(e)[:150]}). {hint}"})
            _gh("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
            continue
        login = (job.get("login") or "").lower()
        s_cfg = CFG["streamers"].get(login, {})
        name = s_cfg.get("name") or (job.get("streamer") if job.get("streamer") not in ("Nicholas", "Test") else "") or ""
        # (no streamer picked: leave the name empty so hooks never say "Nicholas" - you're the poster, not the person on screen)
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
                if m.get("part") and not m["text"]:
                    try:
                        m["text"] = transcript(raw)
                    except Exception:  # noqa: BLE001
                        m["text"] = ""
                try:
                    cam, q, frac = find_cam_info(raw, dur, st.get("learned_cams", {}).get(login))
                except Exception as e:  # noqa: BLE001   a webcam-check error never costs you the clip: full screen
                    log(f"webcam check failed {cid}: {e}")
                    cam, q, frac = None, "none", None
                rec = {"status": "new", "source": "upload", "streamer": name, "login": login, "src_link": job.get("url") or "",
                       "tag": s_cfg.get("tag") or ("#" + re.sub(r"\W", "", name) if name else ""), "campaign": s_cfg.get("campaign", ""),
                       "title": f"{job.get('name', 'Your video')} @ {int(m['start'] // 60)}:{int(m['start'] % 60):02d}",
                       "created": iso(NOW), "found": iso(NOW), "twitch": link, "transcript": m["text"],
                       "seconds": round(dur), "moment": {k: m[k] for k in ("start", "end", "score", "reasons")},
                       "cam_v": CAM_V}
                if m.get("full"):   # one full video: no text box from us (add your own in the Studio), no AI hook
                    rec.update(full=True, title=job.get("name") or "Your video", hook="", no_hook=True,
                               hook_kind="user", hook_by_user=True)
                    m.pop("hook", None)
                if m.get("part"):   # the hook is just "Part 1", "Part 2", ... (no AI hook - Nicholas's call)
                    rec.update(part=m["part"], parts=m["parts"], title=f"{job.get('name', 'Your video')} · Part {m['part']}",
                               hook=f"Part {m['part']}", hook_kind="user", hook_by_user=True)
                    m.pop("hook", None)
                if m.get("hook_ai"):
                    rec.update(hook=m["hook"], hook_kind="ai")
                elif m.get("hook"):
                    rec.update(hook=m["hook"], hook_auto=True, hook_kind="transcript")
                if m.get("ai_score") is not None:
                    rec["moment"]["ai_score"] = m["ai_score"]
                    sc = int(m["ai_score"] * 10)
                    rec.update(viral=sc, viral_verdict="post" if sc >= 70 else "maybe" if sc >= 45 else "skip",
                               viral_why=", ".join(m.get("reasons", []))[:90])
                if job.get("asis"):
                    rec.update(cam="0,0,1,1", cam_q="none", layout="fill", asis=True, title=job.get("name") or "Your clip")
                    if job.get("hook"):
                        rec["hook_by_user"] = True
                elif cam and q in ("edge", "learned"):
                    rec.update(cam=cam, cam_q=q, layout="split")
                else:   # no webcam in your own link/upload: keep it, gameplay fills the whole screen
                    rec.update(cam="0,0,1,1", cam_q="none", layout="fill")
                st["clips"][cid] = rec
                made.append(f"- {int(m['start'] // 60)}:{int(m['start'] % 60):02d}–{int(m['end'] // 60)}:{int(m['end'] % 60):02d} "
                            f"({', '.join(m['reasons']) or 'good moment'}) — {rec.get('hook') or ''}")
            except Exception as e:  # noqa: BLE001
                log(f"cutting clip {cid} failed: {e}")
        msg = (f"Made the full video **{job.get('name', 'your video')}** — it's in Your clips (it doesn't auto-post)"
               + (f". Long video: it's the first {job['cut_short']} minutes — paste it again with a time range for another part" if job.get("cut_short") else "") + ":\n"
               + "\n".join(made)) if made else "Couldn't make parts from this video — try uploading the file instead."
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


# ---------------------------------------------------------------- quick text edits (quickedit.py, `edits` branch)
EDITS_DIR = ROOT / "_edits"
_EDITS: dict = {}


def load_edits() -> dict:
    try:
        return json.loads((EDITS_DIR / "edits.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def apply_quick_edits(clips: dict, edits: dict) -> None:
    """Your quick text edits become the clip's real hook/caption/box (so later re-makes keep them), without
    triggering a re-make: the edited video already exists on the site."""
    for cid, e in edits.items():
        c = clips.get(cid)
        if not c or e.get("at", 0) <= max(c.get("quick_at", 0), c.get("rendered", 0) or 0):
            continue
        if e.get("hook"):
            c["hook"] = e["hook"]
            c.pop("no_hook", None)
            c["hook_by_user"] = True
        if "caption" in e:
            c["caption"] = e["caption"]
        ul = dict(c.get("user_layout") or {})
        for k in ("hook_style", "hook_y", "hook_scale"):
            if k in e:
                ul[k] = e[k]
        if ul:
            c["user_layout"] = ul
        c["quick_at"] = e["at"]
        if c.get("final_hash"):   # the edited video stands in for the final one: don't re-make it for the new words
            c["final_hash"] = c["final_v"] = hashlib.sha1((c["hook"] + c.get("cam", "") + LAYOUT_V + c.get("layout", "")).encode()).hexdigest()[:10]
        log(f"quick edit applied: {cid}")


def sync_edit_videos() -> None:
    """Copy the quick-edited videos onto the site folder, so publishing the site never removes them."""
    try:
        if os.environ.get("GITHUB_ACTIONS"):
            subprocess.run(["bash", "-c", 'rm -rf "$D" && mkdir -p "$D" && git fetch -q --depth=1 origin edits:refs/remotes/origin/edits '
                            '&& git archive origin/edits | tar -x -C "$D"'], cwd=str(ROOT), env={**os.environ, "D": str(EDITS_DIR)},
                           timeout=180, check=False)
        src = EDITS_DIR / "v" / "e"
        dst = VID / "e"
        dst.mkdir(parents=True, exist_ok=True)
        want = {f.name for f in src.glob("*.mp4")} if src.exists() else set()
        for f in dst.glob("*.mp4"):
            if f.name not in want:
                f.unlink()
        for n in want:
            if not (dst / n).exists():
                shutil.copy2(src / n, dst / n)
    except Exception as e:  # noqa: BLE001
        log(f"edit videos sync skipped: {e}")


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
    try:
        sync_edit_videos()
        apply_quick_edits(st.get("clips", {}), load_edits())
    except Exception as e:  # noqa: BLE001
        log(f"quick edits not applied: {e}")
    _topic = st.get("topic") or dict(DEFAULT_TOPIC)   # what we're clipping (site's topic search)
    fetch_clips_mod.TOPIC.clear(); fetch_clips_mod.TOPIC.update(_topic)
    import viral as _viral_topic
    _viral_topic.TOPIC.clear(); _viral_topic.TOPIC.update(_topic)
    clips = st["clips"]
    try:   # what the engagement learner found on our channels -> feeds the AI hook writer
        import viral
        _ins = json.loads((POSTED_DIR / "insights.json").read_text())
        viral.LEARNED["guidance"] = learn.guidance_text(_ins)
        LEARNED_INS.update(_ins)
        if _ins.get("guidance"):
            log(f"hook learner: {len(_ins['guidance'])} lessons from {_ins.get('videos')} videos, style weights {_ins.get('weights')}")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"hook learner not loaded: {e}")
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
            topic = st.get("topic") or dict(DEFAULT_TOPIC)
            fetch_clips_mod.TOPIC.clear(); fetch_clips_mod.TOPIC.update(topic)
            import viral as _v
            _v.TOPIC.clear(); _v.TOPIC.update(topic)
            picks = pick_clips(twitch_headers(), set(clips))
            for pc in picks:
                pc["topic"], pc["topic_tags"] = topic.get("name", ""), topic.get("tags", "")
            try:   # what's big on Twitch right now (shown in the site's topic search)
                st["trending"] = [{"id": g["id"], "name": g["name"], "box": g.get("box_art_url", "").replace("{width}", "144").replace("{height}", "192")}
                                  for g in (helix(twitch_headers(), "games/top", first=24).get("data") or [])]
            except Exception as e:  # noqa: BLE001
                log(f"trending list failed: {e}")
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
                    "seconds": round(dur), "topic": c.get("topic", ""), "topic_tags": c.get("topic_tags", ""),
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
                    if not cam and (c.get("view_count") or 0) >= NOCAM_MIN_VIEWS:
                        # no webcam, but already proven on Twitch (lots of views): full-screen gameplay instead of skipping
                        rec.update(cam="0,0,1,1", cam_q="none", layout="fill", nocam=True, cam_v=CAM_V)
                    elif not cam:
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
        global _EDITS
        sync_edit_videos()
        _EDITS = load_edits()
        ready = [dict(id=cid, **{k: v for k, v in c.items() if k not in ("final_hash",)})
                 for cid, c in clips.items() if c["status"] == "new"]
        for r in ready:
            r["video"] = f"v/{r['id']}_final.mp4"
            r["poster"] = f"v/{r['id']}.jpg"
            _hh = hashlib.sha1((r.get("hook", "") + r.get("cam", "") + LAYOUT_V + r.get("layout", "")).encode()).hexdigest()[:10]
            r["final"] = (f"v/{r['id']}_final.mp4?v={r['final_v']}"
                          if r.get("final_v") and (r.get("hook") or r.get("no_hook")) and r.get("final_v") == _hh
                          and (VID / f"{r['id']}_final.mp4").exists() else "")
            r["final_abs"] = f"{PAGES}/v/{r['id']}_final.mp4?v={r['final_v']}" if r["final"] else ""
            r["clean"] = (f"v/{r['id']}_clean.mp4?v={r['clean_v']}"
                          if r.get("clean_v") and (VID / f"{r['id']}_clean.mp4").exists() else "")
            if r["final"] and r.get("rendered"):     # re-made with a new layout: new address so phones don't show the old one
                r["final"] += f"&r={r['rendered']}"
                r["final_abs"] += f"&r={r['rendered']}"
            if r["clean"] and r.get("rendered"):
                r["clean"] += f"&r={r['rendered']}"
            r["src"] = f"v/{r['id']}_src.jpg" if (VID / f"{r['id']}_src.jpg").exists() else ""
            r["vposter"] = (f"v/{r['id']}_p.jpg?r={r.get('rendered', '')}" if (VID / f"{r['id']}_p.jpg").exists() else "")
            r["clipped"] = r.get("created")
            e = _EDITS.get(r["id"])   # newest quick text edit (still newer than the last full re-make)
            if e and e.get("video") and e.get("at", 0) > (r.get("rendered") or 0) and (VID / "e" / Path(e["video"]).name).exists():
                r["final"] = e["video"]
                r["final_abs"] = f"{PAGES}/{e['video']}"
        # quick-edit videos of clips that left the queue only take up site space
        live_ids = {r["id"] for r in ready}
        for f in (VID / "e").glob("*.mp4") if (VID / "e").exists() else []:
            if f.name.rsplit("_", 1)[0] not in live_ids:
                f.unlink(missing_ok=True)
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
            for m in re.finditer(r"\] (.+?): (\d+) fresh .+? clips", log_text):
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
            "topic": st.get("topic") or DEFAULT_TOPIC,
            "trending": st.get("trending", []),
            "ai": {"status": st.get("ai_status", "no key"), "checked": st.get("ai_checked", ""),
                   "rated": sum(1 for c in clips.values() if c["status"] == "new" and "viral" in c)},
        }, indent=1, ensure_ascii=False))


    # one-time: bring back fresh clips that the old replace-every-run rule / cap threw out
    if st.get("redo_wholebox_v", 0) < 1:   # one time: whole webcam box, sized to stay sharp, for every queued clip
        st["redo_wholebox_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                for k in ("final_hash", "clean_v", "hd", "hd_cam"):
                    c.pop(k, None)
                c["hd_sfx"] = (c.get("hd_sfx") or "") + "_w"
    if st.get("redo_natural_cam_v", 0) < 1:   # one time: every queued webcam re-upscaled with the natural (less waxy) setting
        st["redo_natural_cam_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                for k in ("final_hash", "clean_v", "hd", "hd_cam"):
                    c.pop(k, None)
                c["hd_sfx"] = (c.get("hd_sfx") or "") + "_n"
    n_hq = 0
    for cid, c in clips.items():     # every run: queued VOD clips that came down below 1080p get another try
        m = re.match(r"vod(\d+)_(\d+)$", cid)
        if not m or c["status"] != "new" or c.get("src_h", 0) >= 1080 or c.get("hq_tries", 0) >= 3 or n_hq >= 4:
            continue
        rawp = RAW / f"{cid}.mp4"
        try:
            cap = cv2.VideoCapture(str(rawp)); hh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0); cap.release()
        except Exception:  # noqa: BLE001
            hh = 0
        if hh >= 1080:
            c["src_h"] = hh
            continue
        c["hq_tries"] = c.get("hq_tries", 0) + 1
        n_hq += 1
        try:
            a = float(m.group(2)); dur = float(c.get("seconds") or 20) - 2
            got = fetch_link_section(f"https://www.twitch.tv/videos/{m.group(1)}", a, a + max(8.0, dur), rawp, Path(tempfile.mkdtemp()))
            c["src_h"] = got
            if got >= 1080:
                for k in ("final_hash", "clean_v", "hd", "hd_cam"):
                    c.pop(k, None)
                c["hd_sfx"] = (c.get("hd_sfx") or "") + "_hq"
                log(f"{cid}: got the full 1080p source now ({hh or '?'}p before) - re-making it")
        except Exception as e:  # noqa: BLE001
            log(f"1080p re-download failed {cid}: {str(e)[:100]}")
    if st.get("upload_fill_v", 0) < 1:   # one time: your no-webcam clips go full-screen; ones the robot dropped come back
        st["upload_fill_v"] = 1
        for c in clips.values():
            if c.get("source") != "upload":
                continue
            if c["status"] == "new" and c.get("layout") == "full" and (c.get("user_layout") or {}).get("mode") != "full":
                c["layout"] = "fill"; c.pop("final_hash", None)
            elif c["status"] == "skip" and not re.search(r"nud|sex|gore|removed by", c.get("skip_reason", ""), re.I) \
                    and NOW - parse(c.get("created") or iso(NOW)) < timedelta(days=2):
                c.update(status="new"); c.pop("skip_reason", None); c.pop("final_hash", None)
                if c.get("layout") == "full":
                    c["layout"] = "fill"
    if st.get("upload_name_v", 0) < 1:   # one time: your pasted clips were named "Nicholas" - hooks said you were on screen
        st["upload_name_v"] = 1
        for c in clips.values():
            if c.get("source") == "upload" and c.get("streamer") in ("Nicholas", "Test") and not c.get("login"):
                c["streamer"], c["tag"] = "", ""
                if "nicholas" in (c.get("hook") or "").lower() and not c.get("hook_by_user"):
                    c["hook"] = f"Part {c['part']}" if c.get("part") else ""
                    c["hook_auto"] = True
                    for k in ("viral", "pic_ok", "final_hash", "hook_kind"):
                        c.pop(k, None)
    if st.get("parts_plain_v", 0) < 1:   # one time: parts already made get plain "Part N" hooks
        st["parts_plain_v"] = 1
        for c in clips.values():
            if c.get("part") and c["status"] == "new" and c.get("hook") != f"Part {c['part']}" and not c.get("no_hook"):
                c.update(hook=f"Part {c['part']}", hook_kind="user", hook_by_user=True)
                c.pop("hook_auto", None); c.pop("final_hash", None)
            elif c.get("part"):
                c.update(hook_kind="user", hook_by_user=True); c.pop("hook_auto", None)
    if st.get("ufo_nohook_v", 0) < 1:   # one time: the UFO TikTok already shows "Part 1/2/3" - no box from us
        st["ufo_nohook_v"] = 1
        for cid, c in clips.items():
            if cid.startswith("upmwtgx8fl2_"):
                c.update(hook="", no_hook=True, hook_by_user=True, hook_kind="user")
                c.pop("hook_auto", None); c.pop("final_hash", None)
    if st.get("kai_chat_v", 0) < 1:   # one time: re-make queued Kai clips whose webcam crop still showed his chat
        st["kai_chat_v"] = 1
        for c in clips.values():
            if c["status"] == "new" and c.get("login") == "kaicenat" and not (c.get("user_layout") or {}).get("cam"):
                c.pop("final_hash", None)
    if st.get("nick_cam_v", 0) < 1:   # one time: Nick Eh 30's clips were skipped - his cut-out cam has no border
        st["nick_cam_v"] = 1
        box = ",".join(str(v) for v in (CFG["streamers"].get("nickeh30", {}).get("facecam") or []))
        for c in clips.values():
            if c.get("login") == "nickeh30" and c.get("skip_reason") == "no webcam found" and box \
                    and NOW - parse(c.get("created") or c.get("found") or iso(NOW)) < timedelta(hours=34):
                c.update(status="new", cam=box, cam_q="learned", cam_v=CAM_V, layout="split")
                c.pop("skip_reason", None); c.pop("final_hash", None); c.pop("nocam", None)
    if st.get("nocam_revive_v", 0) < 1:   # one time: proven no-webcam clips from the last 34 h -> full-screen gameplay
        st["nocam_revive_v"] = 1
        for c in clips.values():
            if c.get("skip_reason") == "no webcam found" and c.get("source") != "upload" \
                    and (c.get("views") or 0) >= NOCAM_MIN_VIEWS \
                    and NOW - parse(c.get("created") or c.get("found") or iso(NOW)) < timedelta(hours=34):
                c.update(status="new", cam="0,0,1,1", cam_q="none", layout="fill", nocam=True, cam_v=CAM_V)
                c.pop("skip_reason", None); c.pop("final_hash", None)
    if st.get("quality_revive_v", 0) < 2:   # one time (Nov 23, quality over newest): bring back the best clips from the
        st["quality_revive_v"] = 2         # last 36 h that were thrown out for age / space / "replaced" but never posted
        back = sorted([(c.get("views") or 0, cid) for cid, c in clips.items()
                       if c["status"] == "expired" and c.get("source") != "upload" and (c.get("views") or 0) >= 50
                       and (c.get("skip_reason") or "") in ("", "site space (lowest rated)", "replaced by newer clips", "queue full")
                       and NOW - parse(c.get("created") or c.get("found") or iso(NOW)) < timedelta(hours=34)], reverse=True)[:12]
        for _, cid in back:
            c = clips[cid]
            c.update(status="new"); c.pop("skip_reason", None); c.pop("final_hash", None)
        if back:
            log(f"brought back {len(back)} high-view clips from the last 36 h")
    if st.get("redo_camonly_v", 0) < 1:   # one time: black-screen clips skipped earlier get the webcam-only treatment
        st["redo_camonly_v"] = 1
        for c in clips.values():
            if c.get("skip_reason") == "no gameplay on screen (black screen)" \
                    and NOW - parse(c.get("created") or c.get("found") or iso(NOW)) < timedelta(hours=9):
                c.update(status="new"); c.pop("skip_reason", None); c.pop("final_hash", None)
    if st.get("redo_captions_v", 0) < 1:   # one time: re-check every queued clip with the new caption finder
        st["redo_captions_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                c.pop("final_hash", None); c.pop("clean_v", None)
    if st.get("redo_kaichat_v", 0) < 6:   # one time: Kai's chat strip came back (v6: see-through chat over bright game) -> re-frame
        st["redo_kaichat_v"] = 6
        for c in clips.values():
            if c["status"] == "new" and CFG["streamers"].get(c.get("login", ""), {}).get("chat_on_cam") and not (c.get("user_layout") or {}).get("cam"):
                for k in ("final_hash", "clean_v", "hd", "hd_cam"):
                    c.pop(k, None)
                c["hd_sfx"] = "_q5"
    if st.get("redo_vodhq_v", 0) < 1:   # one time: VOD clips were saved blocky -> download again at full quality
        st["redo_vodhq_v"] = 1
        n_re = 0
        for cid, c in clips.items():
            m = re.match(r"vod(\d+)_(\d+)$", cid)
            if not m or c["status"] != "new" or NOW - parse(c.get("created") or c.get("found") or iso(NOW)) > timedelta(hours=10):
                continue
            a = float(m.group(2)); dur = float(c.get("seconds") or 20) - 2
            try:
                fetch_link_section(f"https://www.twitch.tv/videos/{m.group(1)}", a, a + max(8.0, dur), RAW / f"{cid}.mp4", Path(tempfile.mkdtemp()))
                n_re += 1
            except Exception as e:  # noqa: BLE001
                c.update(status="skip", skip_reason="old blocky download, couldn't get it again")
                log(f"re-download failed {cid}: {str(e)[:120]}")
                continue
            for k in ("final_hash", "clean_v", "hd", "hd_cam", "cam_used"):
                c.pop(k, None)
            c["hd_sfx"] = "_q2"
        for c in clips.values():   # every other queued clip: re-upscale its webcam from the full-detail source
            if c["status"] == "new" and not c.get("hd_sfx"):
                for k in ("final_hash", "clean_v", "hd", "hd_cam"):
                    c.pop(k, None)
                c["hd_sfx"] = "_q2"
        log(f"re-downloaded {n_re} VOD clips at full quality")
    if st.get("redo_wholecam_v", 0) < 1:   # one time: re-frame queued clips with the whole webcam (less zoom = sharper)
        st["redo_wholecam_v"] = 1
        for c in clips.values():
            if c["status"] == "new" and not c.get("user_layout"):
                c.pop("final_hash", None); c.pop("clean_v", None); c.pop("hd", None)
    if st.get("redo_hd_v", 0) < 1:   # one time: re-make queued clips so their webcams go to the AI upscaler
        st["redo_hd_v"] = 1
        for c in clips.values():
            if c["status"] == "new" and not c.get("hd"):
                c.pop("final_hash", None); c.pop("clean_v", None)
    if st.get("redo_face_v2", 0) < 1:   # one time: re-frame again with the fixed chat trim
        st["redo_face_v2"] = 1
        for c in clips.values():
            if c["status"] == "new":
                c.pop("final_hash", None); c.pop("clean_v", None)
    if st.get("redo_face_v", 0) < 1:   # one time: re-frame every queued clip around the streamer's real face
        st["redo_face_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                c.pop("final_hash", None); c.pop("clean_v", None)
    if st.get("redo_vision_v", 0) < 1:   # one time: re-check every queued clip (picture hook, face, smaller caption)
        st["redo_vision_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                c.pop("pic_ok", None); c.pop("final_hash", None); c.pop("clean_v", None)
    if st.get("redo_hookfit_v", 0) < 1:   # one time: re-make queued videos with the narrower hook box
        st["redo_hookfit_v"] = 1
        for c in clips.values():
            if c["status"] == "new":
                c.pop("final_hash", None)
    if st.get("redo_kai_v2", 0) < 1:   # one time: Kai again (more room above his chat)
        st["redo_kai_v2"] = 1
        for c in clips.values():
            if c["status"] == "new" and c.get("login") == "kaicenat":
                c.pop("final_hash", None); c.pop("clean_v", None)
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

    # 3d. new clips REPLACE old ones: once the newest runs have made enough finished clips, stream clips older than
    #     5 h leave the queue so the poster only has fresh moments to post (first poster wins - Nov 22 study).
    #     Kept: your pasted clips, Post-now picks and clips you edited.
    def _age_h(c):
        try:
            return (NOW - parse(c.get("created") or c.get("found") or iso(NOW))).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            return 0.0
    newer_ok = [c for c in clips.values() if c["status"] == "new" and c.get("source") != "upload" and _age_h(c) < REPLACE_AGE_H
                and c.get("final_hash") and c.get("pic_ok") is not False]
    if len(newer_ok) >= REPLACE_MIN_NEW:
        n_old = 0
        for cid, c in clips.items():
            if c["status"] == "new" and c.get("source") != "upload" and not c.get("post_now") and not c.get("hook_by_user") \
                    and _age_h(c) >= REPLACE_AGE_H:
                c.update(status="expired", skip_reason="replaced by newer clips")
                n_old += 1
        if n_old:
            log(f"queue refreshed: {n_old} clips over {REPLACE_AGE_H} h old replaced by {len(newer_ok)} newer finished clips")

    # 4a. keep the queue to the best QUEUE_MAX clips (about 3 days of posts) so the site stays small
    fresh = [dict(id=k, **v) for k, v in clips.items() if v["status"] == "new"]
    keep = {r["id"] for r in mix_order([r for r in fresh if r.get("source") != "upload"])[:QUEUE_MAX]}   # your parts don't use up stream-clip spots
    for r in fresh:
        if r["id"] not in keep and not r.get("post_now") and not r.get("hook_by_user") and r.get("source") != "upload":
            clips[r["id"]].update(status="expired", skip_reason="queue full")
    if len([r for r in fresh if r.get("source") != "upload"]) > QUEUE_MAX:
        log(f"queue full: kept {QUEUE_MAX} stream clips")

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
                if r["score"] <= 10 and not c.get("hook_by_user") and not c.get("post_now") and c.get("source") != "upload":
                    c.update(status="skip", skip_reason=f"AI: {r['why'] or 'not postable'}")
                    log(f"AI removed {cid}: {r['why']}")
                    continue
                if r["hook"] and len(r["hook"]) >= 12 and (c.get("hook_auto") or not c.get("hook")) and not c.get("hook_by_user"):
                    c.update(hook=(f"Part {c['part']}: " if c.get("part") else "") + r["hook"], hook_kind="ai")
                    c.pop("hook_auto", None)
            if not res and re.search(r"429|quota|exhaust|rate", ai_status, re.I):
                st["ai_backoff2"] = iso(NOW + timedelta(minutes=60))   # free daily/minute limit: try again in an hour
            st["ai_status"] = ai_status
            st["ai_checked"] = iso(NOW)
    except Exception as e:  # noqa: BLE001
        log(f"AI scoring failed: {e}")

    # 3c. short hooks: automatic hooks that came out long get rewritten shorter by the AI (yours are never touched)
    try:
        import viral
        def _lim(c):
            return 64 if (c.get("style") or {}).get("hooklen") == "tiny" else viral.HOOK_MAX
        long_h = {cid: c["hook"] for cid, c in clips.items()
                  if c["status"] == "new" and c.get("hook") and not c.get("hook_by_user")
                  and len(c["hook"]) > _lim(c) and not c.get("hook_short_tried")}
        got = {}
        for tiny in (True, False):
            part = {k: v for k, v in long_h.items() if (_lim(clips[k]) < viral.HOOK_MAX) == tiny}
            if part:
                got.update(viral.ai_shorten(dict(list(part.items())[:30]), log, tiny=tiny))
        if got:
            for cid, h in got.items():
                log(f"shorter hook {cid}: {h}")
                clips[cid]["hook"] = h
                clips[cid]["hook_short_tried"] = True
            if got:
                log(f"shortened {len(got)} long hooks")
    except Exception as e:  # noqa: BLE001
        log(f"hook shortening skipped: {e}")

    # 4. drop old stuff, get queued clips ready (thumbnail, hook); videos are made in 4b
    jobs = []
    for cid, c in list(clips.items()):
        age_ref = c.get("posted_at") or c.get("found") or iso(NOW)
        age_h = (NOW - parse(age_ref)).total_seconds() / 3600
        clip_age_h = (NOW - parse(c.get("created") or c.get("found") or iso(NOW))).total_seconds() / 3600
        keep_h = KEEP_UPLOAD_HOURS if c.get("source") == "upload" else KEEP_NEW_HOURS
        if (c["status"] == "new" and clip_age_h > keep_h) or (c["status"] != "new" and age_h > KEEP_POSTED_HOURS):
            c["status"] = "expired" if c["status"] == "new" else c["status"]
        if c["status"] == "used" and age_h < 3:
            continue  # keep the posted video online a few hours (Buffer downloads it from here within minutes)
        if c["status"] != "new":
            for suf in (".mp4", "_final.mp4", "_clean.mp4", ".jpg", "_src.jpg", "_p.jpg"):
                (VID / f"{cid}{suf}").unlink(missing_ok=True)
            (RAW / f"{cid}.mp4").unlink(missing_ok=True)
            continue
        raw = RAW / f"{cid}.mp4"
        if not raw.exists() and c.get("source") == "upload":
            # your pasted / uploaded clips: the original isn't kept between runs and has no Twitch address.
            # Finished video already on the site -> keep it as is. Needs a re-make -> fetch the link again.
            if (VID / f"{cid}_final.mp4").exists() and c.get("final_hash"):
                hh0 = hashlib.sha1(((c.get("hook") or "") + c.get("cam", "") + LAYOUT_V + c.get("layout", "")).encode()).hexdigest()[:10]
                if c.get("final_hash") == hh0:
                    continue
            try:
                refetch_upload(c, raw, tmp)
            except Exception as e:  # noqa: BLE001
                log(f"couldn't get your clip {cid} again: {str(e)[:120]}")
                if not (VID / f"{cid}_final.mp4").exists():
                    c["status"] = "expired"
                continue
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
            if not c.get("hook") and not c.get("no_hook"):
                c["hook"], kind = auto_hook(c)
                c["hook_auto"] = True
                c["hook_kind"] = kind
                log(f"auto hook {cid}: {c['hook']}")
            if c.get("hook") or c.get("no_hook"):   # no_hook: the video already has its own text - no box from us
                c["hook"] = c.get("hook") or ""
                hh = hashlib.sha1((c["hook"] + c["cam"] + LAYOUT_V + c.get("layout", "")).encode()).hexdigest()[:10]
                ch = hashlib.sha1((c["cam"] + LAYOUT_V + "clean" + c.get("layout", "")).encode()).hexdigest()[:10]
                need_f = c.get("final_hash") != hh or not (VID / f"{cid}_final.mp4").exists()
                need_c = (c.get("clean_v") != ch or not (VID / f"{cid}_clean.mp4").exists()) and not c.get("no_clean")
                if (CAMHD / f"{hd_key(cid, c)}.mp4").exists() and not c.get("hd"):   # AI-upscaled webcam came back: re-make
                    need_f = need_c = True
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
        if not c.get("style") and not c.get("user_layout") and not c.get("asis") and c.get("source") != "upload" \
                and not c.get("final_hash"):
            c["style"] = learn.pick_style(LEARNED_INS, cid)   # dynamic model: test looks, keep what gets views
        sty = {**(c.get("style") or {}), **{k: v for k, v in (c.get("user_layout") or {}).items() if k in ("subs", "punch")}}
        if not c.get("asis") and not c.get("part") and not c.get("full"):   # your full video / parts stay whole (no trims)
            raw = best_part(raw, cid, tmp, c)
            raw = cut_dead_air(raw, cid, tmp)
        if lay == "split" and not c.get("user_layout") and c.get("source") != "upload" and c.get("cam") \
                and (c.get("cam_only") or gameplay_is_black(raw, c.get("cam") or "")):
            vs = c.get("viral")
            if c.get("cam_only") or (c.get("viral_verdict") != "skip" and (vs is None or vs >= 60)):
                c["cam_only"] = True       # viral moment with no game on screen: the webcam is the whole video
            else:
                c.update(status="skip", skip_reason="no gameplay on screen (black screen)")
                log(f"skipped {cid}: the stream showed a black screen, no gameplay")
                return job
        _ul = c.get("user_layout") or {}
        if _ul.get("trim") or (_ul.get("speed") or 1) > 1.01:   # your trim / speed from the editor
            try:
                a0, b0 = (_ul.get("trim") or [0, 9999])
                sp = float(_ul.get("speed") or 1)
                out_t = tmp / f"{cid}_edit.mp4"
                vf = f"trim=start={a0}:end={b0},setpts=(PTS-STARTPTS)/{sp:.3f}"
                af = f"atrim=start={a0}:end={b0},asetpts=PTS-STARTPTS,atempo={sp:.3f}"
                ff("-i", str(raw), "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]", "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-preset", "fast", "-crf", "13", "-c:a", "aac", "-b:a", "160k", str(out_t))
                raw = out_t
            except Exception as e:  # noqa: BLE001
                log(f"trim/speed skipped {cid}: {e}")
        if c.get("pic_ok") is None and not control.get("ai_off"):   # look at the picture: no nudity / gore
            try:
                import viral
                fr = _frames(raw, [1.5, 6, 11, 16])
                jp = [cv2.imencode(".jpg", cv2.resize(f, (768, int(f.shape[0] * 768 / f.shape[1]))),
                                   [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes() for f in fr]
                var = c.get("hook_variant") or learn.pick_variant(LEARNED_INS, cid)   # hook style being tested
                var_p = var + ("|tiny" if sty.get("hooklen") == "tiny" else "")
                ok, why, vhook = viral.ai_visual_ok(jp, log, c.get("streamer", ""),
                                                   (c.get("transcript") or "") + " | title: " + (c.get("title") or ""),
                                                   variant=var_p)
                if ok is not None:
                    c["pic_ok"] = ok
                if ok and vhook and len(vhook) >= 20 and not c.get("hook_by_user") and "streamer" not in vhook.lower():
                    c["hook"], c["hook_kind"], c["hook_variant"] = (f"Part {c['part']}: " if c.get("part") else "") + vhook, "ai", var
                    c.pop("hook_short_tried", None)   # a fresh hook gets its own length check
                    hh = hashlib.sha1((c["hook"] + c["cam"] + LAYOUT_V + c.get("layout", "")).encode()).hexdigest()[:10]
                    job = (cid, job[1], hh, ch, True, need_c)
                    need_f = True
                mine_ok = c.get("source") == "upload" and not re.search(r"nud|sex|gore|blood|explicit|naked", why or "", re.I)
                if ok is False and not c.get("post_now") and not mine_ok:   # your own clips: only nudity/gore removes them
                    c.update(status="skip", skip_reason=f"AI picture check: {why or 'nudity/sexual content'}")
                    log(f"picture check removed {cid}: {why}")
                    return job
            except Exception as e:  # noqa: BLE001
                log(f"picture check skipped {cid}: {e}")
        cut = 0.0
        cam = c.get("cam", "")
        ul = c.get("user_layout") or {}          # your own layout from the site's editor wins over the automatic one
        try:   # full-frame still for the editor (you drag the webcam box on it)
            sfr = _frames(raw, [3.0])
            if sfr:
                f0 = sfr[0]
                cv2.imwrite(str(VID / f"{cid}_src.jpg"), cv2.resize(f0, (960, int(f0.shape[0] * 960 / f0.shape[1]))),
                            [cv2.IMWRITE_JPEG_QUALITY, 72])
        except Exception:  # noqa: BLE001
            pass
        if ul.get("mode") == "full":
            lay = "full"
        elif ul.get("mode") == "irl":
            ix = ul.get("irl_x", c.get("irl_x") if (c.get("irl_x") or -1) >= 0 else 0.5)
            lay = f"irl:{ix:.3f}"
        if ul.get("cam"):
            cam = ul["cam"]
        auto = not ul                            # automatic webcam / no-gameplay decisions only without your layout
        if auto and lay == "split" and cam and c.get("irl_x") is None:
            try:
                ix = irl_focus(raw, cam)
            except Exception as e:  # noqa: BLE001
                ix = None
                log(f"no-gameplay check skipped {cid}: {e}")
            c["irl_x"] = ix if ix is not None else -1
        if auto and lay == "split" and (c.get("irl_x") or -1) >= 0:
            lay = f"irl:{c['irl_x']:.3f}"
            log(f"no gameplay in {cid}: full-screen shot on the streamer")
        if lay == "split" and cam and not ul.get("cam"):
            scfg = CFG["streamers"].get(c.get("login", ""), {})
            cam2 = cam_without_chat(raw, cam, bool(scfg.get("chat_on_cam")), scfg.get("chat_cut"))
            if cam2 != cam:
                log(f"chat over the webcam in {cid}: webcam box ends above it")
                cam = cam2
            try:
                fc = face_cam(raw, cam)       # frame the strip around the streamer's actual face
            except Exception as e:  # noqa: BLE001
                fc = cam
                log(f"face framing skipped {cid}: {e}")
            if fc is None and auto and not c.get("post_now") and c.get("source") != "upload":
                c.update(status="skip", skip_reason="no one steady in the webcam (away / camera off)")
                log(f"skipped {cid}: no steady face on camera")
                return job
            if c.get("cam_only") and auto:
                lay = "cam"                   # whole webcam, full width, on a blurred copy of itself (9:16)
                if not c.get("hd_sfx", "").endswith("_c"):
                    c["hd_sfx"] = c.get("hd_sfx", "") + "_c"
                log(f"no gameplay in {cid} but a strong moment: webcam-only TikTok video")
            # (the whole webcam box is shown now - no more zooming in on the face)
        hd = CAMHD / f"{hd_key(cid, c)}.mp4"
        cam_video = hd if (lay in ("split", "cam") and hd.exists() and c.get("hd_cam") == cam) else None
        if lay in ("split", "cam") and cam and not cam_video:
            try:   # small webcam source for the AI upscaler (runs on its own server, comes back in a later run)
                CAMSRC.mkdir(exist_ok=True)
                x, y, w, h = [float(v) for v in cam.split(",")]
                crop_h = f"ih*{h}"   # the whole webcam box (split and cam-only layouts both show all of it)
                ff("-i", str(raw), "-vf", f"crop=w=iw*{w}:h={crop_h}:x=iw*{x}:"
                   f"y=ih*{y},scale='min(480,iw)':-2:flags=area,fps=30",   # full detail (was cut to 270 px)
                   "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "12", str(CAMSRC / f"{hd_key(cid, c)}.mp4"))
                c["hd_cam"] = cam
            except Exception as e:  # noqa: BLE001
                log(f"webcam source for upscaling failed {cid}: {e}")
        if cam_video:
            c["hd"] = True
            log(f"using AI-upscaled webcam for {cid}")
        if lay == "split":
            cut = purple_caption_cut(raw, 0, c.get("cam", ""))
            if cut:
                log(f"burned-in captions in {cid}: cropped just {cut:.0%} off the bottom of the gameplay")
            c["cap_cut"] = cut
        c["cam_used"], c["layout_used"] = cam, lay
        if ul.get("trim") or (ul.get("speed") or 1) > 1.01:
            cam_video = None    # the sharp webcam was made for the untrimmed clip
        opts = dict(layout=lay, cut=cut, cam_video=cam_video, hook_y=ul.get("hook_y"), zoom=ul.get("zoom", 1.0),
                    pan=ul.get("pan", 0.0), hook_style=ul.get("hook_style") or sty.get("box") or "white",
                    hook_scale=ul.get("hook_scale", 1.0),
                    top_h=int(ul["top_h"]) if ul.get("top_h") else learn.CAM_PX.get(sty.get("cam", ""), None))
        # learner tests from the Nov 23 tool review: word-by-word subtitles, punch-in zoom on the big reaction
        try:
            if sty.get("subs") == "on":
                import viral
                ws = [w for sg in viral.transcribe(viral._audio(raw)) for w in sg["words"]]
                opts["subs"] = ws or None
            if sty.get("punch") == "on" and lay == "split":
                import viral
                a = viral._audio(raw)
                secs = len(a) // 16000
                if secs >= 4:
                    rms = [float(np.sqrt(np.mean(a[i * 16000:(i + 1) * 16000] ** 2))) for i in range(secs)]
                    opts["punch_at"] = float(int(np.argmax(rms[1:-1])) + 1)
        except Exception as e:  # noqa: BLE001
            log(f"subtitles/punch-in skipped for {cid}: {e}")
        if need_f:
            render_final(raw, cid, cam, None if c.get("no_hook") else c["hook"], tmp, **opts)
            try:   # vertical thumbnail of the finished video for the site
                ff("-ss", "1.2", "-i", str(VID / f"{cid}_final.mp4"), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "6",
                   str(VID / f"{cid}_p.jpg"))
            except Exception:  # noqa: BLE001
                pass
        if need_c:
            render_final(raw, cid, cam, None, tmp, out="_clean", **opts)
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
                    try:   # real length of OUR video (c["seconds"] is the original Twitch clip's length)
                        c["final_s"] = round(duration_of(VID / f"{cid}_final.mp4"))
                    except Exception:  # noqa: BLE001
                        pass
                if need_c:
                    c["clean_v"] = ch
                if need_f or need_c:
                    c["rendered"] = int(time.time())
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

    # 4c2. clips that missed the picture check (Google busy) get checked now; until then the poster won't post them
    if not control.get("ai_off"):
        import viral
        todo = [cid for _, cid in sorted(((clip_quality(c), cid) for cid, c in clips.items() if c["status"] == "new"
                and c.get("pic_ok") is None and (VID / f"{cid}_final.mp4").exists()), reverse=True)][:6]   # best clips first
        for cid in todo:
            c = clips[cid]
            try:
                fr = _frames(VID / f"{cid}_final.mp4", [1.5, 6, 11, 16])
                jp = [cv2.imencode(".jpg", cv2.resize(f, (432, 768)), [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes() for f in fr]
                ok, why, _h = viral.ai_visual_ok(jp, log, c.get("streamer", ""), c.get("title") or "")
                if ok is None:
                    break                      # Google still busy: try again next run
                c["pic_ok"] = ok
                if ok is False and not c.get("post_now") and not (c.get("source") == "upload" and not re.search(
                        r"nud|sex|gore|blood|explicit|naked", why or "", re.I)):
                    c.update(status="skip", skip_reason=f"AI picture check: {why or 'nudity/sexual content'}")
                    log(f"picture check removed {cid}: {why}")
            except Exception as e:  # noqa: BLE001
                log(f"late picture check skipped {cid}: {e}")

    # 4d2. webcam sources still waiting for the upscaler: only clips that are queued and not upscaled yet
    if CAMSRC.exists():
        by_key = {hd_key(k, v): v for k, v in clips.items()}
        for f in CAMSRC.glob("*.mp4"):
            c = by_key.get(f.stem)
            if not c or c["status"] != "new" or c.get("hd"):
                f.unlink(missing_ok=True)

    # 4e. size guard: GitHub Pages sites must stay under 1 GB -> drop the lowest-rated queued clips if close
    def site_mb():
        return sum(f.stat().st_size for f in VID.rglob("*") if f.is_file()) / 1e6   # incl. quick-edit videos (v/e)
    shrink_site_videos(st)
    mb = site_mb()
    if mb > SITE_MAX_MB:   # first: no-caption spare copies of the weaker half of the queue (the posted copy stays;
        ranked = sorted([(clip_quality(c), cid) for cid, c in clips.items()   # edits still work - they just take a remake)
                         if c["status"] == "new" and c.get("source") != "upload" and not c.get("hook_by_user")])
        for _, cid in ranked[: len(ranked) // 2]:
            if mb <= SITE_MAX_MB:
                break
            f = VID / f"{cid}_clean.mp4"
            if f.exists():
                f.unlink(); clips[cid].pop("clean_v", None); clips[cid]["no_clean"] = True
                mb = site_mb()
    if mb > SITE_MAX_MB:   # then: no-caption spare copies of your oldest parts
        mine = sorted([(c.get("found") or "", cid) for cid, c in clips.items() if c["status"] == "new" and c.get("source") == "upload"])
        for _, cid in mine[:-10]:
            if mb <= SITE_MAX_MB:
                break
            f = VID / f"{cid}_clean.mp4"
            if f.exists():
                f.unlink(); clips[cid].pop("clean_v", None); clips[cid]["no_clean"] = True
                mb = site_mb()
    if mb > SITE_MAX_MB:
        def keep_score(c: dict) -> float:
            """Higher = keep. Unrated clips count as average (they're usually brand new, not bad), and fresh moments
            are worth more (the first poster wins - Nov 22 study), so the oldest, weakest clips go first."""
            v = c.get("viral")
            v = 60.0 if v is None else float(v)
            try:
                age = (NOW - datetime.fromisoformat((c.get("created") or c.get("found") or "").replace("Z", "+00:00"))).total_seconds() / 3600
            except Exception:  # noqa: BLE001
                age = 6.0
            return clip_quality(c)
        cands = sorted([(keep_score(c), cid) for cid, c in clips.items() if c["status"] == "new"
                        and c.get("source") != "upload" and not c.get("post_now")])
        for _, cid in cands:
            if mb <= SITE_MAX_MB:
                break
            clips[cid].update(status="expired", skip_reason="site space (lowest rated)")
            for suf in ("_final.mp4", "_clean.mp4", ".mp4", ".jpg", "_src.jpg", "_p.jpg"):
                (VID / f"{cid}{suf}").unlink(missing_ok=True)
            mb = site_mb()
        log(f"site size guard: now {mb:.0f} MB")

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

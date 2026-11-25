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
# MINE_MODE=1: the separate "your clips" maker (Nov 24) - pasted links / uploads only, its own saved data
# (branch "mine") and its own site folder (v/m + mine.json), so it never waits for / clashes with the auto clip finder.
MINE = os.environ.get("MINE_MODE") == "1"
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT / "_state"))
POSTED_DIR = Path(os.environ.get("POSTED_DIR", ROOT / "_posted"))
SITE = Path(os.environ.get("SITE_OUT", ROOT / "_site"))
VID = SITE / "v"
RAW = Path(os.environ.get("RAW_DIR", ROOT / "_raw"))  # cached raw downloads (actions cache)
PAGES = os.environ.get("SITE_URL", "https://gtaclip-studio.pages.dev")   # Cloudflare since Nov 25 (GitHub Pages was capped at 1 GB)
KEEP_UPLOAD_HOURS = 48   # clips made from your own uploads stay 2 days
NOCAM_MIN_VIEWS = 200   # no-webcam clips are only kept (full-screen gameplay) when 200+ people already watched them
KEEP_NEW_HOURS = 36      # clips stay postable for 36 h after they were clipped (Nov 23: quality over newest)
REPLACE_QUEUE_EACH_RUN = False   # (the old all-at-once swap stays off - see REPLACE_MIN_NEW)
REPLACE_MIN_NEW = 5   # once 5+ finished clips under REPLACE_AGE_H old are ready, older stream clips leave the queue
PER_STREAMER_MAX = 4   # most clips one streamer can have waiting in the queue (their best ones)
REPLACE_AGE_H = 36   # same as the keep limit: quality decides what posts, age only retires clips at 36 h
MAX_RENDERS = 12         # videos made per run (2 at a time); the rest next run
CAMSRC = Path("_camsrc")   # webcam sources waiting for the AI upscaler (branch camsrc)
CAMHD = Path("_camhd")     # AI-upscaled webcam strips (branch camhd)


def hd_key(cid: str, c: dict) -> str:
    """File name for a clip's webcam in the upscaler branches (a new suffix = a fresh upscale from a new source)."""
    return cid + c.get("hd_sfx", "")
SITE_MAX_MB = int(os.environ.get("SITE_MAX_MB", "1600"))       # stream clips on the site (Cloudflare since Nov 25 - no 1 GB limit;
                                                               # this just keeps the GitHub working copy a sensible size)
QUEUE_MAX = 30           # finished videos kept ready (11 posts a day)
CAPTION_CUT = 0.0    # no cutting: full gameplay (user choice, Nov 19)
LAYOUT_V = "v4-hq"   # Nov 25: highest quality re-make of every unposted clip (final + no-caption copy)
LAYOUT_V_OLD = "v3-textonly-capcut"
FINAL_V = LAYOUT_V + "-follow4"   # posted videos only (not the clean copies): + follow pop-up / progress bar, follow4: + the spoken follow line (Nov 25)
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


def apply_layout(c: dict, body: str) -> str:
    """Your edit from the Studio (layout: tap) onto one clip. Used by the clip maker and the fast edit lane."""
    try:
        ul = json.loads(body[body.index("{"):body.rindex("}") + 1])
        keep = {}
        if ul.get("mode") in ("split", "full", "irl", "react"):
            keep["mode"] = ul["mode"]
        if isinstance(ul.get("v"), int):
            keep["v"] = ul["v"]   # v2+: the Studio knows the Reaction layout, so "split" really means split
        if isinstance(ul.get("cam"), str) and re.match(r"^[\d.]+,[\d.]+,[\d.]+,[\d.]+$", ul["cam"]):
            keep["cam"] = ul["cam"]
        for k, lo, hi in (("hook_y", 0.05, 0.95), ("zoom", 1.0, 2.5), ("pan", -1.0, 1.0), ("irl_x", 0.0, 1.0),
                          ("hook_scale", 0.6, 1.6), ("speed", 1.0, 2.0), ("top_h", 420, 1100)):
            if isinstance(ul.get(k), (int, float)):
                keep[k] = max(lo, min(hi, float(ul[k])))
        if ul.get("hook_style") in ("white", "black", "outline"):
            keep["hook_style"] = ul["hook_style"]
        for k in ("punch",):   # studio switch: punch-in zoom (subtitles are gone for good)
            if ul.get(k) in ("on", "off"):
                keep[k] = ul[k]
        tr = ul.get("trim")
        if isinstance(tr, list) and len(tr) == 2 and all(isinstance(v, (int, float)) for v in tr) and tr[1] - tr[0] >= 3:
            keep["trim"] = [max(0.0, float(tr[0])), float(tr[1])]
        sp = ul.get("span")   # new start / end on the whole footage (can go earlier / later than the clip)
        if isinstance(sp, list) and len(sp) == 2 and all(isinstance(v, (int, float)) for v in sp) and 3 <= sp[1] - sp[0] <= 120:
            keep["span"] = [round(max(0.0, float(sp[0])), 2), round(float(sp[1]), 2)]
            keep.pop("trim", None)
        if isinstance(ul.get("hook"), str) and ul["hook"].strip():
            c["hook"] = fix_names(strip_emoji(ul["hook"]))[:140]
            c.pop("no_hook", None)
            c["hook_by_user"] = True
        if isinstance(ul.get("caption"), str):
            c["caption"] = ul["caption"].strip()[:2000]
        c["user_layout"] = keep
        c.pop("final_hash", None); c.pop("clean_v", None)
        msg = "Done — the video is being re-made with your layout (next run, a few minutes)."
    except Exception as e:  # noqa: BLE001
        msg = f"Couldn't read that layout ({e})."
    return msg


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
        tm = None if MINE else re.match(r"^\s*topic:\s*(.*)$", title, re.I)   # the topic belongs to the auto finder
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
        if MINE != key.startswith("up"):   # your clips (ids start with "up") belong to the your-clips maker
            continue
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
                c["hook"] = fix_names(strip_emoji(hm.group(1)))[:140]   # streamer names spelled right
                c.pop("no_hook", None)
                c["hook_by_user"] = True
            if cm:
                c["caption"] = cm.group(1).strip()[:2000]
            msg = "Done — the video was re-made with your hook and caption."
        elif action == "layout":
            msg = apply_layout(st["clips"][cid], body)
        else:
            msg = "Not needed anymore — use Start/Stop on the dashboard."
        log(f"{action} {cid}: {msg}")
        _gh("POST", f"/repos/{REPO}/issues/{it['number']}/comments", {"body": msg})
        _gh("PATCH", f"/repos/{REPO}/issues/{it['number']}", {"state": "closed"})


SWEARS = re.compile(r"\b(f+u+c*k+\w*|sh+i+t+\w*|bitch\w*|ass(hole)?|damn|dick|hell|wtf|stfu|crap|bastard|motherf\w*)\b", re.I)


_EXTRA_NAMES = ["Jynxzi", "ohnePixel", "NICKMERCS", "IShowSpeed", "YourRAGE", "JasonTheWeen", "Vanoss", "Marlon",
                "Ronaldo", "Bartolozzi"]


from collections import Counter


def _osa(a: str, b: str) -> int:
    """Edit distance where swapping two neighbouring letters counts as one typo."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def fix_names(text: str) -> str:
    """Streamer names spelled right in YOUR hooks: 'Jynzxi' / 'jyxniz' / 'Jyzxi' -> 'Jynxzi'. Only a word that is
    clearly a mistyped streamer name (same first letter, nearly the same letters) - normal words are left alone."""
    names = set(_EXTRA_NAMES)
    for login, s_ in (CFG.get("streamers") or {}).items():
        for t in re.findall(r"[A-Za-z0-9]+", (s_ or {}).get("name") or ""):
            if len(t) >= 6 and t.lower() not in ("delirious", "stable", "gaming"):   # real words stay words
                names.add(t)

    text = re.sub(r"\bnick[\s-]*(?:eh|a)[\s-]*30\b", lambda m: m.group(0) if m.group(0) == "NickEh30" else "Nick Eh 30",
                  text or "", flags=re.I)   # Nickeh 30 / nick eh30 / Nick A30 -> Nick Eh 30

    def one(m):
        w = m.group(0)
        lw = w.lower()
        if len(lw) < 5:
            return w
        for nm in names:
            ln = nm.lower()
            if lw == ln:
                return nm if w.lower() == w or w.istitle() else w   # right spelling: just the right capitals
            if lw[0] != ln[0] or abs(len(lw) - len(ln)) > 1:
                continue
            close = _osa(lw, ln) <= (1 if len(ln) < 8 else 2)
            jumbled = len(lw) >= 5 and sum((Counter(ln) - Counter(lw)).values()) \
                + sum((Counter(lw) - Counter(ln)).values()) <= 1
            if close or jumbled:
                return nm
        return w
    return re.sub(r"[A-Za-z0-9]+", one, text or "")


def clean_hook(t: str) -> str:
    t = strip_emoji(t)
    t = SWEARS.sub("", t)
    t = re.sub(r"https?://\S+|[#@]\w+", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -|:,.")
    return t


JUNK_TITLE = re.compile(r"^(lol|lmao|w|l|yo|wow|omg|bro|gta ?6?|clip|meme|\W*)$", re.I)


def no_game_in_hook(h: str) -> str:
    """'Lacy was SHOCKED finding a puppy for sale in GTA 6' -> '... for sale' when GTA 6 isn't actually on screen."""
    tn = re.escape(fetch_clips_mod.TOPIC.get("name") or "GTA 6")
    h2 = re.sub(rf"\s*\b(in|inside|on|playing|during)\s+(?:{tn}|GTA\s*(?:6|VI))\b", "", h, flags=re.I)
    h2 = re.sub(rf"\b(?:{tn}|GTA\s*(?:6|VI))\s*", "", h2, flags=re.I) if re.search(r"\bGTA\b", h2, re.I) else h2
    h2 = re.sub(r"\s{2,}", " ", h2).strip(" -:,")
    return h2 if len(h2) >= 12 else h


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


def _guess_box(face, W, H, widen: float = 2.8, headroom: float = 0.0):
    x, y, w, h = face
    aspect = 1080 / 960
    bw = min(W, max(w * widen, 0.135 * W))   # cut-out webcams (no border): head and shoulders, less game
    bh = bw / aspect
    if bh > H:
        bh, bw = float(H), H * aspect
    cx, cy = x + w / 2, y + h * 0.85
    bx = min(max(0.0, cx - bw / 2), W - bw)
    by = min(max(0.0, cy - bh / 2), H - bh)
    if headroom and by > y - headroom * h:   # room above the head (hair, headphones, cat ears) - grow the box up
        top = max(0.0, y - headroom * h)
        bh, by = by + bh - top, top
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
    if cam and q == "guess":
        # CUT-OUT webcam (no border - Nick Eh 30, ohnePixel, YourRAGE): a steady face at the edge of the screen, of
        # webcam size, that moves like a live camera (not a picture / game character) -> use the box around it
        if cam_is_live(path, cam):
            q = "cutout"
        else:
            return "", "", None
    if not cam:
        return "", "", None   # no face that stays put -> skip the clip rather than risk a wrong face
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
    if not 0.06 <= face[3] / H <= 0.35:   # a cut-out webcam's face is webcam-sized: not a tiny NPC, not the whole screen
        return "", "", None
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


CLIP_MAX_S = 18    # auto clips never longer than this (Nov 24: our TikToks - avg watch 5-12 s, ~13% finish; the
CLIP_KEEP_S = 14   # short 10-15 s ones with the moment early finished 34-49%) - your own clips aren't trimmed


def cut_dead_air(src: Path, cid: str, tmp: Path, min_gap: float = 0.8) -> Path:
    """Auto-Editor's method with its tested defaults: anything quieter than 4% of the clip's loudest sound is
    "dead air"; gaps of at least `min_gap` s are cut, keeping 0.2 s of padding on each side so cuts don't feel
    abrupt. Short pauses (timing, reactions) are left alone. Capped at 20% of the clip (see budget below)."""
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
        # Budget (Nov 24): in GTA clips "quiet" is often the streamer silently watching the action - part of the
        # joke - and cutting all of it turned a 33 s clip into 10 s. So cut at most 20% of the clip, never below
        # 15 s, longest pauses first.
        budget = min(0.2 * dur, max(0.0, dur - 15.0))
        picked, used = [], 0.0
        for g in sorted(gaps, key=lambda g: g[0] - g[1]):   # longest first
            if used + (g[1] - g[0]) <= budget:
                picked.append(g); used += g[1] - g[0]
        gaps = sorted(picked)
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
        keep_s = CLIP_KEEP_S   # ~14 s (Nov 24 TikTok data: short clips with the moment early hold viewers best)
        if secs < 6:
            return raw
        rms = np.array([np.sqrt(np.mean(a[i * 16000:(i + 1) * 16000] ** 2) + 1e-9) for i in range(secs)])
        db = 20 * np.log10(rms + 1e-9)
        sm = np.convolve(db, np.ones(3) / 3, mode="same")
        p = int(np.argmax(sm[1:-1])) + 1
        if False:
            pass
        else:
            want = p - round(keep_s * 0.28)        # the big moment ~4 s in: a beat of setup, then the payoff
            med = float(np.median(db))
            # start on someone talking: the second (near the target) where the sound rises, and isn't silence
            cand = [t for t in range(max(1, want - 3), max(1, min(p - 3, want + 3) + 1)) if db[t] >= med - 3]
            start = float(max(cand, key=lambda t: (db[t] - db[t - 1]) - 0.4 * abs(t - want)) if cand else max(0, want))
            start = max(0.0, start)
        end = float(min(secs, max(start + keep_s, p + 5)))
        if end - start < keep_s:                  # moment near the end of the Twitch clip: start earlier instead of
            start = float(max(0, end - keep_s))   # making a short clip (a 60 s clip was coming out 11 s - Nov 24)
        # never cut mid-sentence / mid-laugh (Nov 24: clips started too late or stopped right before the payoff):
        # the start moves back to the pause before the first line, the end runs on to the next pause (the reaction lands)
        quiet = float(np.median(db)) - 6
        back = [t for t in range(int(start), max(-1, int(start) - 2), -1) if t < secs and db[t] < quiet]
        if back:
            start = float(back[0])
        fwd = [t for t in range(int(end), min(secs, int(end) + 6)) if db[t] < quiet]
        end = float(min(secs, fwd[0] + 0.5)) if fwd else float(min(secs, end + 2))
        if secs - end < 3 and secs - start <= CLIP_MAX_S:   # don't leave a tiny leftover at the end
            end = float(secs)
        if end - start > CLIP_MAX_S:
            end = start + CLIP_MAX_S
        if start < 1.5 and secs - end < 3:        # the whole clip already is the right part
            return raw
        out = tmp / f"{cid}_best.mp4"
        if not out.exists():
            viral.cut(raw, start, end, out)
        c["trim"] = [start, end]
        log(f"trimmed {cid}: {secs}s -> {start:.0f}-{end:.0f}s (moment at {p}s, {p - start:.0f}s in)")
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


REACT_TOP = 400   # react layout: where the watched screen starts (the hook sits above it, below TikTok's top bar)
SAFE_TOP = 150    # TikTok / Shorts put their top bar (Following | For You, search) over the top ~150 px of 1920
SAFE_BOTTOM = 300 # ...and the caption / username / music over the bottom ~300 px: keep faces and text out of both
REACT_GAP, REACT_BOTTOM, REACT_CAM_W = 24, 160, 1040   # react layout (Nov 25): webcam nearly full width, ending above most of TikTok's caption


def react_view(box, cam: str) -> str | None:
    """The part of the screen showing what the streamer is watching (AI box, 0-1000) - only if it's a sensible,
    big area that isn't just the streamer's own webcam."""
    try:
        y0, x0, y1, x1 = [max(0.0, min(1000.0, float(v))) / 1000 for v in box]
        if (x1 - x0) * (y1 - y0) < 0.12 or x1 - x0 < 0.2 or y1 - y0 < 0.3:
            return None
        cx, cy, cw, chh = [float(v) for v in cam.split(",")]
        ix = max(0.0, min(x1, cx + cw) - max(x0, cx)) * max(0.0, min(y1, cy + chh) - max(y0, cy))
        if ix > 0.6 * cw * chh and (x1 - x0) * (y1 - y0) < 2.5 * cw * chh:
            return None   # that's mostly the webcam itself
        return f"{x0:.4f},{y0:.4f},{x1 - x0:.4f},{y1 - y0:.4f}"
    except Exception:  # noqa: BLE001
        return None


def view_ok(frames, view: str, cam: str = "") -> bool:
    """Only show just the watched part if everything left out really is empty: blurred / plain side fill around a
    vertical TikTok. If there's real picture outside the AI's box in any frame (the watched video changed, or the
    AI's box was simply wrong - Nov 24: Nick Eh 30 was cut in half), keep the whole screen instead."""
    try:
        vx, vy, vw, vh = [float(v) for v in view.split(",")]
        cx = cy = cw = chh = -1.0
        if cam:
            cx, cy, cw, chh = [float(v) for v in cam.split(",")]
        for f in frames:
            H, W = f.shape[:2]
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
            det = np.abs(cv2.Laplacian(g, cv2.CV_32F))
            mask = np.zeros((H, W), bool)
            if cw > 0:
                mask[int(cy * H):int((cy + chh) * H), int(cx * W):int((cx + cw) * W)] = True   # the webcam isn't "content"
            inside = np.zeros((H, W), bool)
            inside[int(vy * H):int((vy + vh) * H), int(vx * W):int((vx + vw) * W)] = True
            out = ~inside & ~mask
            if out.sum() < 0.02 * H * W:
                continue
            d_in = float(det[inside & ~mask].mean()) if (inside & ~mask).any() else 0.0
            d_out = float(det[out].mean())
            if d_in <= 0 or d_out > 0.45 * d_in:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def cam_is_live(raw: Path, cam: str) -> bool:
    """A real webcam is never perfectly still (people move, cameras have noise). If the box looks the same in every
    frame, it's a picture in the stream overlay (art, a logo, a game character), not a camera."""
    try:
        x, y, w, h = [float(v) for v in cam.split(",")]
        cap = cv2.VideoCapture(str(raw))
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
        cap.release()
        crops = []
        for fr in _frames(raw, [max(0.3, dur * p) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]):
            H, W = fr.shape[:2]
            g = cv2.cvtColor(fr[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)], cv2.COLOR_BGR2GRAY)
            crops.append(cv2.resize(g, (96, 54)).astype(np.float32))
        if len(crops) < 3:
            return True
        change = max(float(np.mean(np.abs(a - b))) for a, b in zip(crops, crops[1:]))
        return change >= 1.5    # still pictures: ~0-1 (video compression only); real webcams: 3+
    except Exception:  # noqa: BLE001
        return True   # can't tell: don't throw the clip away


def find_bars(path: Path, secs=None) -> tuple[int, int, int, int] | None:
    """Phone screen recordings / re-uploads: flat bars (black OR dark-gray / blurred fill) around the real picture.
    A bar = edge columns/rows that are one flat colour and never change from frame to frame, while the picture
    inside moves. Returns the picture's (x, y, w, h) in the file's real pixels, or None if there are no bars."""
    cap = cv2.VideoCapture(str(path))
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS) or 30
    cap.release()
    if not W or not H:
        return None
    dur = n / fps if n else 30.0
    secs = secs or [max(0.3, dur * p) for p in (0.04, 0.16, 0.28, 0.4, 0.52, 0.64, 0.76, 0.88, 0.97)]
    cap = cv2.VideoCapture(str(path))
    gs = []
    for t in secs:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if ok:
            gs.append(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32))
    cap.release()
    if len(gs) < 3:
        return None
    g = np.stack(gs)                                  # frames x H x W
    # a bar line is darkish and has NO detail from end to end in every sampled frame. It may be a smooth gradient
    # and its shade may drift with the picture (iPhone recordings tint the bars) - so measure detail (how much
    # neighbouring pixels differ), not flatness. Real picture: >= 0.25; bars: ~0.03.
    col_ok = (np.abs(np.diff(g, axis=1)).mean(axis=1).max(axis=0) < 0.15) & (g.max(axis=1).max(axis=0) < 120)
    row_ok = (np.abs(np.diff(g, axis=2)).mean(axis=2).max(axis=0) < 0.15) & (g.max(axis=2).max(axis=0) < 120)

    def run(ok):
        k = 0
        while k < len(ok) and ok[k]:
            k += 1
        return k
    x0, x1 = run(col_ok), W - run(col_ok[::-1])
    y0, y1 = run(row_ok), H - run(row_ok[::-1])
    if x1 - x0 < 0.4 * W or y1 - y0 < 0.4 * H:
        return None                                    # a dark/still scene, not bars
    if x0 + (W - x1) < 0.03 * W and y0 + (H - y1) < 0.03 * H:
        return None                                    # no real bars
    pad = 2                                            # don't keep a soft edge of the bar
    x0, x1 = (x0 + pad if x0 else 0), (x1 - pad if x1 < W else W)
    y0, y1 = (y0 + pad if y0 else 0), (y1 - pad if y1 < H else H)
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    return x0, y0, w, h


_FILTERS = {}


def _has_filter(name: str) -> bool:
    if name not in _FILTERS:
        out = subprocess.run([FF, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        _FILTERS[name] = bool(re.search(rf"\s{re.escape(name)}\s", out))
    return _FILTERS[name]


def src_fps(path: Path) -> float:
    """Frame rate, the higher of the average and the container's rate (iPhone screen recordings are variable-rate:
    ~60 on average but stored as 120 - copying that would double every frame)."""
    cap = cv2.VideoCapture(str(path))
    f = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.release()
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"([\d.]+)k? tbr", err)
    return max(f, float(m.group(1)) if m else 0.0)


def is_hdr(path: Path) -> bool:
    """iPhone screen recordings (and phone camera video) are HDR (PQ / HLG). Copied straight into a normal video
    they look grey and washed out - they have to be converted."""
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    return bool(re.search(r"smpte2084|arib-std-b67", err))


# HDR -> normal (SDR) colours, as the recording looked on screen: the phone puts normal white at 203 nits
HDR_TO_SDR = ("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=clip:desat=0,"
              "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def bars_vf(src: Path, crop) -> str | None:
    """ffmpeg filter for cutting clips from an upload, all in one pass: bars off, 120 fps phone video down to 60,
    HDR converted to normal colours."""
    fps = src_fps(src)
    f = [f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}"] if crop else []
    if fps > 61:
        f.append("fps=60")
    if is_hdr(src):
        if _has_filter("zscale") and _has_filter("tonemap"):
            f.append(HDR_TO_SDR)
            log(f"{src.name}: HDR phone video -> converting to normal colours")
        else:
            log("HDR video but this ffmpeg can't convert it (no zscale) - colours may look washed out")
            f.append("format=yuv420p")
    return ",".join(f) or None


def crop_bars(raw: Path, tmp: Path, crop: tuple | None = None) -> list | None:
    """Cut the bars off (crop from find_bars, worked out once for a whole upload; or found on this clip).
    Replaces the file; returns the crop [x, y, w, h] it used (saved with the clip, so a re-fetch matches)."""
    try:
        crop = crop or find_bars(raw)
        if not crop:
            return None
        x, y, w, h = crop
        tmp_out = tmp / f"{raw.stem}_nobars.mp4"
        fps = src_fps(raw)
        ff("-i", str(raw), "-vf", f"crop={w}:{h}:{x}:{y}" + (",fps=60" if fps > 61 else "")   # 120 fps phone video -> 60
           + ("," + HDR_TO_SDR if is_hdr(raw) and _has_filter("zscale") else ""),
           "-c:v", "libx264", "-preset", "fast", "-crf", "14",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp_out))
        shutil.move(str(tmp_out), raw)
        log(f"{raw.stem}: cut off the bars around the stream -> {w}x{h}")
        return [int(x), int(y), int(w), int(h)]
    except Exception as e:  # noqa: BLE001
        log(f"bar check skipped {raw.stem}: {e}")
        return None


def _pool_frames(paths, n: int = 24, crop: tuple | None = None):
    """Frames spread across one or more videos (a whole upload / all clips from it), optionally cropped."""
    paths = [p for p in paths if p and Path(p).exists()]
    if not paths:
        return []
    per = max(3, -(-n // len(paths)))
    out = []
    for p in paths:
        d = duration_of(Path(p))
        secs = [max(0.4, d * (k + 0.5) / per) for k in range(per)]
        cap = cv2.VideoCapture(str(p))
        for t in secs:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if not ok:
                continue
            if crop:
                x, y, w, h = crop
                fr = fr[y:y + h, x:x + w]
            if fr.shape[1] > 1280:
                fr = cv2.resize(fr, (1280, int(fr.shape[0] * 1280 / fr.shape[1])), interpolation=cv2.INTER_AREA)
            out.append(fr)
        cap.release()
    return out


def owner_cam(frames) -> tuple[str, str] | tuple[None, None]:
    """The person whose stream it is: in a reaction stream there are TWO+ faces (the streamer's webcam and the
    creator in the video they're watching). The streamer's own webcam is part of their stream overlay, so their
    face sits in the SAME spot in nearly every frame of the whole video; faces in the watched videos change
    size and place from video to video. Works for webcams with no border (cut-outs) too.
    Returns (cam box, 'owner'/'owner-edge') or (None, None)."""
    if len(frames) < 6:
        return None, None
    H, W = frames[0].shape[:2]
    per = []
    for f in frames:
        try:
            per.append([(x, y, w, h) for (x, y, w, h, sc) in _yunet_faces(f, 0.7) if h >= 0.05 * H])
        except Exception:  # noqa: BLE001
            per.append([])
    best, best_hits, best_edge = None, 0, 0.0
    for i, faces in enumerate(per):
        for (x, y, w, h) in faces:
            cx, cy = x + w / 2, y + h / 2
            hits = 1 + sum(1 for j, o in enumerate(per) if j != i and any(
                abs(ox + ow / 2 - cx) < 0.6 * w and abs(oy + oh / 2 - cy) < 0.6 * h and 0.65 < ow / w < 1.5
                for (ox, oy, ow, oh) in o))
            edge = max(abs(cx / W - 0.5), abs(cy / H - 0.5))   # 0 = dead centre, 0.5 = at an edge
            if hits > best_hits + 1 or (hits >= best_hits - 1 and best and edge > best_edge + 0.08):
                best, best_hits, best_edge = (x, y, w, h), hits, edge
    if not best or best_hits < max(4, int(0.5 * len(frames))):
        return None, None    # no face that's always in the same place
    # median spot of that face across the frames (steadier than one detection)
    cx0, cy0 = best[0] + best[2] / 2, best[1] + best[3] / 2
    same = [f for faces in per for f in faces
            if abs(f[0] + f[2] / 2 - cx0) < 0.6 * best[2] and abs(f[1] + f[3] / 2 - cy0) < 0.6 * best[3]]
    face = tuple(float(np.median([f[k] for f in same])) for k in range(4))
    if not 0.06 <= face[3] / H <= 0.45:
        return None, None
    iface = tuple(int(v) for v in face)
    try:
        rect = _overlay_rect(frames, iface)    # a real webcam border around it -> use exactly that box
        if rect:
            return _fit(rect, iface, W, H), "owner-edge"
    except Exception:  # noqa: BLE001
        pass
    return _guess_box(face, W, H, 3.1, 0.35), "owner"


def face_in_box(frames, cam: str) -> int:
    """How many frames have a face inside this box."""
    x, y, w, h = [float(v) for v in cam.split(",")]
    n = 0
    for f in frames:
        H, W = f.shape[:2]
        crop = f[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
        if crop.size and _yunet_faces(cv2.resize(crop, None, fx=1.5, fy=1.5), 0.6):
            n += 1
    return n


def upload_cams(raws: list, log_name: str = "") -> tuple[str | None, str]:
    """Your upload: the streamer's own webcam, found across ALL the clips cut from it (more frames = the face
    that never moves stands out from the faces in the videos they're watching). Returns (box, quality):
    'owner' = seen in the same spot across several clips (trusted over the AI's pick), 'owner1' = one clip only."""
    try:
        cam, q = owner_cam(_pool_frames(raws, 30))
        if cam:
            q = "owner" if len(raws) >= 2 else "owner1"
            log(f"{log_name}: the streamer's own webcam is at {cam} ({q})")
        return cam, q or ""
    except Exception as e:  # noqa: BLE001
        log(f"{log_name}: webcam search failed: {e}")
        return None, ""


def other_faces(frames, cam: str) -> int:
    """How many frames show a clear face OUTSIDE the streamer's webcam box (someone in the video they watch)."""
    try:
        x, y, w, h = [float(v) for v in cam.split(",")]
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for f in frames:
        H, W = f.shape[:2]
        for (fx, fy, fw, fh, sc) in _yunet_faces(f, 0.75):
            cx, cy = (fx + fw / 2) / W, (fy + fh / 2) / H
            if fh >= 0.06 * H and not (x <= cx <= x + w and y <= cy <= y + h):
                n += 1
                break
    return n


def widen_cam(frames, box: str) -> str:
    """The AI's webcam box is often just the face: grow it to head-and-shoulders (the whole webcam), so it isn't a
    blurry zoom on a nose."""
    try:
        x, y, w, h = [float(v) for v in box.split(",")]
        if w >= 0.2 and h >= 0.25:
            return box
        f = frames[0]
        H, W = f.shape[:2]
        faces = [(fx, fy, fw, fh) for (fx, fy, fw, fh, sc) in _yunet_faces(f, 0.6)
                 if x * W <= fx + fw / 2 <= (x + w) * W and y * H <= fy + fh / 2 <= (y + h) * H]
        if not faces:
            return box
        face = max(faces, key=lambda r: r[2] * r[3])
        return _guess_box(face, W, H, 3.1, 0.35)
    except Exception:  # noqa: BLE001
        return box


def small_cam(cam: str) -> bool:
    """A small webcam box (under ~15% of the screen's width): fine on top of gameplay, too small for the
    reaction layout's big webcam (Nov 25: Kai's tiny GTA webcam ended up a small box in a big empty band)."""
    try:
        return float(cam.split(",")[2]) < 0.15
    except Exception:  # noqa: BLE001
        return False


def same_spot(a: str, b: str) -> bool:
    """Two webcam boxes point at the same webcam (each one's centre is inside the other)."""
    try:
        ax, ay, aw, ah = [float(v) for v in a.split(",")]
        bx, by, bw, bh = [float(v) for v in b.split(",")]
    except Exception:  # noqa: BLE001
        return False
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    return (bx <= acx <= bx + bw and by <= acy <= by + bh) or (ax <= bcx <= ax + aw and ay <= bcy <= ay + ah)


def react_cam(frames, box) -> str | None:
    """The channel owner's webcam in a react clip, from the AI's box ([ymin, xmin, ymax, xmax], 0-1000).
    Telling the two webcams apart: the streamer's own webcam is part of their stream overlay, so there's a face in
    that SAME spot in most of the clip; a face inside the watched video (another creator's webcam / TikTok) moves
    around or comes and goes. So it's only used if a face is inside it in at least 3 of 4 frames."""
    try:
        y0, x0, y1, x1 = [max(0.0, min(1000.0, float(v))) / 1000 for v in box]
        if x1 - x0 < 0.06 or y1 - y0 < 0.06 or (x1 - x0) * (y1 - y0) > 0.45:
            return None   # too small to be a webcam / so big it's the whole screen
        pad = 0.01
        x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(1, x1 + pad), min(1, y1 + pad)
        frames = [f for f in (frames if isinstance(frames, list) else [frames]) if f is not None]
        if frames:
            hits = 0
            for fr in frames:
                H, W = fr.shape[:2]
                crop = fr[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]
                if crop.size and _yunet_faces(cv2.resize(crop, None, fx=2, fy=2), 0.6):
                    hits += 1
            if hits < max(1, -(-3 * len(frames) // 4)):   # face there in at least 3 of every 4 frames
                return None
        return f"{x0:.4f},{y0:.4f},{x1 - x0:.4f},{y1 - y0:.4f}"
    except Exception:  # noqa: BLE001
        return None


def react_geometry(raw: Path, w: float, h: float, view: str | None = None) -> dict:
    """React layout sizes (1080x1920) - the look you liked (Nov 24 morning): the hook, what the streamer is
    watching right under it, then the streamer's webcam BIG - nearly the full width, down close to the bottom.
    The webcam comes first (the reaction is the point): it's sized full-width (never past 2.6x its real pixels, so
    the face stays sharp) and what they're watching gets the rest (at least 40% of the space). The face sits in
    the middle of the webcam, above TikTok's caption - only shoulders / desk go under it."""
    cap = cv2.VideoCapture(str(raw))
    W = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920
    H = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080
    cap.release()
    vx, vy, vw, vh = [float(v) for v in view.split(",")] if view else (0.0, 0.0, 1.0, 1.0)
    pw, ph = max(2.0, W * w), max(2.0, H * h)                 # webcam pixels
    gap, bottom = REACT_GAP, REACT_BOTTOM
    avail = 1920 - REACT_TOP - gap - bottom
    ch_full = 1080 * (H * vh) / (W * vw)                      # what they watch, at full width
    fh_cap = min(REACT_CAM_W * ph / pw, ph * 2.6)             # webcam nearly full width, but sharp
    ch = min(ch_full, max(avail - fh_cap, 0.40 * avail))
    fh = min(fh_cap, avail - ch)
    cw = min(1080.0, ch * (W * vw) / (H * vh))
    fw = min(float(REACT_CAM_W), fh * pw / ph)
    cw, ch, fw, fh = [int(v) // 2 * 2 for v in (cw, ch, fw, fh)]
    ct = REACT_TOP                                            # fixed: the site's editor puts the hook right above it
    cb = ct + ch + gap
    return {"vx": vx, "vy": vy, "vw": vw, "vh": vh, "cw": cw, "ch": ch, "ct": ct, "cb": cb, "fw": fw, "fh": fh}


def render_final(raw: Path, cid: str, cam: str, hook: str | None, tmp: Path, out: str = "_final",
                 layout: str = "split", cut: float = 0.0, cam_video: Path | None = None,
                 hook_y: float | None = None, zoom: float = 1.0, pan: float = 0.0,
                 hook_style: str = "white", hook_scale: float = 1.0, top_h: int | None = None,
                 subs: list | None = None, punch_at: float | None = None, view: str | None = None,
                 cta: bool = False, pbar: bool = False) -> None:
    """Split screen (webcam top, gameplay bottom). hook=None makes the clean copy with no caption box.
    No subtitles on any video (Nov 24): whatever is passed in, none are drawn.
    layout="full": no webcam in the video -> the whole frame centred on a blurred background."""
    subs = None               # NO subtitles, ever
    T = int(top_h or TOP_H)   # webcam strip height (editor can make the webcam bigger or smaller)
    info: dict = {"layout": layout}
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
                "boxblur=20:2,setsar=1[bg];[b]scale=1080:-2:flags=lanczos,setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    elif layout == "react":
        # REACTION clips (Nov 24): the streamer watching someone else's stream / video. Nothing is cut off:
        # the WHOLE screen (what they're watching) full-width on top, the hook above it, and the reacting
        # streamer's own webcam - the whole box - big underneath, on a blurred background.
        g = react_geometry(raw, w, h, view)
        cb, fw, fh = g["cb"], g["fw"], g["fh"]
        info["cam_px"] = [int(fw), int(fh)]
        punch = ""
        if punch_at is not None:
            a, b = punch_at - 0.15, punch_at + 2.0
            punch = (f",fps=30,zoompan=z='1+0.10*max(0,min(1,min((it-{a:.2f})/0.2,({b:.2f}-it)/0.4)))'"
                     f":x='iw/2-(iw/zoom/2)':y='ih/2.4-(ih/zoom/2.4)':d=1:s={fw}x{fh}:fps=30")
        # background = a blur of the streamer's WEBCAM (Nov 25 pm): never black - a dark stream / phone recording's
        # black bars made black bands above and under the webcam with a blur of what they watch
        base = ("[0:v]split=3[a][b][c];"
                f"[a]crop=w=iw*{w * 0.7:.4f}:h=ih*{h * 0.7:.4f}:x=iw*{x + w * 0.15:.4f}:y=ih*{y + h * 0.15:.4f},"   # its middle: no edge bars
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:2,eq=brightness=-0.1,setsar=1[bg];"
                f"[b]crop=w=iw*{g['vw']}:h=ih*{g['vh']}:x=iw*{g['vx']}:y=ih*{g['vy']},scale={g['cw']}:{g['ch']}:flags=lanczos,setsar=1[ct];"
                f"[c]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},hqdn3d=1.2:1.2:3:3,"
                f"scale={fw}:{fh}:flags=lanczos,unsharp=5:5:0.7:5:5:0.0,setsar=1{punch}[cm];"
                f"[bg][ct]overlay=(W-w)/2:{g['ct']}[t1];[t1][cm]overlay=(W-w)/2:{cb}")
    else:
        # webcam: the WHOLE webcam box (nothing cropped off), only as big as its pixels allow so it stays sharp
        # (max ~3.6x with the AI-upscaled webcam, 2.8x without), centred on a blurred copy of itself
        fw, fh = _cam_fit(raw, w, h, T, 3.6 if cam_video else 2.8)
        info["cam_px"] = [int(fw), int(fh)]
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
        from PIL import Image as _Im
        hw, hh = _Im.open(png).size
        yy = (1920 * hook_y - hh / 2 if hook_y is not None                       # your own spot (editor)
              else (1920 - hh) / 2 if layout == "full" else 1920 * 0.2 - hh / 2 if layout == "fill"
              else 1920 * 0.6 if layout.startswith("irl")                         # irl: under the faces
              else max(SAFE_TOP, REACT_TOP - hh - 26) if layout == "react"        # react: just above what they watch
              else max(150, _cam_fg_top(raw, w, h) - hh - 28) if layout == "cam"  # cam: just above the webcam
              else T - hh * 0.2)                                                  # split: on the line
        yy = max(SAFE_TOP, min(1920 - hh - SAFE_BOTTOM, yy))   # never under TikTok / Shorts' own top bar or caption
        oy = str(int(yy))
        info["hook_box"] = [int((1080 - hw) / 2), int(yy), int(hw), int(hh)]
    else:
        inputs = ["-i", str(raw)] + extra
    flt = base + "[v0]"
    last = "v0"
    # word-by-word subtitles (WhisperX-style): 1-3 words at a time, big white outlined letters
    if subs:
        from render_hook import render_sub, sub_chunks
        _rg = react_geometry(raw, w, h, view) if layout == "react" else {}
        sy = (f"{T}+({1920 - T})*0.62-h/2" if layout == "split"
              else f"'min(H-h-50,{_rg['cb'] + int(_rg['fh'] * 0.80)}-h/2)'" if layout == "react"
              else "H*0.74-h/2")
        for i, (a, b, txt) in enumerate(sub_chunks(subs)[:60]):
            k = len(inputs) // 2
            inputs += ["-i", render_sub(txt, str(tmp / f"{cid}_sub{i}.png"))]
            flt += f";[{last}][{k}:v]overlay=(W-w)/2:{sy}:enable='between(t,{a:.2f},{b:.2f})'[s{i}]"
            last = f"s{i}"
    if hook:
        flt += f";[{last}][1:v]overlay=(W-w)/2:{oy}[vh]"
        last = "vh"
        if cta or pbar:   # follow pop-up at the start + progress bar (engage.py) - posted videos only
            try:
                import engage
                inputs, flt, last = engage.add(inputs, flt, last, duration_of(raw), tmp, float(oy), float(hh),
                                               layout, cta=cta, pbar=pbar)
                info["cta"], info["pbar"] = cta, pbar
                if cta and has_audio(raw) and duration_of(raw) > 4:   # the pop sound + a small dip of the clip's sound
                    inputs, flt, alab = engage.add_audio(inputs, flt, tmp)
                    flt += f";[{alab}]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
                    info["pop"] = True
            except Exception as e:  # noqa: BLE001
                log(f"follow pop-up skipped for {cid}: {e}")
    flt += f";[{last}]format=yuv420p[out]"
    # clean, even sound (FFmpeg loudnorm to TikTok/YouTube's -14 LUFS): no more too-quiet or blaring clips
    out_fps = 60 if src_fps(raw) >= 50 else 30   # keep 60 fps gameplay smooth (TikTok + Shorts play 60 fps)
    amap = (["-map", "[aout]"] if info.get("pop") else
            ["-map", "0:a?", *(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000"] if has_audio(raw) else [])])
    ff(*inputs, "-filter_complex", flt, "-map", "[out]", *amap,
       "-c:v", "libx264", "-preset", os.environ.get("X264_PRESET", "medium"), *enc_args(out), "-r", str(out_fps),   # medium: best detail per bit
       "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(VID / f"{cid}{out}.mp4"))
    return info


def make_wide(cid: str, c: dict) -> bool:
    """Small 16:9 preview of ALL the footage this clip has (the whole Twitch clip / your video around the moment), so
    the Studio's Trim shows the real picture when you move the start earlier or the end later."""
    raw = RAW / f"{cid}.mp4"
    out = VID / f"{cid}_wide.mp4"
    if not raw.exists() or c.get("no_wide"):
        return False
    up = c.get("source") == "upload"
    mom = c.get("moment") or {}
    win0 = float((c.get("raw_win") or [mom.get("start", 0)])[0]) if up else 0.0
    rs = duration_of(raw)
    key = f"q2-{win0:.2f}-{rs:.1f}-{raw.stat().st_size}"   # q2: sharper preview (Nov 25: the first one was too soft)
    if out.exists() and c.get("wide", {}).get("k") == key:
        return False
    ff("-i", str(raw), "-vf", "scale=854:-2:flags=lanczos,fps=30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
       "-maxrate", "1200k", "-bufsize", "2400k", "-g", "30",       # a keyframe every second: jumps land instantly
       "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out))
    c["wide"] = {"k": key, "t0": round(win0, 2), "t1": round(win0 + rs, 2)}
    return True


ED_REL = "site-big"   # the live editor's full-quality footage: a release file, served by the site as v/ed/<name>
_ED_REL_CACHE: dict = {}


def _ed_assets() -> list:
    """Every file on the editor-footage release (all pages), read once per run."""
    if "assets" not in _ED_REL_CACHE:
        code, rel = _gh("GET", f"/repos/{REPO}/releases/tags/{ED_REL}")
        _ED_REL_CACHE["rel"] = rel if code == 200 else {}
        out = []
        if code == 200:
            for page in range(1, 30):
                c2, items = _gh("GET", f"/repos/{REPO}/releases/{rel['id']}/assets?per_page=100&page={page}")
                if c2 != 200 or not isinstance(items, list):
                    break
                out += items
                if len(items) < 100:
                    break
        _ED_REL_CACHE["assets"] = out
    return _ED_REL_CACHE["assets"]


def make_ed(cid: str, c: dict) -> bool:
    """Full-quality copy of ALL this clip's footage for the Studio's live editor (Nov 25): the editor plays it under
    your edit like CapCut / TikTok, and your phone makes the finished video from it in seconds. Source size and frame
    rate (up to 1920 wide, 60 fps), a keyframe every half second so jumps are instant. It lives on the 'site-big'
    release and the site serves it at v/ed/<cid>_<key>.mp4 (the Cloudflare function passes it through)."""
    raw = RAW / f"{cid}.mp4"
    up = c.get("source") == "upload"
    if GH_TOKEN and not raw.exists() and up and c.get("raw_asset"):
        try:   # your clip's footage from its saved copy (fast)
            raw_get(c, raw, cid)
        except Exception:  # noqa: BLE001
            pass
    if not GH_TOKEN or not raw.exists() or c.get("asis"):
        return False
    mom = c.get("moment") or {}
    win0 = float((c.get("raw_win") or [mom.get("start", 0)])[0]) if up else 0.0
    rs = duration_of(raw)
    key = hashlib.sha1(f"e1-{win0:.2f}-{rs:.1f}-{raw.stat().st_size}".encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", cid)
    have = {x["name"] for x in _ed_assets()}
    if (c.get("ed") or {}).get("k") == key and f"v__ed__{safe}_{key}.mp4" in have:
        return False
    fps = 60 if src_fps(raw) >= 50 else 30
    out = RAW.parent / "_ed" / f"{safe}_{key}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    ff("-i", str(raw), "-vf", "scale='min(1920,iw)':-2:flags=lanczos,format=yuv420p", "-r", str(fps),
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-maxrate", "14M", "-bufsize", "28M", "-profile:v", "high",
       "-g", str(fps // 2), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out))
    import requests
    rel = _ED_REL_CACHE.get("rel") or {}
    if not rel.get("id"):
        code, rel = _gh("POST", f"/repos/{REPO}/releases", {"tag_name": ED_REL, "name": "Site files over 25 MB",
                                                             "body": "Served by the site. Managed automatically.",
                                                             "prerelease": True, "target_commitish": "main"})
    name = f"v__ed__{safe}_{key}.mp4"
    for old in [x for x in _ed_assets() if x["name"].startswith(f"v__ed__{safe}_")]:
        _gh("DELETE", f"/repos/{REPO}/releases/assets/{old['id']}")
    with open(out, "rb") as fh:
        r = requests.post(f"https://uploads.github.com/repos/{REPO}/releases/{rel['id']}/assets?name={name}", data=fh,
                          timeout=600, headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "video/mp4",
                                                "Accept": "application/vnd.github+json"})
    out.unlink(missing_ok=True)
    if r.status_code not in (200, 201):
        log(f"editor footage for {cid} not saved: HTTP {r.status_code}")
        return False
    c["ed"] = {"k": key, "t0": round(win0, 2), "t1": round(win0 + rs, 2), "path": f"v/ed/{safe}_{key}.mp4"}
    return True


def view_check(video: Path, lay: str, info: dict | None) -> tuple[bool, str]:
    """Would this look right to someone scrolling TikTok / Shorts? Checked on the finished video before it can go out:
    - the hook is fully visible (not under the app's top bar / caption, not a wall of text)
    - the streamer's face is clearly in view (not cut off, covered or tiny) when the layout shows a webcam
    - the picture isn't black / far too dark
    Returns (ok, what's wrong)."""
    probs = []
    hb = (info or {}).get("hook_box")
    if hb:
        if hb[1] < SAFE_TOP - 1 or hb[1] + hb[3] > 1920 - SAFE_BOTTOM + 1:
            probs.append("the hook is under TikTok's buttons")
        if hb[3] > 340:
            probs.append("the hook is too long (4+ lines)")
    cap = cv2.VideoCapture(str(video))
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = []
    for p in (0.15, 0.35, 0.55, 0.75, 0.9):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * p))
        ok, fr = cap.read()
        if ok:
            frames.append(fr)
    cap.release()
    if not frames:
        return False, "the video won't open"
    if max(float(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean()) for f in frames) < 22:
        probs.append("the picture is black / too dark")
    else:   # Nov 25: a dead black band (an empty gap in the layout) anywhere on screen, in most frames
        bands = []
        for f in frames:
            g_ = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            H_ = g_.shape[0]
            # only the part people actually see: TikTok / Shorts cover the top bar and the caption area anyway
            g_ = g_[int(H_ * SAFE_TOP / 1920):int(H_ * (1920 - SAFE_BOTTOM) / 1920)]
            rows = (g_.mean(axis=1) < 10) & (g_.std(axis=1) < 6)
            run, best = 0, 0
            for v in rows:
                run = run + 1 if v else 0
                best = max(best, run)
            bands.append(best / H_)
        if sum(1 for b in bands if b >= 0.07) >= 2:   # >= ~135 px of pure black in 2+ of the frames checked
            probs.append(f"a black empty gap in the layout ({int(max(bands) * 1920)} px)")
    cp = (info or {}).get("cam_px")
    if cp and lay in ("split", "react") and cp[0] < 500 and cp[1] < 400:
        probs.append("the webcam is too small to see (low-quality webcam)")
    if lay == "split":   # the game part is a blank page / plain screen (a document, a desktop) most of the time
        flat = 0
        for f in frames:
            Hh = f.shape[0]
            sc = Hh / 1920
            reg = f[int(TOP_H * sc):int((1920 - SAFE_BOTTOM) * sc)].reshape(-1, 3).astype(np.int16)
            med = np.median(reg, axis=0)
            if float((np.abs(reg - med).max(axis=1) < 14).mean()) > 0.85:
                flat += 1
        if flat >= 3:
            probs.append("the game part is a blank / plain screen")
    if lay in ("split", "react", "cam") or lay.startswith("irl"):
        good = 0
        for f in frames:
            Hh, Ww = f.shape[:2]
            sc = 1920 / Hh
            for (x, y, w, h, scr) in _yunet_faces(f, 0.6):
                cx, cy, fh = (x + w / 2) * sc, (y + h / 2) * sc, h * sc
                if fh >= 60 and SAFE_TOP - 40 <= cy <= 1920 - SAFE_BOTTOM + 120 and 40 <= cx <= 1040:
                    good += 1
                    break
        if good < 2:
            probs.append("the streamer's face isn't clearly in view")
    return (not probs), "; ".join(probs)


def enc_args(out: str) -> list:
    """Video quality for saved clips. Posted copy: CRF 20, max 6 Mbps - TikTok re-compresses every upload to ~2-3 Mbps,
    so this looks the same on a phone (the old CRF 17 / 12 Mbps made ~27 MB files and the site only fit ~10 clips).
    The no-caption copy is only a working copy for edits / saving without the hook: a bit lighter."""
    # Nov 25: no more 1 GB site -> sharper again (the no-caption copy is what edits are made from, so it gets the
    # same quality - every re-encode of a soft copy looked worse)
    # Nov 25 (2): highest quality - near-lossless to the eye (CRF 16, up to 16 Mbps, High profile). TikTok / YouTube
    # re-compress every upload, so the cleaner the file we give them, the better it still looks after.
    return ["-crf", "16", "-maxrate", "16000k", "-bufsize", "32M", "-profile:v", "high"]


def shrink_site_videos(st: dict) -> None:
    """One time (Nov 24): re-save the videos already on the site at the new sizes - same look, about half the space."""
    if st.get("shrink_v", 0) >= 1:
        return
    done, saved = 0, 0.0
    for f in sorted(VID.rglob("*.mp4")):
        if not MINE and "m" in f.relative_to(VID).parts[:-1]:
            continue   # your clips' folder belongs to the your-clips maker
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


UPLOAD_MAX_CLIPS = 4   # funniest moments made from one pasted link / upload
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


_UPLOAD_SRC = {}   # upload commit -> the whole video, put back together (once per run)


def _name_tokens() -> list[tuple[str, list[str]]]:
    out = []
    for login, s_ in (CFG.get("streamers") or {}).items():
        nm = (s_ or {}).get("name") or login
        common = {"stable", "adapt", "the", "gaming"}   # everyday words: a hook using them isn't naming anyone
        toks = [t for t in re.findall(r"[a-z]+", nm.lower()) if len(t) >= 3 and t not in common]
        if login.lower() not in common:
            toks.append(login.lower())
        if toks:
            out.append((nm, toks))
    return out


def names_ok(hook: str, allowed: str) -> bool:
    """A hook may only name a streamer who is in what the AI was given (the clip's streamer, what was said, the
    title, who they're watching). Stops the AI inventing 'Lacy' for a clip of someone else when the name is unknown."""
    h, a = (hook or "").lower(), (allowed or "").lower()
    for nm, toks in _name_tokens():
        hit = [t for t in toks if re.search(rf"\b{re.escape(t)}\b", h)]
        if hit and not any(re.search(rf"\b{re.escape(t)}\b", a) for t in toks):
            return False
    return True


def hook_context(c: dict) -> str:
    return " ".join(str(c.get(k) or "") for k in ("streamer", "login", "transcript", "title", "watching", "hook"))


def upload_source(c: dict, cid: str, tmp: Path) -> Path:
    """An uploaded FILE again, from the commit its pieces were uploaded in. The upload branch is deleted after the
    clips are made, but GitHub keeps the commit - we find it from the branch's history if it wasn't saved."""
    jid = re.sub(r"^up|_\d+$", "", cid)
    sha = c.get("src_commit") or ""
    if not sha:
        code, acts = _gh("GET", f"/repos/{REPO}/activity?ref=refs/heads/upload-{jid}&per_page=20")
        for a in (acts if isinstance(acts, list) else []):
            if a.get("activity_type") in ("branch_creation", "push") and a.get("after", "0" * 40) != "0" * 40:
                sha = a["after"]
                break
    if not sha:
        raise RuntimeError("no link saved (uploaded file) and the upload's commit is gone")
    c["src_commit"] = sha
    if sha in _UPLOAD_SRC and _UPLOAD_SRC[sha].exists():
        return _UPLOAD_SRC[sha]
    subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", sha], check=True, timeout=1800)   # by commit id
    blobs = subprocess.run(["git", "ls-tree", "-r", "--name-only", sha], capture_output=True, text=True,
                           check=True).stdout.split()
    parts = sorted(p for p in blobs if p.startswith(f"uploads/{jid}/")) or sorted(blobs)
    src = tmp / f"upload_{jid}_again.mp4"
    with open(src, "wb") as fh:
        for part in parts:
            fh.write(subprocess.run(["git", "show", f"{sha}:{part}"], capture_output=True, check=True).stdout)
    log(f"got your uploaded video {jid} back ({src.stat().st_size // 1_000_000} MB)")
    _UPLOAD_SRC[sha] = src
    return src


RAW_REL = "raws"   # your clips' footage, ready to use (colours fixed, bars off) - so a re-make after an edit is fast


def _raw_key(c: dict) -> str:
    return hashlib.sha1(json.dumps([c.get("raw_win"), c.get("bars"), c.get("src_commit"), c.get("src_link"), 1]).encode()).hexdigest()[:10]


def raw_get(c: dict, raw: Path, cid: str) -> bool:
    """The saved footage from the 'raws' release (seconds), instead of the whole upload again + colour fixing (minutes)."""
    a = c.get("raw_asset") or {}
    if not a or a.get("key") != _raw_key(c):
        return False
    try:
        import requests
        with requests.get(f"https://github.com/{REPO}/releases/download/{RAW_REL}/{a['name']}", stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp_raw = raw.with_suffix(".dl.mp4")
            with open(tmp_raw, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        if tmp_raw.stat().st_size != a.get("size"):
            tmp_raw.unlink(missing_ok=True)
            return False
        tmp_raw.replace(raw)
        log(f"got your clip's footage from the saved copy (fast): {cid}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"saved footage copy not available for {cid}: {e}")
        return False


def raw_put(c: dict, raw: Path, cid: str) -> None:
    """Keep this clip's footage on the 'raws' release so the next edit doesn't redo the slow part."""
    if not GH_TOKEN or not raw.exists() or raw.stat().st_size > 1_900_000_000:
        return
    key = _raw_key(c)
    if (c.get("raw_asset") or {}).get("key") == key:
        return
    try:
        import requests
        code, rel = _gh("GET", f"/repos/{REPO}/releases/tags/{RAW_REL}")
        if code != 200:
            code, rel = _gh("POST", f"/repos/{REPO}/releases", {"tag_name": RAW_REL, "name": "Your clips' footage (for fast edits)",
                                                                 "body": "Working copies used when a clip is re-made after an edit. Managed automatically.",
                                                                 "prerelease": True, "target_commitish": "main"})
        name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', cid)}_{key}.mp4"
        for old in [x for x in (rel.get("assets") or []) if x["name"].startswith(re.sub(r'[^A-Za-z0-9_.-]', '_', cid) + "_")]:
            _gh("DELETE", f"/repos/{REPO}/releases/assets/{old['id']}")
        with open(raw, "rb") as fh:
            r = requests.post(f"https://uploads.github.com/repos/{REPO}/releases/{rel['id']}/assets?name={name}", data=fh,
                              timeout=600, headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "video/mp4",
                                                    "Accept": "application/vnd.github+json"})
        if r.status_code in (200, 201):
            c["raw_asset"] = {"name": name, "key": key, "size": raw.stat().st_size}
            log(f"saved {cid}'s footage for fast edits ({raw.stat().st_size // 1_000_000} MB)")
        else:
            log(f"couldn't save {cid}'s footage for fast edits: HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        log(f"couldn't save {cid}'s footage for fast edits: {e}")


def refetch_upload(c: dict, raw: Path, tmp: Path, cid: str = "") -> None:
    """Get one of your clips again (to re-make it after an edit): same link / uploaded file, same start and end,
    same bars cut off (so the webcam box still fits). From the saved copy when there is one (fast)."""
    if raw_get(c, raw, cid or raw.stem):
        return
    _refetch_slow(c, raw, tmp, cid)
    raw_put(c, raw, cid or raw.stem)


def _refetch_slow(c: dict, raw: Path, tmp: Path, cid: str = "") -> None:
    link = c.get("src_link") or ""
    a, b = [float(v) for v in (c.get("raw_win") or [c["moment"]["start"], c["moment"]["end"]])]   # the saved copy's span
    if not link:
        import viral
        src = upload_source(c, cid or raw.stem, tmp)
        c["src_s"] = round(duration_of(src), 1)
        a, b = max(0.0, a), min(c["src_s"], b)
        viral.cut(src, a, b, raw, vf=bars_vf(src, c.get("bars")))   # same bars off as when it was made
        log(f"got your clip again: {raw.stem}")
        return
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
    if c.get("bars"):
        crop_bars(raw, tmp, tuple(c["bars"]))
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
                                                              else "Got your video — finding the funniest moments, same as the Twitch clips (usually 5–15 min)."})
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
                if duration_of(src) <= 30:   # already a tiny clip - nothing to cut: use it whole, in our style
                    job["asis"] = True
            elif link:
                src, offset, title = fetch_link_audio(link, job.get("section"), tmp)
                job.setdefault("name", title)
                if not job.get("name") or job["name"] == "Link":
                    job["name"] = title
            else:
                subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", f"{ref}:refs/remotes/origin/{ref}"], check=True)
                job["src_commit"] = subprocess.run(["git", "rev-parse", f"origin/{ref}"], capture_output=True, text=True,
                                                   check=True).stdout.strip()
                with open(src, "wb") as fh:
                    for part in parts:
                        fh.write(subprocess.run(["git", "show", f"origin/{ref}:{part}"], capture_output=True, check=True).stdout)
            if job.get("asis"):   # a finished clip (e.g. from Higgsfield's YouTube clipper): use the whole thing
                d0 = duration_of(src)
                moments = [{"start": 0.0, "end": d0, "score": 1.0, "reasons": ["finished clip"],
                            "text": transcript(src) if d0 < 180 else "", "hook": (job.get("hook") or "").strip()}]
                if moments[0]["hook"]:
                    moments[0]["hook_ai"] = True
            else:   # your pasted link / upload: made exactly like the Twitch clips (Nov 24) - find the funniest
                    # moments (laughs, yelling, big reactions), cut each to the best ~16-22 s with the peak mid-clip,
                    # AI hook from what's said and shown. Up to 4 clips per video so one video can't flood the queue.
                moments = viral.find_moments(src, max_clips=UPLOAD_MAX_CLIPS, log=log)[:UPLOAD_MAX_CLIPS]
                if not moments:
                    raise RuntimeError("couldn't find any moments with talking or reactions in it")
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
        # 1) cut every moment; bars around the stream (phone screen recordings) come off first
        bars = None
        if not link and not job.get("asis"):
            try:
                bars = find_bars(src)
            except Exception as e:  # noqa: BLE001
                log(f"bar check failed: {e}")
        cut_ok, crops, wins = {}, {}, {}
        src_dur = 0.0
        if not link:
            try:
                src_dur = duration_of(src)
            except Exception:  # noqa: BLE001
                pass
        for i, m in enumerate(moments):
            cid = f"up{job.get('id', num)}_{i + 1}"
            raw = RAW / f"{cid}.mp4"
            # the saved copy covers ~20 s more on each side, so the Studio's Trim can show (and use) what's around it
            pad = 0 if (job.get("asis") or m.get("part") or m.get("full")) else 20.0
            wa = max(0.0, float(m["start"]) - pad)
            wb = float(m["end"]) + pad
            if src_dur:
                wb = min(src_dur, wb)
            wins[i] = [round(wa, 2), round(wb, 2)]
            try:
                import viral
                if link:
                    fetch_link_section(link, offset + wa, offset + wb, raw, tmp)
                    if not job.get("asis"):
                        crops[i] = crop_bars(raw, tmp)
                else:   # uploaded file: bars (found once on the whole video) come off while cutting - one encode
                    viral.cut(src, wa, wb, raw, vf=bars_vf(src, None if job.get("asis") else bars))
                    crops[i] = None if job.get("asis") or not bars else [int(v) for v in bars]
                cut_ok[i] = raw
            except Exception as e:  # noqa: BLE001
                log(f"cutting clip {cid} failed: {e}")
        # 2) the streamer's own webcam: the face that stays in one spot across every clip from this video
        own, own_q = upload_cams(list(cut_ok.values()), f"upload {num}") if not job.get("asis") and cut_ok else (None, "")
        for i, m in enumerate(moments):
            cid = f"up{job.get('id', num)}_{i + 1}"
            raw = RAW / f"{cid}.mp4"
            if i not in cut_ok:
                continue
            try:
                import viral
                dur = float(m["end"]) - float(m["start"])   # the clip itself (the saved copy has ~20 s more each side)
                if m.get("part") and not m["text"]:
                    try:
                        m["text"] = transcript(raw)
                    except Exception:  # noqa: BLE001
                        m["text"] = ""
                cam, q, frac = None, "none", None
                try:
                    cfr = _pool_frames([raw], 6)
                    if own and face_in_box(cfr, own) >= max(2, len(cfr) // 2):
                        cam, q = own, own_q        # the streamer's own webcam (works for cut-outs with no border)
                    else:
                        cam, q, frac = find_cam_info(raw, dur, st.get("learned_cams", {}).get(login))
                except Exception as e:  # noqa: BLE001   a webcam-check error never costs you the clip: full screen
                    log(f"webcam check failed {cid}: {e}")
                    cam, q, frac = None, "none", None
                rec = {"status": "new", "source": "upload", "streamer": name, "login": login, "src_link": job.get("url") or "",
                       "src_commit": job.get("src_commit") or "", "bars": crops.get(i),
                       "src_s": round(src_dur, 1) if src_dur else 0, "raw_win": wins.get(i),
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
                if m.get("hook_ai") and not names_ok(m["hook"], f"{name} {login} {m.get('text') or ''} {job.get('name') or ''}"):
                    log(f"AI hook for {cid} named someone who isn't in the clip - not used: {m['hook']}")
                    m["hook_ai"], m["hook"] = False, ""
                if m.get("hook_ai"):
                    rec.update(hook=viral.limit_caps(m["hook"]), hook_kind="ai")
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
                elif cam and q in ("edge", "learned", "cutout", "owner", "owner1"):
                    rec.update(cam=cam, cam_q=q, layout="split")
                    try:   # someone else's face on screen too (the video they're reacting to) -> reaction layout;
                        if other_faces(_pool_frames([raw], 4), cam) >= 2:   # the AI picture check refines it later
                            rec.update(layout="react", react=True, react_by="faces")
                    except Exception:  # noqa: BLE001
                        pass
                else:   # no webcam in your own link/upload: keep it, gameplay fills the whole screen
                    rec.update(cam="0,0,1,1", cam_q="none", layout="fill")
                st["clips"][cid] = rec
                made.append(f"- {int(m['start'] // 60)}:{int(m['start'] % 60):02d}–{int(m['end'] // 60)}:{int(m['end'] % 60):02d} "
                            f"({', '.join(m['reasons']) or 'good moment'}) — {rec.get('hook') or ''}")
            except Exception as e:  # noqa: BLE001
                log(f"cutting clip {cid} failed: {e}")
        msg = (f"Made {len(made)} clip{'s' if len(made) != 1 else ''} from the funniest moments of **{job.get('name', 'your video')}** "
               "— they're in Your clips (they don't auto-post):\n" + "\n".join(made)) if made \
            else "Couldn't make clips from this video — try uploading the file instead."
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
            c["hook"] = fix_names(e["hook"])   # streamer names spelled right
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
            c["final_hash"] = c["final_v"] = hashlib.sha1((c["hook"] + c.get("cam", "") + FINAL_V + c.get("layout", "")).encode()).hexdigest()[:10]
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


# keep the your-clips maker's folder (v/m + mine.json) from the LIVE site when the auto finder publishes, so neither
# maker removes the other's videos. $base = the live site version it was copied from (the push only goes through
# if nobody changed the site since - otherwise it copies again and retries).
KEEP_MINE_SH = r"""
git fetch -q --depth=1 origin +gh-pages:refs/remotes/origin/gh-pages || true
base=$(git rev-parse origin/gh-pages 2>/dev/null || echo "")
rm -rf "$SITE/v/m" "$SITE/mine.json" "$SITE/v/l" "$SITE/longs.json"
git archive origin/gh-pages v/m 2>/dev/null | tar -x -C "$SITE" || true
git show origin/gh-pages:mine.json > "$SITE/mine.json" 2>/dev/null || rm -f "$SITE/mine.json"
git archive origin/gh-pages v/l 2>/dev/null | tar -x -C "$SITE" || true      # long videos (their own maker)
git show origin/gh-pages:longs.json > "$SITE/longs.json" 2>/dev/null || rm -f "$SITE/longs.json"
"""


def publish_site() -> None:
    """Put the current site folder live now (mid-run), same way the workflow does at the end."""
    if not os.environ.get("GITHUB_ACTIONS") or MINE:
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
    sh = sh.replace('touch "$SITE/.nojekyll"', KEEP_MINE_SH + '\ntouch "$SITE/.nojekyll"').replace(
        'git push -qf origin "$c:refs/heads/gh-pages"', 'git push -q --force-with-lease=gh-pages:"$base" origin "$c:refs/heads/gh-pages"')
    try:
        subprocess.run(["bash", "-c", sh], check=True, cwd=str(ROOT), env={**os.environ, "SITE": str(SITE)}, timeout=240)
        log("site updated (mid-run)")
    except Exception as e:  # noqa: BLE001
        log(f"mid-run publish skipped: {e}")


# ---------------------------------------------------------------- main
def main() -> None:
    global FINAL_V   # + the follow voice you picked (so changing it re-makes the queued clips)
    st = load_state()
    held = {}
    if not MINE:   # your pasted / uploaded clips are made by the separate your-clips maker now: leave them alone
        held = {k: st["clips"].pop(k) for k in list(st.get("clips", {})) if st["clips"][k].get("source") == "upload"}
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
    try:   # the voice that says "Don't forget to follow and subscribe..." (Settings on the site)
        import engage
        engage.VOICE = str(control.get("follow_voice") or "am_puck")
        if engage.VOICE != "off" and not engage.follow_voice(Path(tempfile.gettempdir()) / "engage"):
            log(f"follow line in voice {engage.VOICE} not made yet - clips stay as they are (pop only) for now")
            FINAL_V = LAYOUT_V + "-follow3"   # nothing re-made until the voice exists (then they all get it)
            engage.VOICE = "off"
        elif engage.VOICE != "am_puck":
            FINAL_V = LAYOUT_V + "-follow4-" + engage.VOICE
    except Exception as e:  # noqa: BLE001
        log(f"follow voice: {e}")
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

    if MINE:
        process_uploads(st, tmp)
    else:
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
                    if usual and (not cam or not same_spot(cam, usual)) and box_has_face(raw, dur, usual):
                        cam, q = usual, "learned"   # his usual webcam spot has his face: that's the webcam (Nov 25)
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
            _hh = hashlib.sha1((r.get("hook", "") + r.get("cam", "") + FINAL_V + r.get("layout", "")).encode()).hexdigest()[:10]
            _olds = {hashlib.sha1((r.get("hook", "") + r.get("cam", "") + v_ + r.get("layout", "")).encode()).hexdigest()[:10]
                     for v_ in (LAYOUT_V_OLD, LAYOUT_V_OLD + "-follow1", LAYOUT_V_OLD + "-follow2", LAYOUT_V_OLD + "-follow3",
                                LAYOUT_V + "-follow2", LAYOUT_V + "-follow3")}
            r["final"] = (f"v/{r['id']}_final.mp4?v={r['final_v']}"   # (an older video still counts until it's re-made)
                          if r.get("final_v") and (r.get("hook") or r.get("no_hook")) and (r.get("final_v") == _hh or r.get("final_v") in _olds)
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
            r["wide_src"] = (f"v/{r['id']}_wide.mp4?k={(r.get('wide') or {}).get('k', '')}"
                             if (VID / f"{r['id']}_wide.mp4").exists() and r.get("wide") else "")
            r["vposter"] = (f"v/{r['id']}_p.jpg?r={r.get('rendered', '')}" if (VID / f"{r['id']}_p.jpg").exists() else "")
            r["ed_src"] = (r.get("ed") or {}).get("path", "")
            r["clipped"] = r.get("created")
            e = _EDITS.get(r["id"])   # newest quick text edit (still newer than the last full re-make)
            if e and e.get("video") and e.get("at", 0) > (r.get("rendered") or 0) and (VID / "e" / Path(e["video"]).name).exists():
                r["final"] = e["video"]
                r["final_abs"] = f"{PAGES}/{e['video']}"
        # quick-edit videos of clips that left the queue only take up site space
        live_ids = {r["id"] for r in ready}
        for f in (VID / "e").glob("*.mp4") if (VID / "e").exists() else []:
            if f.name.rsplit("_", 1)[0] not in live_ids and (MINE or not f.name.startswith("up")):
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
    if MINE and st.get("mine_cam_redo_v", 0) < 3:   # one time (Nov 24): your clips that went full-screen or got a
        st["mine_cam_redo_v"] = 3                    # blurry face crop - bars off, find the streamer's own webcam
        tmp0 = Path(tempfile.mkdtemp())              # across all clips of that upload, re-make them
        groups, src_bars = {}, {}
        for cid, c in clips.items():
            if c.get("source") == "upload" and c["status"] == "new" and not c.get("asis") and not c.get("user_layout") \
                    and not c.get("full") and not c.get("part") and cid.startswith("up"):
                groups.setdefault(cid.rsplit("_", 1)[0], []).append(cid)
        fixes = {   # your Jynxzi upload (Nov 24): the AI didn't know whose stream it was and guessed wrong names
            "upmwuw3jf8_1": "Jynxzi says Nick Eh 30 is the FUNNIEST GTA 6 streamer",
            "upmwuw3jf8_2": "Jynxzi ROASTED Nick Eh 30 for being too family friendly",
            "upmwuw3jf8_3": "Nick Eh 30 PANICS on his family friendly stream",
            "upmwuw3jf8_4": "Nick Eh 30 wants a FAMILY FRIENDLY mode in GTA 6",
        }
        for cid, hk in fixes.items():
            if cid in clips:
                clips[cid].update(streamer="Jynxzi", login="jynxzi", tag="#Jynxzi", hook=hk, hook_kind="user",
                                  hook_by_user=True, watching="Nick Eh 30")
                clips[cid].pop("hook_auto", None)
        for gid, cids in groups.items():
            try:
                for cid in list(cids):
                    raw = RAW / f"{cid}.mp4"
                    if raw.exists() and is_hdr(raw) and not clips[cid].get("src_link"):
                        raw.unlink()   # cut from an HDR phone recording without converting: washed out - cut again
                    if not raw.exists():
                        try:
                            if not clips[cid].get("src_link") and not clips[cid].get("bars"):
                                usrc = upload_source(clips[cid], cid, tmp0)
                                if str(usrc) not in src_bars:
                                    fb = find_bars(usrc)
                                    src_bars[str(usrc)] = [int(v) for v in fb] if fb else None
                                clips[cid]["bars"] = src_bars[str(usrc)]   # bars come off while re-cutting
                            refetch_upload(clips[cid], raw, tmp0, cid)   # the saved copy was lost: get it back
                        except Exception as e:  # noqa: BLE001
                            log(f"{cid}: couldn't get the video again ({str(e)[:100]}) - left as it is")
                            cids.remove(cid)
                            continue
                    elif not clips[cid].get("bars"):
                        got = crop_bars(raw, tmp0)   # the saved copy still has its bars: cut them off
                        if got and not clips[cid].get("src_link"):
                            clips[cid]["bars"] = None   # (relative to the clip, not the upload: don't reuse)
                if not cids:
                    continue
                own, own_q = upload_cams([RAW / f"{cid}.mp4" for cid in cids], gid)
                for cid in cids:
                    c, raw = clips[cid], RAW / f"{cid}.mp4"
                    for k in ("final_hash", "final_v", "clean_v", "pic_ok", "irl_x", "react", "react_view", "react_by",
                              "watching", "cam_only", "hd"):
                        c.pop(k, None)   # re-make, and the AI picture check looks again (reaction? whose webcam?)
                    cfr = _pool_frames([raw], 6)
                    cam, q = (own, own_q) if own and face_in_box(cfr, own) >= max(2, len(cfr) // 2) else (None, None)
                    if not cam:
                        cam, q, _f = find_cam_info(raw, duration_of(raw), st.get("learned_cams", {}).get(c.get("login", "")))
                    if cam:
                        c.update(cam=cam, cam_q=q, layout="split", cam_v=CAM_V)
                        if other_faces(_pool_frames([raw], 4), cam) >= 2:
                            c.update(layout="react", react=True, react_by="faces")
                        log(f"{cid}: streamer's webcam {cam} ({q}) -> re-making as {c['layout']}")
                    else:
                        c.update(cam="0,0,1,1", cam_q="none", layout="fill")
                        log(f"{cid}: still no webcam -> re-making full screen")
            except Exception as e:  # noqa: BLE001
                log(f"webcam re-check failed {gid}: {e}")
    if MINE and st.get("mine_hook_fix_v", 0) < 1:   # one time (Nov 24): Jynxzi is the one reacting - the hooks are
        st["mine_hook_fix_v"] = 1                    # about HIS reaction, not about Nick Eh 30
        for cid, hk in {"upmwuw3jf8_3": "Jynxzi LOSES IT at Nick Eh 30's family friendly meltdown",
                        "upmwuw3jf8_4": "Jynxzi reacts to Nick Eh 30 wanting a family friendly GTA 6"}.items():
            if cid in clips and not clips[cid].get("user_layout"):   # never over your own Studio edit
                clips[cid].update(hook=hk, hook_kind="user", hook_by_user=True)
    if MINE and st.get("mine_hook_fix_v", 0) < 2:   # your own Studio edit of clip 3 (layout + hook) stays yours
        st["mine_hook_fix_v"] = 2
        c3 = clips.get("upmwuw3jf8_3")
        if c3 and c3.get("user_layout"):
            c3.update(hook="Jynxzi FREAKED OUT over SCARRING audio", hook_kind="user", hook_by_user=True)
    if MINE and st.get("mine_timing_v", 0) < 1:   # one time (Nov 24): Jynxzi clips started late / stopped before the
        st["mine_timing_v"] = 1                    # payoff - a little more before, more after, and no second trim
        for cid, c in clips.items():
            if cid.startswith("upmwuw3jf8_") and c["status"] == "new" and c.get("moment"):
                c["moment"]["start"] = round(max(0.0, float(c["moment"]["start"]) - 1.0), 2)
                c["moment"]["end"] = round(float(c["moment"]["end"]) + 3.0, 2)
                (RAW / f"{cid}.mp4").unlink(missing_ok=True)   # cut again from your upload with the new start / end
                for k in ("final_hash", "final_v", "clean_v", "trim"):
                    c.pop(k, None)
                log(f"{cid}: re-cutting {c['moment']['start']}-{c['moment']['end']} s (setup + payoff)")
    if not MINE and st.get("short_v", 0) < 1:   # one time (Nov 24): queued auto clips re-cut to ~14 s, moment early
        st["short_v"] = 1
        n = 0
        for cid, c in clips.items():
            tr = c.get("trim")
            ulx = c.get("user_layout") or {}
            if c["status"] == "new" and c.get("source") != "upload" and not ulx.get("span") and not ulx.get("trim") \
                    and (not tr or tr[1] - tr[0] > CLIP_MAX_S + 0.5 or (c.get("seconds") or 0) > CLIP_MAX_S + 3):
                for k in ("final_hash", "final_v", "clean_v"):
                    c.pop(k, None)
                n += 1
        if n:
            log(f"shorter clips: re-making {n} queued clips at ~14 s")
    if st.get("safe_view_v", 0) < 1:   # one time (Nov 24): reaction clips re-made inside TikTok's safe area (the hook
        st["safe_view_v"] = 1           # sat under TikTok's search bar), and every finished clip gets the view check
        n = 0
        for cid, c in clips.items():
            if c["status"] != "new":
                continue
            if (c.get("layout_used") or c.get("layout")) == "react" or (c.get("user_layout") or {}).get("hook_y") is not None:
                for k in ("final_hash", "final_v", "clean_v"):
                    c.pop(k, None)
                n += 1
            elif (VID / f"{cid}_final.mp4").exists() and "view_ok" not in c:
                try:
                    vok, vwhy = view_check(VID / f"{cid}_final.mp4", c.get("layout_used") or c.get("layout") or "split", None)
                    c["view_ok"], c["view_why"] = vok, vwhy
                except Exception as e:  # noqa: BLE001
                    log(f"view check skipped {cid}: {e}")
        if n:
            log(f"safe area: re-making {n} reaction clips with the hook below TikTok's top bar")
    if MINE and st.get("wide_v", 0) < 1:   # one time: your clips' saved copies re-cut ~20 s wider each side
        st["wide_v"] = 1
        tmpw = Path(tempfile.mkdtemp())
        for cid, c in clips.items():
            if c["status"] != "new" or c.get("raw_win") or not c.get("moment") or c.get("asis") or c.get("part") or c.get("full"):
                continue
            if not (c.get("src_commit") or c.get("src_link")):
                continue
            try:
                a0, b0 = float(c["moment"]["start"]), float(c["moment"]["end"])
                c["raw_win"] = [round(max(0.0, a0 - 20), 2), round(b0 + 20, 2)]
                raw = RAW / f"{cid}.mp4"
                old = raw.with_suffix(".old.mp4")
                if raw.exists():
                    raw.replace(old)
                refetch_upload(c, raw, tmpw, cid)
                if c.get("src_s"):
                    c["raw_win"][1] = min(c["raw_win"][1], float(c["src_s"]))
                old.unlink(missing_ok=True)
                log(f"{cid}: saved copy now {c['raw_win'][0]:.0f}-{c['raw_win'][1]:.0f}s (Trim can show around the clip)")
            except Exception as e:  # noqa: BLE001
                c.pop("raw_win", None)
                if (RAW / f"{cid}.old.mp4").exists():
                    (RAW / f"{cid}.old.mp4").replace(RAW / f"{cid}.mp4")
                log(f"{cid}: couldn't widen the saved copy: {e}")
    if st.get("safe_view_v", 0) < 2:   # one time: re-check the queue with the webcam-size / blank-screen checks too
        st["safe_view_v"] = 2
        for cid, c in clips.items():
            fin, raw = VID / f"{cid}_final.mp4", RAW / f"{cid}.mp4"
            lay0 = c.get("layout_used") or c.get("layout") or "split"
            if c["status"] != "new" or not fin.exists() or not c.get("final_hash"):
                continue
            inf = {}
            try:
                if raw.exists() and c.get("cam") and lay0 in ("split", "react"):
                    x0, y0, w0, h0 = [float(v) for v in (c.get("cam_used") or c["cam"]).split(",")]
                    if lay0 == "split":
                        T0 = int((c.get("user_layout") or {}).get("top_h") or learn.CAM_PX.get((c.get("style") or {}).get("cam", ""), TOP_H))
                        inf["cam_px"] = list(_cam_fit(raw, w0, h0, T0, 3.6 if c.get("hd") else 2.8))
                    else:
                        g0 = react_geometry(raw, w0, h0, c.get("react_view") or None)
                        inf["cam_px"] = [g0["fw"], g0["fh"]]
                vok, vwhy = view_check(fin, lay0, inf)
                c["view_ok"], c["view_why"] = vok, vwhy
                if not vok:
                    log(f"view check {cid}: {vwhy} - held back from posting")
            except Exception as e:  # noqa: BLE001
                log(f"view check skipped {cid}: {e}")
    # Nov 25: only YOUR streamers (streamers.yaml). Clips of anyone else (CohhCarnage, BruceGreene...) come out of
    # the queue every run - your own uploads / links are never touched
    allowed = {k.lower() for k in (CFG.get("streamers") or {})}
    gone = []
    for cid0, c0 in clips.items():
        if c0["status"] != "new" or c0.get("source") == "upload" or c0.get("src_link") or MINE:
            continue
        who = (c0.get("login") or c0.get("streamer") or "").lower().replace(" ", "")
        if allowed and who and who not in allowed:
            c0["status"] = "skipped"
            c0["skip_reason"] = "not one of your streamers"
            gone.append(c0.get("streamer") or who)
    if gone:
        log(f"took {len(gone)} clips of other streamers out of the queue: {', '.join(sorted(set(gone)))}")
    if st.get("band_recheck_v", 0) < 1:   # one time (Nov 25): black-band check only looks where people can see
        st["band_recheck_v"] = 1
        n = 0
        for cid0, c0 in clips.items():
            f0 = VID / f"{cid0}_final.mp4"
            if c0["status"] == "new" and "black empty gap" in (c0.get("view_why") or "") and f0.exists():
                try:
                    ok0, why0 = view_check(f0, c0.get("layout_used") or c0.get("layout") or "split", None)
                    if "black empty gap" not in why0:
                        c0["view_ok"], c0["view_why"] = (not why0), why0
                        n += 1
                except Exception as e:  # noqa: BLE001
                    log(f"re-check {cid0}: {e}")
        if n:
            log(f"view check: {n} clips no longer held back for a dark band under TikTok's caption")
    if st.get("react_fill_v", 0) < 4:   # one time (Nov 25 pm): reaction layout back to the big webcam you liked
        st["react_fill_v"] = 4
        n = 0
        for cid0, c0 in clips.items():
            if c0["status"] != "new" or (c0.get("layout_used") or c0.get("layout")) != "react" or c0.get("user_layout"):
                continue
            rv0 = c0.get("react_view") or ""
            if c0.get("cam") and small_cam(c0["cam"]) and rv0 in ("", "0.0000,0.0000,1.0000,1.0000"):
                c0.update(layout="split", react=False)   # gameplay with friends' webcams (Kai + Speed), not a reaction
                c0.pop("react_view", None)
                c0.pop("layout_used", None)
                log(f"{cid0}: small webcam over gameplay - webcam + game layout instead of reaction")
            for k in ("final_hash", "clean_v", "final_v"):
                c0.pop(k, None)
            n += 1
        log(f"reaction clips: {n} re-made with the big webcam layout")
    if st.get("ronaldo_cam_v", 0) < 1:   # one time (Nov 25): Stable Ronaldo's webcam is bottom-left (the "webcam"
        st["ronaldo_cam_v"] = 1           # found was his GTA character's face -> his own webcam never showed)
        usual = ",".join(str(v) for v in (CFG["streamers"].get("stableronaldo", {}).get("facecam") or []))
        for cid0, c0 in clips.items():
            if c0["status"] == "new" and c0.get("login") == "stableronaldo" and usual and not c0.get("user_layout"):
                c0.update(cam=usual, cam_q="learned", layout="split", irl_x=-1, cam_v=CAM_V, in_game=True)
                for k in ("final_hash", "clean_v", "final_v", "react", "react_view", "cam_used", "layout_used"):
                    c0.pop(k, None)
                log(f"{cid0}: Stable Ronaldo's own webcam (bottom-left) on top")
    if st.get("react_fill_v", 0) < 2:   # one time (Nov 25): reaction clips re-made with no black gaps (bottom + top)
        st["react_fill_v"] = 2
        n = 0
        for c0 in clips.values():
            if c0["status"] == "new" and (c0.get("layout_used") or c0.get("layout")) == "react":
                c0.pop("final_hash", None)
                c0.pop("clean_v", None)
                n += 1
        log(f"reaction clips: {n} re-made with the blurred webcam filling the bottom")
    if st.get("not_game_v", 0) < 1:   # one time (Nov 25): Lacy's puppy clip is real life, not GTA 6
        st["not_game_v"] = 1
        c0 = clips.get("vod2939638120_6940")
        if c0 and c0["status"] == "new" and not c0.get("hook_by_user"):
            c0["hook"] = no_game_in_hook(c0.get("hook", ""))
            c0["in_game"] = False
            for k in ("final_hash", "final_v"):
                c0.pop(k, None)
            log(f"vod2939638120_6940: real life, not GTA 6 - hook now: {c0['hook']}")
    if st.get("caps_v", 0) < 1:   # one time (Nov 24): at most one CAPS word in our own hooks (yours stay as you wrote)
        st["caps_v"] = 1
        import viral
        n = 0
        for cid, c in clips.items():
            if c["status"] == "new" and c.get("hook") and not c.get("hook_by_user"):
                h2 = viral.limit_caps(c["hook"])
                if h2 != c["hook"]:
                    c["hook"] = h2
                    for k in ("final_hash", "final_v"):
                        c.pop(k, None)
                    n += 1
        if n:
            log(f"hooks: toned down CAPS in {n} hooks (re-making them)")
    if MINE and st.get("mine_hook_fix_v", 0) < 4:   # clip 3: "Nickeh 30" -> "Nick Eh 30"
        st["mine_hook_fix_v"] = 4
        c3 = clips.get("upmwuw3jf8_3")
        if c3 and c3.get("hook") and fix_names(c3["hook"]) != c3["hook"]:
            c3["hook"] = fix_names(c3["hook"])
            for k in ("final_hash", "final_v", "clean_v"):
                c3.pop(k, None)
    if st.get("subs_off_v", 0) < 2:   # one time (Nov 24): NO subtitles - re-make every clip that has them burned in
        st["subs_off_v"] = 2
        n = 0
        for cid, c in clips.items():
            sty0 = c.get("style") or {}
            ulx = c.get("user_layout") or {}
            had = sty0.get("subs") == "on" or ulx.get("subs") == "on" or c.get("subs_pick") == "on" or c.get("subs_made")
            ulx.pop("subs", None)
            c.pop("subs_pick", None)
            if c["status"] == "new" and had:
                sty0.pop("subs", None)
                for k in ("final_hash", "final_v", "clean_v"):
                    c.pop(k, None)
                n += 1
            elif "subs" in sty0:
                sty0.pop("subs", None)
        if n:
            log(f"subtitles off: re-making {n} clips without them")
    if MINE and st.get("mine_hook_fix_v", 0) < 3:   # clip 4's hook left "6" alone on a third line - shorter
        st["mine_hook_fix_v"] = 3
        c4 = clips.get("upmwuw3jf8_4")
        if c4 and not c4.get("user_layout"):
            c4.update(hook="Jynxzi reacts to Nick Eh 30's family friendly GTA 6", hook_kind="user", hook_by_user=True)
    if MINE and st.get("mine_view_fix_v", 0) < 1:   # one time (Nov 24): the AI's "watched video" box cut Nick Eh 30
        st["mine_view_fix_v"] = 1                    # in half - re-check it against the picture, whole screen if wrong
        for cid, c in clips.items():
            raw = RAW / f"{cid}.mp4"
            if c["status"] == "new" and c.get("react_view") and raw.exists():
                fr = _frames(raw, [1.5, 6, 11, 16])
                if not view_ok(fr, c["react_view"], c.get("cam", "")):
                    c["react_view"] = ""
                    for k in ("final_hash", "final_v", "clean_v"):
                        c.pop(k, None)
                    log(f"{cid}: showing the whole screen on top (the 'watched video' box would cut picture off)")
    if st.get("cutout_redo_v", 0) < 1:   # one time: clips made full-screen because a cut-out webcam was missed
        st["cutout_redo_v"] = 1
        for cid, c in clips.items():
            raw = RAW / f"{cid}.mp4"
            if c["status"] != "new" or not c.get("nocam") or c.get("source") == "upload" or not raw.exists():
                continue
            try:
                d0 = duration_of(raw)
                cam, q, _f = find_cam_info(raw, d0, st.get("learned_cams", {}).get(c.get("login", "")))
                usual = ",".join(str(v) for v in (CFG["streamers"].get(c.get("login", ""), {}).get("facecam") or []))
                if not cam and usual and box_has_face(raw, d0, usual):
                    cam, q = usual, "learned"
                if cam:
                    c.update(cam=cam, cam_q=q, layout="split", cam_v=CAM_V)
                    for k in ("nocam", "final_hash", "clean_v", "irl_x"):
                        c.pop(k, None)
                    log(f"{cid}: found the cut-out webcam ({q}) - re-made with the webcam on top")
            except Exception as e:  # noqa: BLE001
                log(f"cut-out webcam check failed {cid}: {e}")
    if st.get("big_only_v", 0) < 1:   # one time (Nov 24, big streamers only): small streamers' clips leave the queue
        st["big_only_v"] = 1
        for c in clips.values():
            if c["status"] == "new" and c.get("source") not in ("upload",) and c.get("login") not in CFG["streamers"] \
                    and (c.get("views") or 0) < 1000 and not c.get("post_now") and not c.get("hook_by_user"):
                c.update(status="expired", skip_reason="small streamer (big streamers only)")
    if st.get("short_redo_v", 0) < 2:   # one time: re-make queued clips that came out under 15 s (dead-air cut /
        st["short_redo_v"] = 2           # moment near the end of the Twitch clip)
        for c in clips.values():
            if c["status"] == "new" and c.get("source") != "upload" and (c.get("final_s") or 99) < 15 \
                    and (c.get("seconds") or 0) >= 16 and not c.get("hook_by_user"):
                c.pop("final_hash", None); c.pop("final_s", None)
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
    # variety (Nov 24): at most PER_STREAMER_MAX clips per streamer stay in the queue (their best), so one streamer
    # can't fill it (9 of 18 were ohnePixel). Post-now picks and clips you edited always stay.
    per, capped = {}, []
    for r in sorted([r for r in fresh if r.get("source") != "upload"], key=clip_quality, reverse=True):
        k = (r.get("login") or r.get("streamer") or "").lower()
        if r.get("post_now") or r.get("hook_by_user") or per.get(k, 0) < PER_STREAMER_MAX:
            per[k] = per.get(k, 0) + 1
            capped.append(r)
    keep = {r["id"] for r in mix_order(capped)[:QUEUE_MAX]}   # your pasted clips don't use up stream-clip spots
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
                    if not names_ok(r["hook"], hook_context({**c, "hook": ""})):
                        log(f"AI hook for {cid} named someone who isn't in the clip - kept the old one: {r['hook']}")
                        continue
                    c.update(hook=(f"Part {c['part']}: " if c.get("part") else "") + viral.limit_caps(r["hook"]), hook_kind="ai")
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
                clips[cid]["hook_short_tried"] = True
                if not names_ok(h, clips[cid].get("hook", "")):   # a shorter hook can't bring in a new name
                    log(f"shorter hook for {cid} changed who it's about - kept the old one: {h}")
                    continue
                h = viral.limit_caps(h)
                log(f"shorter hook {cid}: {h}")
                clips[cid]["hook"] = h
            if got:
                log(f"shortened {len(got)} long hooks")
    except Exception as e:  # noqa: BLE001
        log(f"hook shortening skipped: {e}")

    # your clip's new start / end goes past what was cut from your video: cut it again from the video, wider
    for cid, c in clips.items():
        sp = (c.get("user_layout") or {}).get("span")
        if c.get("source") != "upload" or not sp:
            continue
        mo = c.get("moment") or {}
        win = c.get("raw_win") or ([mo["start"], mo["end"]] if "start" in mo and "end" in mo else None)
        if c.get("source") == "upload" and c["status"] == "new" and sp and win \
                and (sp[0] < float(win[0]) - 0.05 or sp[1] > float(win[1]) + 0.05):
            if c.get("src_s"):
                sp = [max(0.0, sp[0]), min(float(c["src_s"]), sp[1])]
            c["raw_win"] = [round(max(0.0, float(sp[0]) - 20), 2), round(float(sp[1]) + 20, 2)]   # new saved copy, wider
            if c.get("src_s"):
                c["raw_win"][1] = min(float(c["src_s"]), c["raw_win"][1])
            (RAW / f"{cid}.mp4").unlink(missing_ok=True)
            for k in ("final_hash", "final_v", "clean_v"):
                c.pop(k, None)
            log(f"{cid}: cutting {sp[0]:.1f}-{sp[1]:.1f}s from your video (your new start / end)")

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
            for suf in (".mp4", "_final.mp4", "_clean.mp4", ".jpg", "_src.jpg", "_p.jpg", "_wide.mp4"):
                (VID / f"{cid}{suf}").unlink(missing_ok=True)
            (RAW / f"{cid}.mp4").unlink(missing_ok=True)
            continue
        raw = RAW / f"{cid}.mp4"
        if not raw.exists() and c.get("source") == "upload":
            # your pasted / uploaded clips: the original isn't kept between runs and has no Twitch address.
            # Finished video already on the site -> keep it as is. Needs a re-make -> fetch the link again.
            if (VID / f"{cid}_final.mp4").exists() and c.get("final_hash"):
                hh0 = hashlib.sha1(((c.get("hook") or "") + c.get("cam", "") + FINAL_V + c.get("layout", "")).encode()).hexdigest()[:10]
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
                hh = hashlib.sha1((c["hook"] + c["cam"] + FINAL_V + c.get("layout", "")).encode()).hexdigest()[:10]
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
        mine_new = c.get("source") == "upload" and set(c.get("style") or {}) <= {"subs"} and not c.get("full") and not c.get("part")
        if (not c.get("style") or mine_new) and not c.get("user_layout") and not c.get("asis") and not c.get("final_hash") \
                and (c.get("source") != "upload" or mine_new):
            # dynamic model: test looks, keep what gets views. Your pasted clips get the same looks (Nov 24) -
            # but the subtitles on/off you picked when making them always wins.
            c["style"] = {**learn.pick_style(LEARNED_INS, cid), **(c.get("style") or {})}
        sty = {**(c.get("style") or {}), **{k: v for k, v in (c.get("user_layout") or {}).items() if k in ("subs", "punch")}}
        span_u = (c.get("user_layout") or {}).get("span")   # your own start / end from the Studio's Trim
        up = c.get("source") == "upload"
        mom = c.get("moment") or {}
        raw_s = duration_of(raw)
        # times are on the whole footage (the Twitch clip / your whole video); the saved copy covers raw_win of it
        win0 = float((c.get("raw_win") or [mom.get("start", 0)])[0]) if up else 0.0
        target = span_u or ([float(mom["start"]), float(mom["end"])] if up and c.get("raw_win") and mom.get("end") else None)
        if target:
            a0, b0 = max(0.0, float(target[0]) - win0), min(raw_s, float(target[1]) - win0)
            if b0 - a0 >= 3 and (a0 > 0.05 or b0 < raw_s - 0.05):
                out_s = tmp / f"{cid}_span.mp4"
                import viral
                viral.cut(raw, a0, b0, out_s)
                raw = out_s
            c["span"] = [round(win0 + a0, 2), round(win0 + b0, 2)]
            if span_u:
                log(f"{cid}: your start / end {c['span'][0]:.1f}-{c['span'][1]:.1f}s")
        if not span_u:
            if not c.get("asis") and not c.get("part") and not c.get("full") and not up:
                # your clips were already cut around the moment on whole sentences (setup -> payoff): no second trim,
                # it was cutting off the setup / the payoff. Stream clips get trimmed to the best part.
                raw = best_part(raw, cid, tmp, c)
            if not c.get("asis") and not c.get("part") and not c.get("full"):   # your full video / parts stay whole
                raw = cut_dead_air(raw, cid, tmp)
            if not target:
                tr = c.get("trim") if not up else None
                c["span"] = [round(win0 + float(tr[0]), 2), round(win0 + float(tr[1]), 2)] if tr else [round(win0, 2), round(win0 + raw_s, 2)]
        # how far the Trim tool can go: what the saved copy covers (the preview shows all of it); your clips can go
        # further into your video (it's cut again from it)
        c["avail"] = [round(win0, 2), round(win0 + raw_s, 2)]
        c["avail_max"] = [0.0, round(float(c.get("src_s") or 0) or (win0 + raw_s + 60), 2)] if up and (c.get("src_commit") or c.get("src_link")) else c["avail"]
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
        if (_ul.get("trim") and not _ul.get("span")) or (_ul.get("speed") or 1) > 1.01:   # your trim / speed from the editor
            try:
                a0, b0 = ((_ul.get("trim") if not _ul.get("span") else None) or [0, 9999])
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
                vis = dict(getattr(viral, "LAST_VISUAL", {}) or {})
                if ok is not None and vis.get("in_game") is True:
                    c["in_game"] = True
                if ok is not None and vis.get("question") and not c.get("hook_by_user"):
                    c["cap_q"] = vis["question"]   # the caption's opening question (Nov 25)
                if ok is not None and vis.get("in_game") is False and not c.get("hook_by_user"):
                    # not the game on screen (real life, a browser, another game): the hook mustn't say "in GTA 6"
                    c["in_game"] = False
                    h0 = c.get("hook", "")
                    h1 = no_game_in_hook(h0)
                    if h1 != h0:
                        c["hook"] = h1
                        log(f"{cid}: not {fetch_clips_mod.TOPIC.get('name') or 'GTA 6'} on screen - hook fixed: {h1}")
                if ok is not None and not vis.get("react") and c.get("react_by") == "faces" and not c.get("user_layout"):
                    # we guessed "reaction" from a second face; the AI looked and says it isn't -> normal layout
                    c.update(layout="split", react=False)
                    c.pop("react_by", None); c.pop("react_view", None)
                    lay = "split"
                    hh = hashlib.sha1((c["hook"] + c["cam"] + FINAL_V + "split").encode()).hexdigest()[:10]
                    ch = hashlib.sha1((c["cam"] + LAYOUT_V + "clean" + "split").encode()).hexdigest()[:10]
                    job = (cid, job[1], hh, ch, True, True)
                    need_f = need_c = True
                if ok and vis.get("react") and not c.get("user_layout") and (c.get("layout") != "react" or c.get("react_by") == "faces"):
                    # a REACTION clip (the streamer watching someone else's stream / video): react layout, using
                    # the CHANNEL OWNER's own webcam. Two faces on screen: the AI points out which webcam is the
                    # streamer's; a webcam we already traced / found staying put across the video wins when it's
                    # the same one (its box is the whole webcam, the AI's is often just the face).
                    rcam = react_cam(fr, vis.get("owner_cam"))
                    known = c.get("cam") if c.get("cam_q") in ("owner", "owner1", "owner-edge", "learned", "edge", "cutout",
                                                               "ai-react") else None
                    if known and (c.get("cam_q") in ("owner", "learned") or (rcam and same_spot(known, rcam))):
                        rcam = known   # seen in the same spot across your whole video / this streamer's known spot
                    elif rcam:
                        rcam = widen_cam(fr, rcam)
                    if rcam:
                        c.pop("react_by", None)
                        rv = react_view(vis.get("watched_box"), rcam) or ""
                        if rv and not view_ok(fr, rv, rcam):
                            log(f"react clip {cid}: the AI's 'watched video' box would cut off picture - showing the whole screen")
                            rv = ""
                        if not rv and small_cam(rcam) and vis.get("in_game") is not False:
                            log(f"{cid}: friends' webcams over gameplay, not a reaction - webcam + game layout")
                            rcam = None
                    if rcam:
                        c.update(layout="react", cam=rcam, cam_q=c.get("cam_q") if rcam == known else "ai-react",
                                 react=True, watching=vis.get("watching", ""), react_view=rv)
                        c.pop("irl_x", None); c.pop("cam_only", None)
                        lay = "react"
                        hh = hashlib.sha1((c["hook"] + c["cam"] + FINAL_V + "react").encode()).hexdigest()[:10]
                        ch = hashlib.sha1((c["cam"] + LAYOUT_V + "clean" + "react").encode()).hexdigest()[:10]
                        job = (cid, job[1], hh, ch, True, True)
                        need_f = need_c = True
                        log(f"react clip {cid}: {c.get('streamer')} watching {vis.get('watching') or 'another creator'} -> react layout")
                    elif not c.get("post_now") and c.get("source") != "upload":
                        c.update(status="skip", skip_reason="react clip: couldn't tell which webcam is the streamer's")
                        log(f"skipped {cid}: react clip, streamer's webcam not found")
                        return job
                if ok and vhook and len(vhook) >= 20 and not c.get("hook_by_user") and "streamer" not in vhook.lower() \
                        and names_ok(vhook, hook_context(c) + " " + (vis.get("watching") or "")):
                    c["hook"], c["hook_kind"], c["hook_variant"] = (f"Part {c['part']}: " if c.get("part") else "") + viral.limit_caps(vhook), "ai", var
                    c.pop("hook_short_tried", None)   # a fresh hook gets its own length check
                    hh = hashlib.sha1((c["hook"] + c["cam"] + FINAL_V + c.get("layout", "")).encode()).hexdigest()[:10]
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
        if ul.get("mode") == "react":
            lay = "react"
        elif ul.get("mode") == "split" and ul.get("v", 0) >= 2 and lay == "react":
            lay = "split"                        # you switched a reaction clip to webcam + game
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
        if auto and lay == "split" and (c.get("irl_x") or -1) >= 0 and c.get("in_game") is not True:
            lay = f"irl:{c['irl_x']:.3f}"
            log(f"no gameplay in {cid}: full-screen shot on the streamer")
        if lay == "split" and cam and not ul.get("cam") and c.get("source") != "upload" and not cam_is_live(raw, cam):
            # the "webcam" never changes: it's a picture in the stream overlay (Nov 24: GTA artwork of Jason's face
            # was taken for a webcam), not a camera -> no webcam: big clips go full-screen gameplay, the rest are skipped
            if (c.get("views") or 0) >= NOCAM_MIN_VIEWS:
                lay = "fill"
                c.update(layout="fill", cam="0,0,1,1", cam_q="none", nocam=True)
                cam = c["cam"]
                log(f"{cid}: the webcam box is a still picture - full-screen gameplay instead")
            elif not c.get("post_now"):
                c.update(status="skip", skip_reason="no webcam (the box was a still picture)")
                log(f"skipped {cid}: webcam box is a still picture, not a camera")
                return job
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
                    top_h=int(ul["top_h"]) if ul.get("top_h") else learn.CAM_PX.get(sty.get("cam", ""), None),
                    view=(c.get("react_view") or None) if lay == "react" else None)
        # learner tests from the Nov 23 tool review: word-by-word subtitles, punch-in zoom on the big reaction
        try:
            if sty.get("punch") == "on" and lay in ("split", "react"):
                import viral
                a = viral._audio(raw)
                secs = len(a) // 16000
                if secs >= 4:
                    rms = [float(np.sqrt(np.mean(a[i * 16000:(i + 1) * 16000] ** 2))) for i in range(secs)]
                    opts["punch_at"] = float(int(np.argmax(rms[1:-1])) + 1)
        except Exception as e:  # noqa: BLE001
            log(f"subtitles/punch-in skipped for {cid}: {e}")
        if need_f:
            rinfo = render_final(raw, cid, cam, None if c.get("no_hook") else c["hook"], tmp, **opts,
                                 cta=True, pbar=sty.get("pbar") == "on")
            try:   # would it look right on TikTok / Shorts? (checked before it can be posted)
                vok, vwhy = view_check(VID / f"{cid}_final.mp4", lay, rinfo)
                c["view_ok"], c["view_why"] = vok, vwhy
                if not vok:
                    log(f"view check {cid}: {vwhy} - held back from posting")
            except Exception as e:  # noqa: BLE001
                log(f"view check skipped {cid}: {e}")
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
        hh = hashlib.sha1((c["hook"] + c["cam"] + FINAL_V + c.get("layout", "")).encode()).hexdigest()[:10]
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
                if ok and (getattr(viral, "LAST_VISUAL", {}) or {}).get("react") and c.get("layout") != "react" \
                        and not c.get("user_layout"):
                    c.pop("final_hash", None)   # a react clip made in the old layout: look again at the original next run
                    log(f"{cid} looks like a react clip: re-made in the react layout next run")
                    continue
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
        return sum(f.stat().st_size for f in VID.rglob("*") if f.is_file()
                   and (MINE or "m" not in f.relative_to(VID).parts[:-1])) / 1e6   # incl. quick-edit videos (v/e), not v/m
    if not MINE and (SITE / "mine.json").exists():   # your clips moved to their own folder (v/m): old copies go
        for f in VID.glob("up*"):
            if f.is_file():
                f.unlink(missing_ok=True)
    shrink_site_videos(st)
    if MINE:   # your clips: keep their footage ready on the 'raws' release, so edits re-make fast (a few per run)
        n_put = 0
        for cid, c in clips.items():
            if n_put >= 8:
                break
            raw_f = RAW / f"{cid}.mp4"
            if c["status"] == "new" and c.get("raw_win") and raw_f.exists() and (c.get("raw_asset") or {}).get("key") != _raw_key(c):
                raw_put(c, raw_f, cid)
                n_put += 1
    made_w = 0   # small previews of each clip's whole footage, for the Studio's Trim (cheap: 480 px, ~2 MB a minute)
    for cid, c in clips.items():
        if c["status"] == "new" and made_w < 60 and not c.get("asis"):
            try:
                made_w += make_wide(cid, c)
            except Exception as e:  # noqa: BLE001
                log(f"trim preview skipped {cid}: {e}")
    if made_w:
        log(f"trim previews: made {made_w}")
    made_e, t_e = 0, time.time()   # the live editor's full-quality footage: a few per run (your clips first)
    for cid, c in sorted(clips.items(), key=lambda kv: (kv[1].get("source") != "upload", -(kv[1].get("viral") or 0))):
        if c["status"] != "new" or made_e >= int(os.environ.get("ED_PER_RUN", "12")) or time.time() - t_e > 420:
            continue
        try:
            made_e += make_ed(cid, c)
        except Exception as e:  # noqa: BLE001
            log(f"editor footage skipped {cid}: {str(e)[:120]}")
    if made_e:
        log(f"live editor footage: made {made_e}")
    try:   # footage of clips that left the queue (posted / removed / expired) comes off the release
        if GH_TOKEN and not os.environ.get("ED_PER_RUN") == "0":
            _ED_REL_CACHE.pop("assets", None)
            keep = {(c.get("ed") or {}).get("path", "").replace("/", "__") for c in clips.values() if c["status"] == "new"}
            mine_ids = {re.sub(r"[^A-Za-z0-9_-]", "_", k) for k in clips}
            gone = 0
            for a_ in _ed_assets():
                n_ = a_["name"]
                if n_.startswith("v__ed__") and n_ not in keep:
                    owner = n_[len("v__ed__"):].rsplit("_", 1)[0]
                    if owner in mine_ids:   # only this maker's clips (stream clips vs your clips are made separately)
                        _gh("DELETE", f"/repos/{REPO}/releases/assets/{a_['id']}")
                        gone += 1
            if gone:
                log(f"live editor footage: removed {gone} old")
    except Exception as e:  # noqa: BLE001
        log(f"editor footage tidy skipped: {e}")
    mb = site_mb()
    if mb > SITE_MAX_MB:   # very first: Trim previews of the weaker half (Trim then uses the clip itself) - never cost a clip
        ranked = sorted([(clip_quality(c), cid) for cid, c in clips.items() if c["status"] == "new" and c.get("wide")])
        for _, cid in ranked[: max(1, len(ranked) // 2)]:
            if mb <= SITE_MAX_MB:
                break
            (VID / f"{cid}_wide.mp4").unlink(missing_ok=True)
            clips[cid].pop("wide", None); clips[cid]["no_wide"] = True
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
            for suf in ("_final.mp4", "_clean.mp4", ".mp4", ".jpg", "_src.jpg", "_p.jpg", "_wide.mp4"):
                (VID / f"{cid}{suf}").unlink(missing_ok=True)
            mb = site_mb()
        log(f"site size guard: now {mb:.0f} MB")

    # 5. queue.json
    write_queue()
    st["clips"].update(held)   # (set-aside clips go back into the saved state unchanged)
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

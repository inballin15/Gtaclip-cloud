#!/usr/bin/env python3
"""
gtaclip-cloud: runs on GitHub Actions (free), no PC and no OpusClip.

Every run:
  1. asks Twitch for the newest GTA 6 clips from the approved streamers
  2. downloads the best fresh ones directly (yt-dlp) - clean, no watermark, no captions
  3. uploads each raw 16:9 clip to Cloudinary, tagged "gtaclip_new" with its details

The scheduled Claude posting runs then pick the best "gtaclip_new" clip, write the
hook, build the boat-style 9:16 video with Cloudinary, post it, and re-tag it "gtaclip_used".

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, CLOUDINARY_URL
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cloudinary
import cloudinary.api
import cloudinary.uploader
import requests
import yaml
import yt_dlp

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "streamers.yaml").read_text(encoding="utf-8"))


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def twitch_headers() -> dict:
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip().strip('"').strip("'")
    secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip().strip('"').strip("'")
    # diagnostics only (never print the values)
    log(f"twitch id length={len(cid)} secret length={len(secret)} (both should be 30)")
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}, timeout=20)
    if r.status_code != 200:
        log(f"Twitch said {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return {"Client-ID": cid, "Authorization": f"Bearer {r.json()['access_token']}"}


def helix(h: dict, path: str, **params) -> dict:
    r = requests.get(f"https://api.twitch.tv/helix/{path}", headers=h, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def already_have() -> set[str]:
    """Twitch clip ids already uploaded (new or used) so we never download twice."""
    seen, cursor = set(), None
    for tag in ("gtaclip_new", "gtaclip_used", "gtaclip_skip"):
        cursor = None
        while True:
            res = cloudinary.api.resources_by_tag(tag, resource_type="video", max_results=500,
                                                  context=True, next_cursor=cursor)
            for r in res.get("resources", []):
                cid = (r.get("context") or {}).get("custom", {}).get("clip_id")
                if cid:
                    seen.add(cid)
            cursor = res.get("next_cursor")
            if not cursor:
                break
    return seen


LIVE: list[dict] = []   # who's live right now (for the dashboard)
# What we're clipping right now (set from the dashboard's topic search; GTA 6 by default).
#   game_ids: Twitch categories (empty = any game)   keywords: words that must be in the clip/stream title (optional)
TOPIC: dict = {"name": "GTA 6", "game_ids": [CFG["game_id"]], "keywords": [], "tags": "#GTA6 #GTAVI #gaming"}


def on_topic(game_id: str, title: str) -> bool:
    ids = TOPIC.get("game_ids") or []
    kws = [k.lower() for k in TOPIC.get("keywords") or []]
    if ids and str(game_id) not in ids:
        return False
    if kws and not any(k in (title or "").lower() for k in kws):
        return False
    return True


def live_now(h: dict) -> tuple[list[dict], dict]:
    """Approved streamers live now + big English GTA 6 streams (extra clip sources)."""
    d = CFG["discovery"]
    out, extra = [], {}
    logins = list(CFG["streamers"])
    try:
        mine = helix(h, "streams", user_login=logins, first=100).get("data") or []
    except Exception as e:  # noqa: BLE001
        log(f"live check failed: {e}")
        mine = []
    top = []
    try:
        if TOPIC.get("game_ids"):
            top = helix(h, "streams", game_id=TOPIC["game_ids"], first=40, language="en").get("data") or []
        elif TOPIC.get("keywords"):   # a topic that isn't one game: live channels talking about it
            found = helix(h, "search/channels", query=TOPIC.get("query") or " ".join(TOPIC["keywords"]), live_only="true", first=40).get("data") or []
            ids = [c["id"] for c in found if c.get("broadcaster_language", "en") == "en"][:40]
            top = helix(h, "streams", user_id=ids, first=40).get("data") or [] if ids else []
    except Exception as e:  # noqa: BLE001
        log(f"top {TOPIC.get('name')} streams failed: {e}")
    top = [st for st in top if on_topic(st.get("game_id", ""), st.get("title", ""))] if TOPIC.get("keywords") and TOPIC.get("game_ids") else top
    big_min = d.get("min_live_viewers", 10000) if TOPIC.get("name") == "GTA 6" else d.get("min_live_viewers_other", 1500)
    for st in mine + top:
        login = st["user_login"].lower()
        if any(x["login"] == login for x in out):
            continue
        approved = login in CFG["streamers"]
        big = st["viewer_count"] >= big_min and st in top[:30]
        if not approved and not big:
            continue
        out.append({"login": login, "name": st["user_name"], "viewers": st["viewer_count"],
                    "game": st.get("game_name", ""), "gta": on_topic(st.get("game_id", ""), st.get("title", "")),
                    "title": st.get("title", "")[:120], "since": st.get("started_at", ""),
                    "approved": approved,
                    "thumb": st.get("thumbnail_url", "").replace("{width}", "320").replace("{height}", "180")})
        if not approved and big and on_topic(st.get("game_id", ""), st.get("title", "")) and d.get("discover_live", True):
            extra[login] = {"name": st["user_name"], "tag": "#" + "".join(ch for ch in st["user_name"] if ch.isalnum()),
                            "id": st["user_id"], "discovered": True}
    out.sort(key=lambda x: (not x["gta"], -x["viewers"]))
    LIVE[:] = out
    return out, extra


def pick_clips(h: dict, seen: set[str]) -> list[dict]:
    d = CFG["discovery"]
    since = datetime.now(timezone.utc) - timedelta(hours=d["lookback_hours"])
    bad = [w.lower() for w in d["skip_title_words"]]
    picks = []
    _, extra = live_now(h)
    sources = {**CFG["streamers"], **extra}
    for login, s in sources.items():
        try:
            uid = s.get("id") or ((helix(h, "users", login=login).get("data") or [{}])[0]).get("id")
        except Exception as e:  # noqa: BLE001
            log(f"{login}: lookup failed {e}")
            continue
        if not uid:
            continue
        try:
            clips = helix(h, "clips", broadcaster_id=uid, first=50,
                          started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ")).get("data") or []
        except Exception as e:  # noqa: BLE001
            log(f"{login}: clips failed {e}")
            continue
        good = [c for c in clips
                if (on_topic(c.get("game_id", ""), c.get("title", "")) or (s.get("discovered") and not TOPIC.get("game_ids")))
                and c["id"] not in seen
                and c["view_count"] >= d["min_views"]
                and d["min_seconds"] <= c["duration"] <= d["max_seconds"]
                and not any(w in c["title"].lower() for w in bad)]
        good.sort(key=lambda c: c["view_count"], reverse=True)
        for c in good[: d["max_per_streamer"]]:
            c["login"], c["name"], c["tag"] = login, s["name"], s.get("tag", "")
            c["paid"] = bool(s.get("campaign"))
            c["discovered"] = bool(s.get("discovered"))
            picks.append(c)
        log(f"{s['name']}: {len(good)} fresh {TOPIC.get('name', 'topic')} clips" + (" (live topic streamer)" if s.get("discovered") else ""))
    # ANY GTA 6 streamer (Nicholas joined every Content Rewards campaign): the most-watched fresh GTA 6 clips on
    # all of Twitch, including streamers who aren't live right now or aren't on the list
    if d.get("game_wide", True) and TOPIC.get("game_ids"):
        have = {c["id"] for c in picks}
        try:
            gclips = helix(h, "clips", game_id=TOPIC["game_ids"][0], first=100,
                           started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ")).get("data") or []
        except Exception as e:  # noqa: BLE001
            gclips = []
            log(f"game-wide clips failed: {e}")
        n = 0
        per: dict[str, int] = {}
        for c in sorted(gclips, key=lambda c: c["view_count"], reverse=True):
            login = (c.get("broadcaster_name") or "").lower().replace(" ", "")
            if (c["id"] in seen or c["id"] in have or c.get("language", "en") not in ("en", "") or not login
                    or c["view_count"] < d.get("game_wide_min_views", 40)
                    or not d["min_seconds"] <= c["duration"] <= d["max_seconds"]
                    or any(w in c["title"].lower() for w in bad) or per.get(login, 0) >= 2):
                continue
            s = CFG["streamers"].get(login) or {}
            c["login"], c["name"], c["tag"] = login, s.get("name") or c.get("broadcaster_name") or login, s.get("tag") or "#" + "".join(ch for ch in (c.get("broadcaster_name") or login) if ch.isalnum())
            c["paid"] = bool(s.get("campaign"))
            c["discovered"] = login not in CFG["streamers"]
            per[login] = per.get(login, 0) + 1
            picks.append(c)
            n += 1
            if n >= d.get("game_wide_max", 12):
                break
        log(f"all of Twitch: {n} fresh {TOPIC.get('name')} clips from {len(per)} streamers")

    def velocity(c: dict) -> float:   # views per hour: the moments blowing up right now (first poster wins)
        try:
            age = max(0.25, (datetime.now(timezone.utc) - datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).total_seconds() / 3600)
        except Exception:  # noqa: BLE001
            age = 4.0
        return c["view_count"] / age
    # paid first, then the fastest-rising clips; discovered streamers get a fair share, not all of it
    # streamer boost (Nov 23, from the study + our own views): Kai > Speed = Lacy > xQc; Clix's GTA clips lower
    def boost(c: dict) -> float:
        return float((CFG["streamers"].get(c.get("login", "")) or {}).get("boost", 0.8 if c["discovered"] else 1.0))
    picks.sort(key=lambda c: (c["paid"] and boost(c) >= 1, velocity(c) * boost(c)), reverse=True)
    return picks[: d["max_total"]]


def download(url: str, folder: Path) -> Path:
    # highest resolution, then highest frame rate/bitrate; never Twitch's vertical "portrait" versions
    opts = {"outtmpl": str(folder / "%(id)s.%(ext)s"), "quiet": True, "noprogress": True, "noplaylist": True,
            "format": "bestvideo*[aspect_ratio>1]+bestaudio/best[aspect_ratio>1]/best",
            "format_sort": ["res", "fps", "tbr"], "merge_output_format": "mp4"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
    try:
        fl = [f"{f.get('format_id')}:{f.get('width')}x{f.get('height')}@{f.get('fps')}"
              for f in info.get("formats") or [] if f.get("height")]
        print(f"[download] got {info.get('format_id')} {info.get('width')}x{info.get('height')}@{info.get('fps')} "
              f"tbr={info.get('tbr')} | offered: {' '.join(fl)[:300]}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    mp4 = p.with_suffix(".mp4")
    return mp4 if mp4.exists() else p


_WHISPER = None


def transcript(path: Path) -> str:
    """What the streamer says (so the hook writer knows what happens). Free, runs on CPU."""
    global _WHISPER
    try:
        from faster_whisper import WhisperModel
        if _WHISPER is None:
            _WHISPER = WhisperModel("base", device="cpu", compute_type="int8")
        import subprocess
        import numpy as np
        pcm = subprocess.run([__import__("imageio_ffmpeg").get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le",
                              "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        segs, _ = _WHISPER.transcribe(audio, language="en", vad_filter=True)
        text = " ".join(s.text.strip() for s in segs)
    except Exception as e:  # noqa: BLE001
        log(f"transcript failed: {e}")
        return ""
    return clean(text)[:900]


def clean(t: str) -> str:
    """Cloudinary context only accepts plain text: drop emojis/odd symbols and separators."""
    t = t.encode("ascii", "ignore").decode().replace("|", " ").replace("=", " ")
    return " ".join(t.split())


def main() -> None:
    raw = os.environ.get("CLOUDINARY_URL", "").strip().strip('"').strip("'")
    if raw.upper().startswith("CLOUDINARY_URL="):
        raw = raw.split("=", 1)[1].strip()
    os.environ["CLOUDINARY_URL"] = raw
    cloudinary.reset_config()
    cloudinary.config(secure=True)  # reads CLOUDINARY_URL
    c = cloudinary.config()
    k = str(c.api_key or "")
    # diagnostics only: never print the secret
    log(f"cloudinary: starts_ok={raw.startswith('cloudinary://')} cloud_name={c.cloud_name!r} "
        f"api_key_len={len(k)} api_key_digits={k.isdigit()} api_key_end=...{k[-3:]} "
        f"secret_len={len(str(c.api_secret or ''))}")
    h = twitch_headers()
    seen = already_have()
    picks = pick_clips(h, seen)
    if not picks:
        log("no fresh clips this run")
        return
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for c in picks:
            try:
                f = download(c["url"], Path(tmp))
                s = CFG["streamers"][c["login"]]
                fc = s.get("facecam") or ""
                ctx = {
                    "clip_id": c["id"], "streamer": c["name"], "login": c["login"],
                    "title": clean(c["title"])[:200] or "clip",
                    "views": str(c["view_count"]), "created": c["created_at"],
                    "url": c["url"], "facecam": ",".join(str(v) for v in fc) if fc else "",
                    "tag": s.get("tag", ""), "campaign": s.get("campaign", "") or "",
                    "transcript": transcript(f),
                }
                res = cloudinary.uploader.upload_large(
                    str(f), resource_type="video", public_id=f"gtaclip/tw_{c['login']}_{c['id'][:40]}",
                    overwrite=True, tags=["gtaclip_new", c["login"]], context=ctx)
                log(f"uploaded {c['name']} '{c['title']}' -> {res['public_id']}")
                out.append({**ctx, "public_id": res["public_id"]})
            except Exception as e:  # noqa: BLE001
                log(f"failed {c['url']}: {e}")
    print(json.dumps(out, indent=2))
    if not out:
        sys.exit(1)


if __name__ == "__main__":
    import traceback
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)

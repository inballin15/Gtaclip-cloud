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
    cid, secret = os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"]
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}, timeout=20)
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


def pick_clips(h: dict, seen: set[str]) -> list[dict]:
    d = CFG["discovery"]
    since = datetime.now(timezone.utc) - timedelta(hours=d["lookback_hours"])
    bad = [w.lower() for w in d["skip_title_words"]]
    picks = []
    for login, s in CFG["streamers"].items():
        try:
            users = helix(h, "users", login=login).get("data") or []
        except Exception as e:  # noqa: BLE001
            log(f"{login}: lookup failed {e}")
            continue
        if not users:
            continue
        clips = helix(h, "clips", broadcaster_id=users[0]["id"], first=50,
                      started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ")).get("data") or []
        good = [c for c in clips
                if c.get("game_id") == CFG["game_id"]
                and c["id"] not in seen
                and c["view_count"] >= d["min_views"]
                and d["min_seconds"] <= c["duration"] <= d["max_seconds"]
                and not any(w in c["title"].lower() for w in bad)]
        good.sort(key=lambda c: c["view_count"], reverse=True)
        for c in good[: d["max_per_streamer"]]:
            c["login"], c["name"] = login, s["name"]
            c["paid"] = bool(s.get("campaign"))
            picks.append(c)
        log(f"{s['name']}: {len(good)} fresh GTA 6 clips")
    picks.sort(key=lambda c: (c["paid"], c["view_count"]), reverse=True)
    return picks[: d["max_total"]]


def download(url: str, folder: Path) -> Path:
    opts = {"outtmpl": str(folder / "%(id)s.%(ext)s"), "quiet": True, "noplaylist": True,
            "format": "bestvideo*+bestaudio/best", "merge_output_format": "mp4"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
    mp4 = p.with_suffix(".mp4")
    return mp4 if mp4.exists() else p


def main() -> None:
    cloudinary.config(secure=True)  # reads CLOUDINARY_URL
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
                    "title": c["title"][:200].replace("|", " ").replace("=", " "),
                    "views": str(c["view_count"]), "created": c["created_at"],
                    "url": c["url"], "facecam": ",".join(str(v) for v in fc) if fc else "",
                    "tag": s.get("tag", ""), "campaign": s.get("campaign", "") or "",
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
    main()

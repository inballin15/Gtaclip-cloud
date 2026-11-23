"""VOD miner: finds the best moments in streamers' broadcasts (live or just ended), not only viewer-made clips.

Runs on GitHub every 30 min (workflow vods.yml), separate from the clip maker so it never slows it down.
For each streamer in streamers.yaml -> vod_streamers:
  1. look up their latest broadcast (Twitch API; a live stream already has one that keeps growing)
  2. take only the part not checked yet (at most the last 8 hours, at most 90 min per run)
  3. download that part's AUDIO only, find the best moments (viral.find_moments + Gemini rating if a key is set)
  4. download only those video sections
  5. push them to a branch 'vodclip-<time>' -> the clip maker imports them into the queue (webcam, hook, videos)
Progress per broadcast is kept on the 'posted' branch in vod_progress.json so nothing is checked twice.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
P = Path(os.environ.get("POSTED_DIR", ROOT / "_posted"))
OUT = Path(os.environ.get("VOD_OUT", ROOT / "_vodclips"))
NOW = datetime.now(timezone.utc)
MAX_BACK_H = 8          # never look further back than this (the queue only keeps clips < 9 h old)
CHUNK_MIN = 90          # most minutes of one broadcast checked per run
TAIL_SKIP_S = 60        # leave the last minute of a live stream for next time (still being written)
PER_CHUNK = 3           # best moments kept per checked chunk


def log(m: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%SZ}] {m}", flush=True)


def parse_dur(d: str) -> int:
    """Twitch durations look like 3h2m10s."""
    total = 0
    for v, u in re.findall(r"(\d+)([hms])", d or ""):
        total += int(v) * {"h": 3600, "m": 60, "s": 1}[u]
    return total


CHAT_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"   # Twitch's chat-replay query
CHAT_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # Twitch's public web player id (same one TwitchDownloader/yt-dlp use)


def chat_rate(video_id: str, start: int, end: int, step: int = 20, max_req: int = 280):
    """Chat messages per second for start..end of a VOD (from its public chat replay), like TwitchDownloader reads it.
    One request every `step` s: each returns the next batch of messages, and how tightly they're packed gives the
    chat speed there. Returns a numpy array (index 0 = start) or None if chat isn't available."""
    import numpy as np
    import urllib.request
    n = end - start
    if n <= 0:
        return None
    rate = np.zeros(n)
    got = 0
    for t in range(start, end, max(step, int(n / max_req) + 1)):
        body = json.dumps([{"operationName": "VideoCommentsByOffsetOrCursor",
                            "variables": {"videoID": str(video_id), "contentOffsetSeconds": t},
                            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": CHAT_HASH}}}]).encode()
        req = urllib.request.Request("https://gql.twitch.tv/gql", data=body,
                                     headers={"Client-ID": CHAT_CLIENT, "Content-Type": "text/plain"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            edges = (((d[0].get("data") or {}).get("video") or {}).get("comments") or {}).get("edges") or []
        except Exception as e:  # noqa: BLE001
            if got == 0:
                log(f"chat replay unavailable: {str(e)[:100]}")
                return None
            continue
        offs = sorted(e["node"]["contentOffsetSeconds"] for e in edges if e.get("node"))
        if not offs:
            continue
        got += 1
        span = max(1.0, offs[-1] - offs[0])
        a = int(offs[0]) - start
        b = min(n, int(offs[-1]) - start + 1)
        if b > a >= 0:
            rate[a:b] = np.maximum(rate[a:b], len(offs) / span)
    return rate if got else None


def main() -> None:
    from fetch_clips import CFG, helix, twitch_headers
    import viral
    from pipeline import fetch_link_audio, fetch_link_section

    logins = CFG.get("vod_streamers") or [k for k, v in CFG["streamers"].items() if v.get("campaign")]
    prog_path = P / "vod_progress.json"
    try:
        prog = json.loads(prog_path.read_text())
    except Exception:  # noqa: BLE001
        prog = {}
    h = twitch_headers()
    OUT.mkdir(parents=True, exist_ok=True)
    made = 0
    for login in logins:
        s = CFG["streamers"].get(login, {})
        try:
            uid = ((helix(h, "users", login=login).get("data") or [{}])[0]).get("id")
            vids = helix(h, "videos", user_id=uid, type="archive", first=2).get("data") or [] if uid else []
        except Exception as e:  # noqa: BLE001
            log(f"{login}: twitch lookup failed {e}")
            continue
        for v in vids[:1]:
            created = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
            dur = parse_dur(v.get("duration", ""))
            live = (created + timedelta(seconds=dur)) > NOW - timedelta(minutes=10)
            end = dur - (TAIL_SKIP_S if live else 0)
            oldest_ok = max(0, int((NOW - timedelta(hours=MAX_BACK_H) - created).total_seconds()))
            start = max(int(prog.get(v["id"], 0)), oldest_ok)
            if end - start < 180:
                log(f"{s.get('name', login)}: nothing new in '{v['title'][:50]}' ({'live' if live else 'ended'})")
                continue
            start = max(start, end - CHUNK_MIN * 60)   # newest part first; older unseen parts are no longer fresh
            log(f"{s.get('name', login)}: checking {start // 60}–{end // 60} min of '{v['title'][:50]}' ({'LIVE' if live else 'ended'})")
            tmp = Path(tempfile.mkdtemp())
            try:
                audio, _, _ = fetch_link_audio(v["url"], f"{start}-{end}", tmp)
                try:
                    chat = chat_rate(v["id"], start, end)
                except Exception as e:  # noqa: BLE001
                    chat = None
                    log(f"chat skipped: {e}")
                moments = viral.find_moments(audio, max_clips=PER_CHUNK, log=log, streamer=s.get("name", login), chat=chat)
            except Exception as e:  # noqa: BLE001
                log(f"{login}: VOD audio failed {str(e)[:150]}")
                shutil.rmtree(tmp, ignore_errors=True)
                continue
            for i, m in enumerate(moments):
                if m["score"] < 1.0 and m.get("ai_score") is None:
                    continue   # weak moment
                a, b = start + m["start"], start + m["end"]
                cid = f"vod{v['id']}_{int(a)}"
                raw = OUT / f"{cid}.mp4"
                try:
                    fetch_link_section(v["url"], a, b, raw, tmp)
                except Exception as e:  # noqa: BLE001
                    log(f"{cid}: section download failed {str(e)[:120]}")
                    continue
                meta = {"id": cid, "login": login, "streamer": s.get("name", login), "tag": s.get("tag", ""),
                        "campaign": s.get("campaign", ""), "title": f"{v['title'][:80]} @ {int(a // 3600)}:{int(a % 3600 // 60):02d}:{int(a % 60):02d}",
                        "created": (created + timedelta(seconds=float(a))).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "twitch": f"{v['url']}?t={int(a // 3600)}h{int(a % 3600 // 60)}m{int(a % 60)}s",
                        "transcript": m["text"], "hook": m.get("hook", ""), "hook_ai": bool(m.get("hook_ai")),
                        "moment": {k: m.get(k) for k in ("score", "reasons", "ai_score")}}
                (OUT / f"{cid}.json").write_text(json.dumps(meta, default=float))
                made += 1
                log(f"  moment {int(a // 60)}:{int(a % 60):02d} ({', '.join(m['reasons']) or 'good'}) {m.get('hook', '')}")
            prog[v["id"]] = end
            shutil.rmtree(tmp, ignore_errors=True)
    # keep progress small: only broadcasts from the last 2 days
    prog = {k: v for k, v in list(prog.items())[-40:]}
    prog_path.write_text(json.dumps(prog, indent=1))
    log(f"VOD miner: {made} new moments")


if __name__ == "__main__":
    main()

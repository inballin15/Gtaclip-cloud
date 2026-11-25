"""Put an AI-made thumbnail (made with the user's Higgsfield credits, from frames of the video) on a long video:
first in its thumbnail options + the picture on its card. Run by aithumb.yml with LT_ID and LT_URL."""
import json
import os
import re
import sys
from pathlib import Path

import requests

import longform as L

vid, url = os.environ["LT_ID"].strip(), os.environ["LT_URL"].strip()
if not re.fullmatch(r"[\w-]{3,40}", vid) or not re.match(r"https://[\w.-]+\.cloudfront\.net/[\w./-]+\.(png|jpe?g|webp)$", url):
    sys.exit("bad id or url")
raw = L.WORK / "ai_raw"
raw.write_bytes(requests.get(url, timeout=120).content)
out = L.WORK / f"{vid}_ai.jpg"
L.ff("-i", str(raw), "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720", "-q:v", "2", str(out))
L.git("fetch", "-q", "--depth=1", "origin", "+gh-pages:refs/remotes/origin/gh-pages")
cur = json.loads(L.git("show", "origin/gh-pages:longs.json"))
e = next((v for v in cur.get("videos", []) if v.get("id") == vid), None)
if not e:
    sys.exit(f"no long video {vid}")
k = 1 + sum(1 for t in e.get("thumbs", []) if "_ai" in t)
name = f"{vid}_ai{k}.jpg"
link = L.release_upload(out, name, "image/jpeg")
e["thumbs"] = [link] + [t for t in e.get("thumbs", []) if t != link]
e["thumb_full"] = link
L.publish_site(e, {f"v/l/{vid}.jpg": out})
print("AI thumbnail added:", link)

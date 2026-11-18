"""Write site/queue.json: the clips waiting to post (gtaclip_new) and recently posted ones.
Only public info (titles, streamer, public Cloudinary video links). No secrets."""
import json
import os
import time
from datetime import datetime, timezone

import cloudinary
import cloudinary.api
import cloudinary.uploader

raw = os.environ.get("CLOUDINARY_URL", "").strip().strip('"').strip("'")
if raw.upper().startswith("CLOUDINARY_URL="):
    raw = raw.split("=", 1)[1].strip()
os.environ["CLOUDINARY_URL"] = raw
cloudinary.reset_config()
cloudinary.config(secure=True)
CLOUD = cloudinary.config().cloud_name
BASE = f"https://res.cloudinary.com/{CLOUD}/video/upload"


def post_preview(pid, fc):
    cam = ""
    try:
        x, y, w, h = [float(v) for v in fc.split(",")]
        if w > 0:
            cam = (f"l_video:{pid.replace('/', ':')}/ac_none/c_crop,g_north_west,h_{h},w_{w},x_0,y_0/"
                   f"c_scale,w_560/bo_4px_solid_black/fl_layer_apply,g_north_east,y_250/")
    except Exception:  # noqa: BLE001
        pass
    return f"{BASE}/c_fill,g_center,h_1920,w_1080/{cam}q_auto/f_mp4/{pid}.mp4"


# Light phone-friendly copy (720p-ish, smaller file) and the 9:16 "as a post" copy.
PREVIEW_TX = "c_limit,w_960/q_auto:good/vc_h264/f_mp4"
POSTER_TX = "so_3,c_fill,w_640,h_360/q_auto/f_jpg"


def post_tx(pid, fc):
    return post_preview(pid, fc).split("/upload/", 1)[1].rsplit("/", len(pid.split("/")))[0]


def warm(pid, fc):
    """Ask Cloudinary to build the preview copies now, so playback is instant later."""
    try:
        cloudinary.uploader.explicit(
            pid, type="upload", resource_type="video", eager_async=True,
            eager=[PREVIEW_TX, post_tx(pid, fc)])
        cloudinary.uploader.add_context({"warmed": str(int(time.time()))}, [pid], resource_type="video")
    except Exception as e:  # noqa: BLE001
        print(f"warm failed {pid}: {e}")


def rows(tag, do_warm=False):
    out, cursor = [], None
    while True:
        kw = dict(resource_type="video", context=True, tags=True, max_results=100)
        if cursor:
            kw["next_cursor"] = cursor
        res = cloudinary.api.resources_by_tag(tag, **kw)
        for r in res.get("resources", []):
            c = (r.get("context") or {}).get("custom", {})
            pid = r["public_id"]
            warmed = int(c.get("warmed", "0") or 0)
            if do_warm and not warmed:
                warm(pid, c.get("facecam", ""))
            ready_fast = warmed and time.time() - warmed > 180
            out.append({
                "id": pid,
                "streamer": c.get("streamer", ""),
                "title": c.get("title", ""),
                "campaign": c.get("campaign", ""),
                "views": int(c["views"]) if str(c.get("views", "")).isdigit() else None,
                "clipped": c.get("created") or r.get("created_at"),
                "saved": r.get("created_at"),
                "seconds": round(r.get("duration") or 0),
                "transcript": c.get("transcript", ""),
                "twitch": c.get("url", ""),
                "video": f"{BASE}/{PREVIEW_TX}/{pid}.mp4" if ready_fast else r.get("secure_url"),
                "poster": f"{BASE}/{POSTER_TX}/{pid}.jpg",
                "fast": bool(ready_fast),
                "as_post": post_preview(pid, c.get("facecam", "")),
            })
        cursor = res.get("next_cursor")
        if not cursor:
            return out


ready = rows("gtaclip_new", do_warm=True)
ready.sort(key=lambda r: (not r["campaign"], r["clipped"] or ""), reverse=False)
ready.sort(key=lambda r: r["clipped"] or "", reverse=True)
ready.sort(key=lambda r: not r["campaign"])
posted = sorted(rows("gtaclip_used"), key=lambda r: r["saved"] or "", reverse=True)[:12]
os.makedirs("site", exist_ok=True)
with open("site/queue.json", "w") as f:
    json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "ready": ready, "posted": posted}, f, indent=1)
print(f"queue.json: {len(ready)} ready, {len(posted)} posted")

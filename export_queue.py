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


def hook_enc(hook):
    from urllib.parse import quote
    return quote(hook, safe="").replace("%2C", "%252C").replace("%2F", "%252F")


def final_tx(pid, fc, hook):
    """Exactly what gets posted: boat layout + the hook text (same as the posting rules)."""
    cam = post_tx(pid, fc).replace("c_fill,g_center,h_1920,w_1080/", "", 1).rsplit("q_auto/f_mp4", 1)[0]
    return (f"c_fill,g_center,h_1920,w_1080/{cam}bo_6px_solid_black,c_fit,co_white,"
            f"l_text:Arial_68_bold_stroke_center:{hook_enc(hook)},w_900/fl_layer_apply,g_north,y_1200/q_auto:best/f_mp4")


def post_tx(pid, fc):
    return post_preview(pid, fc).split("/upload/", 1)[1].rsplit("/", len(pid.split("/")))[0]


def warm(pid, fc, hook="", only_final=False):
    """Ask Cloudinary to build the preview copies now, so playback is instant later."""
    try:
        eager = [] if only_final else [PREVIEW_TX, post_tx(pid, fc)]
        if hook:
            eager.append(final_tx(pid, fc, hook))
        cloudinary.uploader.explicit(pid, type="upload", resource_type="video", eager_async=True, eager=eager)
        ctx = {"warmed_hook": hook} if only_final else {"warmed": str(int(time.time())), "warmed_hook": hook}
        cloudinary.uploader.add_context(ctx, [pid], resource_type="video")
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
            hook = c.get("hook", "")
            if do_warm and not warmed:
                warm(pid, c.get("facecam", ""), hook)
            elif do_warm and hook and c.get("warmed_hook", "") != hook:
                warm(pid, c.get("facecam", ""), hook, only_final=True)
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
                "hook": hook,
                "caption": c.get("caption", ""),
                "tag": c.get("tag", ""),
                "login": c.get("login", ""),
                "final": f"{BASE}/{final_tx(pid, c.get('facecam', ''), hook)}/{pid}.mp4" if hook else "",
            })
        cursor = res.get("next_cursor")
        if not cursor:
            return out


ready = rows("gtaclip_new", do_warm=True)
ready.sort(key=lambda r: r["clipped"] or "", reverse=True)
ready.sort(key=lambda r: not r["campaign"])


def posted_list():
    """Posts with live links: the hand-kept history plus any clip a posting run marked
    with tiktok/youtube links in its Cloudinary context."""
    out = {}
    try:
        for p in json.load(open("posts_history.json")):
            out[p.get("tiktok") or p.get("youtube")] = p
    except Exception as e:  # noqa: BLE001
        print(f"history: {e}")
    try:
        res = cloudinary.api.resources_by_tag("gtaclip_used", resource_type="video", context=True, max_results=100)
        for r in res.get("resources", []):
            c = (r.get("context") or {}).get("custom", {})
            if not (c.get("tiktok_url") or c.get("youtube_url")):
                continue
            pid = r["public_id"]
            key = c.get("tiktok_url") or c.get("youtube_url")
            out[key] = {
                "hook": c.get("hook") or c.get("title", ""), "streamer": c.get("streamer", ""),
                "campaign": c.get("campaign", ""), "posted": c.get("posted_at") or r.get("created_at"),
                "tiktok": c.get("tiktok_url", ""), "youtube": c.get("youtube_url", ""),
                "poster": f"{BASE}/{POSTER_TX}/{pid}.jpg",
            }
    except Exception as e:  # noqa: BLE001
        print(f"used: {e}")
    return sorted(out.values(), key=lambda p: p.get("posted") or "", reverse=True)[:40]


def fetcher_status():
    try:
        log = open("last_run.log").read()
    except Exception:  # noqa: BLE001
        return {}
    import re
    times = re.findall(r"^\[(\d\d:\d\d:\d\d)Z\]", log, re.M)
    found = {m.group(1): int(m.group(2)) for m in re.finditer(r"\] (.+?): (\d+) fresh GTA 6 clips", log)}
    return {"last_run_utc": times[-1] if times else "", "uploaded": len(re.findall(r"\] uploaded ", log)),
            "failed": len(re.findall(r"\] failed ", log)), "fresh_by_streamer": found}


os.makedirs("site", exist_ok=True)
with open("site/queue.json", "w") as f:
    json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "ready": ready, "posted": posted_list(), "fetcher": fetcher_status()}, f, indent=1)
print(f"queue.json: {len(ready)} ready")

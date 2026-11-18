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


from facecam import cam_shows_face, find_cam  # noqa: E402

# Light phone-friendly copy (720p-ish, smaller file).
PREVIEW_TX = "c_limit,w_960/q_auto:good/vc_h264/f_mp4"
POSTER_TX = "so_3,c_fill,w_640,h_360/q_auto/f_jpg"


def hook_enc(hook):
    from urllib.parse import quote
    return quote(hook, safe="").replace("%2C", "%252C").replace("%2F", "%252F")


def split_tx(pid, cam, hook=""):
    """The posting layout (split screen): streamer's webcam on top, gameplay on the bottom,
    hook in a white rounded box with black bold text across the middle."""
    x, y, w, h = [float(v) for v in cam.split(",")]
    tx = (f"c_fill,g_center,h_960,w_1080/c_pad,b_black,g_south,h_1920,w_1080/"
          f"l_video:{pid.replace('/', ':')}/ac_none/c_crop,g_north_west,h_{h},w_{w},x_{x},y_{y}/"
          f"c_fill,h_960,w_1080/fl_layer_apply,g_north/")
    if hook:
        tx += (f"l_text:Arial_64_bold_center:{hook_enc(hook)},co_black,b_white,c_fit,w_880/"
               f"bo_24px_solid_white/r_28/fl_layer_apply,g_center/")
    return tx + "q_auto:best/f_mp4"


def warm(pid, cam, hook="", only_final=False):
    """Ask Cloudinary to build the preview copies now, so playback is instant later."""
    try:
        eager = [] if only_final else [PREVIEW_TX, split_tx(pid, cam)]
        if hook:
            eager.append(split_tx(pid, cam, hook))
        cloudinary.uploader.explicit(pid, type="upload", resource_type="video", eager_async=True, eager=eager)
        ctx = {"warmed_hook": hook} if only_final else {"warmed": str(int(time.time())), "warmed_hook": hook}
        cloudinary.uploader.add_context(ctx, [pid], resource_type="video")
    except Exception as e:  # noqa: BLE001
        print(f"warm failed {pid}: {e}")


def cam_for(pid, c):
    """Webcam box for the split layout. Detected per clip (streamers move their cam), falling
    back to the streamer's usual box. A clip only qualifies if the top half really shows a face."""
    if c.get("cam"):
        return c["cam"]
    cam = ""
    for cand in (find_cam(CLOUD, pid, 0), c.get("facecam", "")):
        if cand and cam_shows_face(CLOUD, pid, cand):
            cam = cand
            break
    if cam:
        cloudinary.uploader.add_context({"cam": cam}, [pid], resource_type="video")
    else:
        # No webcam face -> can't make the split layout -> never post it.
        cloudinary.uploader.add_tag("gtaclip_skip", [pid], resource_type="video")
        cloudinary.uploader.remove_tag("gtaclip_new", [pid], resource_type="video")
        cloudinary.uploader.add_context({"skip_reason": "no webcam face found"}, [pid], resource_type="video")
        print(f"no webcam face -> skipped {pid}")
    return cam


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
            cam = cam_for(pid, c) if do_warm else c.get("cam", "")
            if not cam:
                continue
            warmed = int(c.get("warmed", "0") or 0) if c.get("cam") else 0
            hook = c.get("hook", "")
            if do_warm and not warmed:
                warm(pid, cam, hook)
            elif do_warm and hook and c.get("warmed_hook", "") != hook:
                warm(pid, cam, hook, only_final=True)
            ready_fast = warmed and time.time() - warmed > 180
            out.append({
                "id": pid,
                "streamer": c.get("streamer", ""),
                "title": c.get("title", ""),
                "campaign": c.get("campaign", ""),
                "views": int(c["views"]) if str(c.get("views", "")).isdigit() else None,
                "clipped": c.get("created") or r.get("created_at"),
                "saved": r.get("created_at"),
                "transcript": c.get("transcript", ""),
                "twitch": c.get("url", ""),
                "video": f"{BASE}/{PREVIEW_TX}/{pid}.mp4" if ready_fast else r.get("secure_url"),
                "poster": f"{BASE}/{POSTER_TX}/{pid}.jpg",
                "cam": cam,
                "as_post": f"{BASE}/{split_tx(pid, cam)}/{pid}.mp4",
                "hook": hook,
                "caption": c.get("caption", ""),
                "tag": c.get("tag", ""),
                "login": c.get("login", ""),
                "final": f"{BASE}/{split_tx(pid, cam, hook)}/{pid}.mp4" if hook else "",
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

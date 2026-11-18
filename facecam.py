"""Find where the streamer's webcam is in a clip, for the split-screen layout.
Looks at 3 frames with Cloudinary face detection and keeps a face that stays put
(a webcam doesn't move; game characters do). Saves context cam=x,y,w,h (fractions)."""
import math

import cloudinary
import cloudinary.uploader

TOP_ASPECT = 1080 / 960  # top half of the 9:16 video


def _faces(cloud, pid, sec):
    url = f"https://res.cloudinary.com/{cloud}/video/upload/so_{sec}/f_jpg/{pid}.jpg"
    try:
        r = cloudinary.uploader.upload(url, public_id=f"gtaclip_frames/{pid.split('/')[-1]}_{sec}",
                                       faces=True, overwrite=True, resource_type="image")
        return r.get("faces") or [], r.get("width", 1920), r.get("height", 1080)
    except Exception as e:  # noqa: BLE001
        print(f"face check failed {pid} @{sec}s: {e}")
        return [], 1920, 1080


def find_cam(cloud, pid, duration):
    secs = [1, 4, 7]  # every clip is at least 8s long
    frames = [_faces(cloud, pid, s) for s in secs]
    W, H = frames[0][1], frames[0][2]
    best = None
    for fi, (faces, _, _) in enumerate(frames):
        for (x, y, w, h) in faces:
            cx, cy = x + w / 2, y + h / 2
            hits = 1
            for fj, (others, _, _) in enumerate(frames):
                if fj == fi:
                    continue
                if any(math.hypot(ox + ow / 2 - cx, oy + oh / 2 - cy) < 0.6 * w and 0.65 < ow / w < 1.5
                       for (ox, oy, ow, oh) in others):
                    hits += 1
            if hits >= 2 and w >= 28 and (best is None or w * h > best[2] * best[3]):
                best = (x, y, w, h)
    if not best:
        return ""
    x, y, w, h = best
    cx, cy = x + w / 2, y + h * 0.62  # a bit below eye line so shoulders show
    bw = min(W, max(w * 4.0, 260))
    bh = bw / TOP_ASPECT
    if bh > H:
        bh, bw = H, H * TOP_ASPECT
    bx = min(max(0, cx - bw / 2), W - bw)
    by = min(max(0, cy - bh / 2), H - bh)
    return f"{bx / W:.4f},{by / H:.4f},{bw / W:.4f},{bh / H:.4f}"


def cam_shows_face(cloud, pid, cam):
    """Final check before a clip can post: the top half (the webcam crop) must show a face
    in at least one of two frames."""
    x, y, w, h = cam.split(",")
    for sec in (2, 5):
        url = (f"https://res.cloudinary.com/{cloud}/video/upload/so_{sec}/"
               f"c_crop,g_north_west,h_{h},w_{w},x_{x},y_{y}/c_fill,h_960,w_1080/f_jpg/{pid}.jpg")
        try:
            r = cloudinary.uploader.upload(url, public_id=f"gtaclip_frames/{pid.split('/')[-1]}_top{sec}",
                                           faces=True, overwrite=True, invalidate=True, resource_type="image")
            if r.get("faces"):
                return True
        except Exception as e:  # noqa: BLE001
            print(f"top-half check failed {pid}: {e}")
    return False

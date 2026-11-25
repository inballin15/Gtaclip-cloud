"""Quick text edit: put new words (or a new box style / size / spot) on a clip in about a minute.

The site sends a "text: <clip id>" tap with JSON like {"hook": "...", "caption": "...", "hook_style": "black"}.
We take the clip's clean copy (the same video with no text box), draw the new text box on it and save the
result to the `edits` branch. The site, the poster and the clip maker all read edits/edits.json, so the
new version is used everywhere right away. Nothing else about the video is touched.

Anything we can't do quickly (a webcam-only clip with no chosen text spot, a missing clean copy) falls back to
the normal re-make, so the edit is never lost.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import imageio_ffmpeg

from render_hook import render as render_hook
from render_hook import strip_emoji

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
ISSUE = json.loads(os.environ["ISSUE_JSON"])
FF = imageio_ffmpeg.get_ffmpeg_exe()
OWNER, NAME = REPO.split("/")
PAGES = os.environ.get("SITE_URL", "https://gtaclip-studio.pages.dev") + "/"
# where to download site files from, freshest first (the GitHub copy has no wait for the Cloudflare upload)
SITE_SOURCES = [f"https://raw.githubusercontent.com/{OWNER}/{NAME}/gh-pages/", PAGES, f"https://{OWNER.lower()}.github.io/{NAME}/"]


def site_get(path: str, out) -> None:
    last = None
    for base in SITE_SOURCES:
        try:
            urllib.request.urlretrieve(base + path, out)
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise last
WORK = Path("/tmp/qe")
WORK.mkdir(exist_ok=True)
TOP_H = 672
CAM_PX = {"small": 560, "medium": 672}


def gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        t = r.read()
        return json.loads(t) if t else {}


def say(msg: str, close: bool = True) -> None:
    gh("POST", f"/issues/{ISSUE['number']}/comments", {"body": msg})
    if close:
        gh("PATCH", f"/issues/{ISSUE['number']}", {"state": "closed"})


def clip_dir(cid: str) -> str:
    """Where a clip's videos are on the site: your clips (ids "up...") have their own folder."""
    return "v/m/" if cid.startswith("up") else "v/"


def fallback(cid: str, edit: dict, why: str, ul: dict | None = None) -> None:
    """Can't do it the quick way: hand it to the clip maker as a normal edit (it re-makes the whole video)."""
    print("fallback:", why)
    body = f"hook: {edit.get('hook', '')}\ncaption: {edit.get('caption', '')}" if "hook_style" not in edit else \
        json.dumps({**(ul or {}), **edit})   # keep your earlier layout (a layout tap replaces it all)
    title = f"edit: {cid}" if "hook_style" not in edit and "hook_y" not in edit and "hook_scale" not in edit else f"layout: {cid}"
    # turn this same tap into a normal edit (it stays yours, so the clip maker accepts it) and start the clip maker
    gh("PATCH", f"/issues/{ISSUE['number']}", {"title": title, "body": body})
    gh("POST", f"/actions/workflows/{'mine' if cid.startswith('up') else 'pipeline'}.yml/dispatches", {"ref": "main"})   # your clips: own maker
    say(f"This one needs a full re-make ({why}) — the clip maker is on it now.", close=False)


def main() -> None:
    m = re.match(r"^\s*text:\s*(\S+)", ISSUE["title"])
    key = m.group(1) if m else ""
    try:
        raw = ISSUE.get("body") or "{}"
        req = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001
        say("Couldn't read that edit.")
        return
    edit: dict = {}
    if isinstance(req.get("hook"), str) and req["hook"].strip():
        edit["hook"] = strip_emoji(req["hook"]).strip()[:140]
    if isinstance(req.get("caption"), str):
        edit["caption"] = req["caption"].strip()[:2000]
    if req.get("hook_style") in ("white", "black", "outline"):
        edit["hook_style"] = req["hook_style"]
    for k, lo, hi in (("hook_y", 0.05, 0.95), ("hook_scale", 0.6, 1.6)):
        if isinstance(req.get(k), (int, float)):
            edit[k] = max(lo, min(hi, float(req[k])))

    st = json.loads(Path("/tmp/state.json").read_text())
    clips = st.get("clips", {})
    cid = next((k for k in clips if k == key or k.startswith(key) or key.startswith(k)), None)
    if not cid:
        say("Couldn't find that clip (it may have expired).")
        return
    c = clips[cid]
    edits = json.loads(Path("/tmp/edits/edits.json").read_text()) if Path("/tmp/edits/edits.json").exists() else {}
    prev = edits.get(cid, {})
    # newest wins: start from the last quick edit of this clip (if it's still current), then this one
    if prev and prev.get("at", 0) > (c.get("rendered") or 0):
        base = {k: prev[k] for k in ("hook", "caption", "hook_style", "hook_y", "hook_scale") if k in prev}
    else:
        base = {}
    full = {**base, **edit}
    print("STEP 1/3 got the clip", cid, json.dumps(full))

    ul = c.get("user_layout") or {}
    sty = c.get("style") or {}
    hook = full.get("hook") or c.get("hook") or ""
    style = full.get("hook_style") or ul.get("hook_style") or sty.get("box") or "white"
    scale = float(full.get("hook_scale", ul.get("hook_scale", 1.0)))
    hy = full.get("hook_y", ul.get("hook_y"))
    lay = c.get("layout_used") or c.get("layout") or "split"
    if c.get("cam_only"):
        lay = "cam"
    if lay == "cam" and hy is None:
        fallback(cid, edit, "webcam-only clip", ul)
        return

    clean = WORK / "clean.mp4"
    try:
        site_get(f"{clip_dir(cid)}{cid}_clean.mp4?t={int(time.time())}", clean)
    except Exception as e:  # noqa: BLE001
        fallback(cid, edit, f"no clean copy: {e}", ul)
        return
    if not hook:   # caption-only change: no new video needed
        out_name = ""
    else:
        png = render_hook(hook, str(WORK / "hook.png"), style, scale)
        T = int(ul.get("top_h") or CAM_PX.get(sty.get("cam", ""), TOP_H))
        oy = (f"H*{hy:.3f}-h/2" if hy is not None
              else "(H-h)/2" if lay == "full" else "'max(150,400-h-26)'" if lay == "react" else "H*0.2-h/2" if lay == "fill" else "H*0.6" if lay.startswith("irl")
              else f"{T}-h*0.2")
        stamp = int(time.time())
        out_name = f"v/e/{cid}_{stamp}.mp4"   # same path on the edits branch and on the site
        out = Path("/tmp/edits") / out_name
        out.parent.mkdir(parents=True, exist_ok=True)
        print("STEP 2/3 adding your text")
        import engage   # hook + the follow pop-up (+ progress bar if this clip tests it), like every posted video
        engage.hooked(FF, clean, png, out, lay, hy, T, WORK, pbar=sty.get("pbar") == "on")
    # keep this clip's newest edit only; drop edits (and their videos) older than 3 days
    old = edits.get(cid, {}).get("video")
    rec = {**full, "at": int(time.time()), "video": out_name or (prev.get("video") if base else "")}
    if not rec["video"]:
        rec.pop("video")
    edits[cid] = rec
    keep_after = time.time() - 3 * 86400
    for k in [k for k, v in edits.items() if v.get("at", 0) < keep_after]:
        edits.pop(k)
    live_files = {v.get("video") for v in edits.values()}
    for f in (Path("/tmp/edits") / "v" / "e").glob("*.mp4"):
        if f"v/e/{f.name}" not in live_files:
            f.unlink()
    if old and old not in live_files:
        (Path("/tmp/edits") / old).unlink(missing_ok=True)
    Path("/tmp/edits/edits.json").write_text(json.dumps(edits, indent=1, ensure_ascii=False))
    Path("/tmp/qe_done").write_text(cid)
    Path("/tmp/qe_entry.json").write_text(json.dumps({"id": cid, "rec": rec}))   # for merging if another edit saved first
    print("STEP 3/3 saving")
    if out_name:
        import shutil
        shutil.copy(Path("/tmp/edits") / out_name, "/tmp/qe_video.mp4")
        # instant link: a release file is live the moment it's uploaded (the website itself can take minutes to
        # republish a 1 GB site), so the site and the poster use this link right away
        try:
            rec["url"] = upload_asset(Path("/tmp/edits") / out_name)
            edits[cid] = rec
            Path("/tmp/edits/edits.json").write_text(json.dumps(edits, indent=1, ensure_ascii=False))
            Path("/tmp/qe_entry.json").write_text(json.dumps({"id": cid, "rec": rec}))
            print("instant link:", rec["url"])
        except Exception as e:  # noqa: BLE001
            print("instant link failed (the site copy still comes):", e)
        try:
            put_on_site(out_name, Path("/tmp/edits") / out_name)
        except Exception as e:  # noqa: BLE001
            print("site copy failed (instant link still works):", e)


def upload_asset(f: Path) -> str:
    try:
        rel = gh("GET", "/releases/tags/quick-edits")
    except Exception:  # noqa: BLE001
        rel = gh("POST", "/releases", {"tag_name": "quick-edits", "name": "Quick edits (temporary videos)",
                                       "body": "Videos from quick text edits. Old ones are removed automatically.",
                                       "target_commitish": "main", "prerelease": True})
    # tidy: remove edit videos older than 3 days
    try:
        for a in gh("GET", f"/releases/{rel['id']}/assets?per_page=100"):
            if time.time() - time.mktime(time.strptime(a["created_at"], "%Y-%m-%dT%H:%M:%SZ")) > 3 * 86400:
                gh("DELETE", f"/releases/assets/{a['id']}")
    except Exception as e:  # noqa: BLE001
        print("asset tidy skipped:", e)
    req = urllib.request.Request(f"https://uploads.github.com/repos/{REPO}/releases/{rel['id']}/assets?name={f.name}",
                                 data=f.read_bytes(), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "video/mp4",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["browser_download_url"]


def put_on_site(path: str, f: Path) -> None:
    """Add one file to the published site (gh-pages) on top of whatever is there now — no big download."""
    import base64
    blob = gh("POST", "/git/blobs", {"content": base64.b64encode(f.read_bytes()).decode(), "encoding": "base64"})["sha"]
    for i in range(6):
        tip = gh("GET", "/git/ref/heads/gh-pages")["object"]["sha"]
        tree = gh("GET", f"/git/commits/{tip}")["tree"]["sha"]
        nt = gh("POST", "/git/trees", {"base_tree": tree, "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob}]})["sha"]
        cm = gh("POST", "/git/commits", {"message": f"quick edit {path}", "tree": nt, "parents": [tip]})["sha"]
        try:
            gh("PATCH", "/git/refs/heads/gh-pages", {"sha": cm, "force": False})
            print("on the site:", path)
            return
        except Exception as e:  # noqa: BLE001  (the site moved on meanwhile: try again on top of it)
            print("retry site push", e)
            time.sleep(2 + 3 * i)
    raise RuntimeError("couldn't update the site")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("quick edit failed:", e)
        say(f"Quick edit failed ({e}). Try again, or use Layout to re-make it.")
        sys.exit(1)

"""Fast lane for layout edits (Nov 25): a 'layout: <clip id>' tap from the Studio (webcam box, layout, trim, speed,
text position) is re-made here right away - on its own, without waiting for the clip maker's current run (that
wait was up to 20+ minutes). The finished video goes out like a quick text edit: an instant link + the site copy,
listed in edits/edits.json, so the site, the Save button and the poster use it straight away.

The tap itself stays open: the clip maker still applies it on its next run (same settings -> same video), so the
saved state always has your edit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
W = Path("/tmp/fe")


def main() -> None:
    issue = json.loads(os.environ.get("ISSUE_JSON") or "{}")
    title, body = issue.get("title", ""), issue.get("body") or ""
    m = re.match(r"^\s*layout:\s*(?:gtaclip/)?(?:tw_[a-z0-9_]+?_)?([A-Za-z0-9_\-]+)\s*$", title, re.I)
    if not m:
        print("not a layout edit")
        return
    key = m.group(1)
    full = json.loads(Path("/tmp/state.json").read_text())
    cid = next((k for k in full.get("clips", {}) if k == key or k.startswith(key) or key.startswith(k)), None)
    if not cid:
        print("clip not found:", key)
        return
    c = full["clips"][cid]
    if c.get("status") != "new":
        print("clip is not in the queue:", cid, c.get("status"))
        return
    for d in ("raw", "site/v", "st", "posted"):
        (W / d).mkdir(parents=True, exist_ok=True)
    os.environ.update(RAW_DIR=str(W / "raw"), SITE_OUT=str(W / "site"), STATE_DIR=str(W / "st"),
                      POSTED_DIR=str(W / "posted"), FETCH="0", X264_PRESET="veryfast",
                      MINE_MODE="1" if cid.startswith("up") else "0")
    os.environ.pop("GEMINI_API_KEY", None)   # no AI calls here: the clip was checked already
    if Path("/tmp/control.json").exists():   # your settings (follow line voice)
        shutil.copy("/tmp/control.json", W / "posted" / "control.json")
    sys.path.insert(0, str(ROOT))
    import pipeline as P
    print(P.apply_layout(c, body))
    # just this clip; the one-time fixes are marked done so nothing else runs
    small = {k: v for k, v in full.items() if k != "clips"}
    small["clips"] = {cid: c}
    (W / "st" / "state.json").write_text(json.dumps(small))
    for name in ("apply_issues", "import_vod_clips"):
        setattr(P, name, lambda *a, **k: None)
    P.process_uploads = lambda *a, **k: None
    t0 = time.time()
    P.main()
    out = W / "site" / "v" / f"{cid}_final.mp4"
    if not out.exists():
        raise RuntimeError("the video wasn't made (see the log above)")
    print(f"made in {time.time() - t0:.0f}s: {out.stat().st_size // 1_000_000} MB")
    c2 = json.loads((W / "st" / "state.json").read_text())["clips"][cid]
    # out like a quick edit: edits.json entry + the video on the edits branch / site / an instant link
    import quickedit as qe
    stamp = int(time.time())
    name = f"v/e/{cid}_{stamp}.mp4"
    dst = Path("/tmp/edits") / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(out, dst)
    ep = Path("/tmp/edits/edits.json")
    edits = json.loads(ep.read_text()) if ep.exists() else {}
    old = (edits.get(cid) or {}).get("video")
    rec = {"at": stamp, "video": name, "layout": True, "hook": c2.get("hook") or "",
           "final_s": c2.get("final_s"), "span": c2.get("span")}
    if c2.get("caption"):
        rec["caption"] = c2["caption"]
    edits[cid] = rec
    if old and old != name:
        (Path("/tmp/edits") / old).unlink(missing_ok=True)
    try:
        rec["url"] = qe.upload_asset(dst)
        print("instant link:", rec["url"])
    except Exception as e:  # noqa: BLE001
        print("instant link failed (the site copy still comes):", e)
    ep.write_text(json.dumps(edits, indent=1, ensure_ascii=False))
    Path("/tmp/qe_done").write_text(cid)
    Path("/tmp/qe_entry.json").write_text(json.dumps({"id": cid, "rec": rec}))
    shutil.copy(dst, "/tmp/qe_video.mp4")
    try:
        qe.put_on_site(name, dst)
    except Exception as e:  # noqa: BLE001
        print("site copy failed (the instant link still works):", e)


if __name__ == "__main__":
    main()

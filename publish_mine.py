"""Publish the your-clips maker's work onto the live site WITHOUT touching anything else on it.

The your-clips maker (mine.yml, pipeline.py in MINE_MODE) makes your pasted / uploaded clips into _minesite/.
This puts them on the site as:
  v/m/<clip files>      your clips' videos + pictures
  mine.json             your clips list (same format as queue.json; the site and the poster read both)
  v/e/up*.mp4           quick-edit videos of your clips
as a new commit ON TOP of the live site (never replacing it), so the auto clip finder's videos stay as they are.
If the site changed in the meantime (the auto finder published), it starts again from the new version.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

SITE = Path("_minesite")
PAGES = "https://inballin15.github.io/Gtaclip-cloud"


def git(*args: str, inp: bytes | None = None, env: dict | None = None) -> str:
    import os
    r = subprocess.run(["git", *args], input=inp, capture_output=True, env={**os.environ, **(env or {})})
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.decode()[:300]}")
    return r.stdout.decode().strip()


def mine_json() -> bytes:
    """queue.json of the your-clips run -> mine.json with every video path pointing into v/m/ (edits stay in v/e/)."""
    q = json.loads((SITE / "queue.json").read_text())
    txt = json.dumps({"updated": q.get("updated"), "ready": q.get("ready", [])}, indent=1, ensure_ascii=False)
    txt = re.sub(r'"v/(?!e/|m/)', '"v/m/', txt)
    txt = re.sub(re.escape(PAGES) + r"/v/(?!e/|m/)", PAGES + "/v/m/", txt)
    return txt.encode()


def files() -> dict[str, Path]:
    out = {}
    vid = SITE / "v"
    for f in vid.glob("*"):
        if f.is_file() and f.suffix in (".mp4", ".jpg", ".png"):
            out[f"v/m/{f.name}"] = f
    for f in (vid / "e").glob("up*.mp4") if (vid / "e").exists() else []:
        out[f"v/e/{f.name}"] = f
    return out


def publish() -> None:
    want = files()
    blob_of = {path: git("hash-object", "-w", str(f)) for path, f in want.items()}
    mj = git("hash-object", "-w", "--stdin", inp=mine_json())
    for attempt in range(8):
        git("fetch", "-q", "--depth=1", "origin", "+gh-pages:refs/remotes/origin/gh-pages")
        base = git("rev-parse", "origin/gh-pages")
        idx = {"GIT_INDEX_FILE": "/tmp/idx-mine"}
        Path("/tmp/idx-mine").unlink(missing_ok=True)
        git("read-tree", base, env=idx)
        old = git("ls-files", "v/m", env=idx).splitlines()
        old += [p for p in git("ls-files", "v/e", env=idx).splitlines() if Path(p).name.startswith("up") and p not in want]
        for p in old:
            if p not in want:
                git("update-index", "--force-remove", p, env=idx)
        for path, sha in blob_of.items():
            git("update-index", "--add", "--cacheinfo", f"100644,{sha},{path}", env=idx)
        git("update-index", "--add", "--cacheinfo", f"100644,{mj},mine.json", env=idx)
        tree = git("write-tree", env=idx)
        if tree == git("rev-parse", f"{base}^{{tree}}"):
            print("site already up to date")
            return
        c = git("-c", "user.name=gtaclip-bot", "-c", "user.email=gtaclip-bot@users.noreply.github.com",
                "commit-tree", tree, "-p", base, "-m", f"your clips {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        try:
            git("push", "-q", "origin", f"{c}:refs/heads/gh-pages")
            print(f"your clips published: {len(want)} files")
            return
        except RuntimeError as e:   # the auto finder published meanwhile: start again from the new site
            print(f"site changed, retrying ({attempt + 1}): {str(e)[:120]}")
            time.sleep(3 + attempt * 3)
    sys.exit("couldn't publish your clips (the site kept changing)")


if __name__ == "__main__":
    publish()

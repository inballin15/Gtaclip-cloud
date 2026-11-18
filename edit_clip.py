"""Change a queued clip's on-screen hook and/or post caption (from a GitHub issue).
Issue title: 'edit: <public_id>'. Body has 'hook: ...' and/or 'caption: ...' (caption may span lines)."""
import os
import re
import sys

import cloudinary
import cloudinary.uploader

raw = os.environ.get("CLOUDINARY_URL", "").strip().strip('"').strip("'")
if raw.upper().startswith("CLOUDINARY_URL="):
    raw = raw.split("=", 1)[1].strip()
os.environ["CLOUDINARY_URL"] = raw
cloudinary.reset_config()
cloudinary.config(secure=True)

m = re.match(r"^\s*edit:\s*(gtaclip/[A-Za-z0-9_\-]+)\s*$", os.environ.get("ISSUE_TITLE", ""))
if not m:
    sys.exit("not an edit request")
pid = m.group(1)
body = os.environ.get("ISSUE_BODY", "").replace("\r\n", "\n")

ctx = {}
hm = re.search(r"^hook:[ \t]*(.*)$", body, re.M)
if hm:
    hook = hm.group(1).strip()
    # on-screen text: plain characters only (emojis break the video render)
    hook = hook.strip()[:120]
    ctx["hook"] = hook
cm = re.search(r"^caption:[ \t]*(.*)\Z", body, re.M | re.S)
if cm:
    ctx["caption"] = cm.group(1).strip()[:2000]
if not ctx:
    sys.exit("no hook: or caption: line found")
cloudinary.uploader.add_context(ctx, [pid], resource_type="video")
print(f"updated {pid}: {ctx}")

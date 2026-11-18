"""Mark a queued clip as 'don't post' (from a GitHub issue titled 'skip: <public_id>')."""
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

title = os.environ.get("ISSUE_TITLE", "")
m = re.match(r"^\s*skip:\s*(gtaclip/[A-Za-z0-9_\-]+)\s*$", title)
if not m:
    print(f"not a skip request: {title!r}")
    sys.exit(1)
pid = m.group(1)
cloudinary.uploader.add_tag("gtaclip_skip", [pid], resource_type="video")
cloudinary.uploader.remove_tag("gtaclip_new", [pid], resource_type="video")
cloudinary.uploader.add_context({"skipped_by": "nicholas"}, [pid], resource_type="video")
print(f"skipped {pid}")

"""GTA Clip poster — runs on GitHub Actions every 5 minutes and right after a dashboard tap.

Talks to Buffer's own API directly (no Claude, no Zapier), so it never hits usage limits.
  * Start / Stop        (issues  "pause: posting" / "resume: posting")
  * Post now            (issue   "post: <clip id>")
  * Regular slots       :07 and :37, 7:07 am - 10:37 pm New York time
  * Upload status       every post is tracked until TikTok + YouTube both show it live
  * Stats               views / likes / comments / shares / reposts for every post

Reads  : /tmp/queue.json  (the published site queue)
Writes : _posted/control.json, _posted/posted/<id>.json, _posted/live.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
P = Path(os.environ.get("POSTED_DIR", ROOT / "_posted"))
QUEUE = Path(os.environ.get("QUEUE_JSON", "/tmp/queue.json"))
REPO = os.environ.get("GITHUB_REPOSITORY", "inballin15/Gtaclip-cloud")
OWNER = REPO.split("/")[0]
GH_TOKEN = os.environ.get("GH_TOKEN", "")
BUFFER_KEY = os.environ.get("BUFFER_API_KEY", "").strip()
ORG = os.environ.get("BUFFER_ORG", "6afd2f02dfdc69bf665498b2")
CH = {"tiktok": os.environ.get("BUFFER_TIKTOK", "6afd3005ece6dfde5f04d0f4"),
      "youtube": os.environ.get("BUFFER_YOUTUBE", "6afd3fceece6dfde5f06164d")}
ET = ZoneInfo("America/New_York")
NOW = parse_now = datetime.fromisoformat(os.environ["FAKE_NOW"]) if os.environ.get("FAKE_NOW") else datetime.now(timezone.utc)
DRY_RUN = os.environ.get("DRY_RUN") == "1"   # test mode: never calls Buffer or GitHub
SLOT_GRACE_MIN = 25          # a slot can still go out up to 25 min late (GitHub's timer can run late)
REQUEST_MAX_AGE_MIN = 45     # Start/Stop/Post-now taps older than this are ignored (stale)
LOG: list[str] = []
QUEUE_BY_ID: dict = {}


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc):%H:%M:%SZ}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def rj(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return default


def wj(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------ http
def http(method: str, url: str, headers: dict, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**headers, "User-Agent": "gtaclip-poster"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(txt)
        except Exception:  # noqa: BLE001
            return e.code, {"message": txt[:300]}


def gh(method: str, path: str, body=None):
    return http(method, f"https://api.github.com{path}",
                {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}, body)


def buffer(query: str, variables: dict | None = None):
    code, res = http("POST", "https://api.buffer.com", {"Authorization": f"Bearer {BUFFER_KEY}"},
                     {"query": query, "variables": variables or {}})
    if code >= 400 and not res.get("errors"):
        raise RuntimeError(f"Buffer HTTP {code}: {res.get('message') or res}")
    if res.get("errors"):
        raise RuntimeError("Buffer: " + "; ".join(e.get("message", "?") for e in res["errors"])[:400])
    return res.get("data") or {}


# ------------------------------------------------------------------ dashboard requests (GitHub issues)
ID_RE = r"(?:gtaclip/)?(?:tw_[a-z0-9_]+?_)?([A-Za-z0-9_\-]+)"


def issues(state: str, since_hours: int = 72) -> list[dict]:
    since = iso(NOW - timedelta(hours=since_hours))
    code, res = gh("GET", f"/repos/{REPO}/issues?state={state}&creator={OWNER}&since={since}&per_page=100")
    if code != 200 or not isinstance(res, list):
        log(f"couldn't read dashboard requests ({code})")
        return []
    return [i for i in res if "pull_request" not in i]


def close_issue(num: int, msg: str) -> None:
    gh("POST", f"/repos/{REPO}/issues/{num}/comments", {"body": msg})
    gh("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})


def handle_requests(ctl: dict, ready_ids: set[str], posted_ids: set[str]) -> None:
    for it in sorted(issues("open", 24), key=lambda i: i["created_at"]):
        t = it["title"].strip()
        tl = t.lower()
        age_min = (NOW - parse(it["created_at"])).total_seconds() / 60
        kind = None
        if tl.startswith("pause:") or tl.startswith("resume:"):
            kind = "switch"
        elif re.match(r"^(post|postnow):", tl):
            kind = "post"
        elif tl.startswith("manual:"):
            kind = "manual"
        elif tl.startswith("ai:"):
            kind = "ai"
        if not kind:
            continue  # skip:/edit: belong to the render pipeline
        if age_min > REQUEST_MAX_AGE_MIN:
            close_issue(it["number"], "Expired — this tap is too old, so it was ignored. Tap again if you still want it.")
            log(f"expired request: {t}")
            continue
        if kind == "switch":
            on = tl.startswith("resume:")
            ctl["paused"] = not on
            ctl["switched_at"] = iso(NOW)
            msg = ("Automatic posting is ON — next post at the next :07 or :37 (7am–10:37pm)." if on
                   else "Automatic posting is STOPPED. Nothing posts on the schedule until you press Start.")
            close_issue(it["number"], msg)
            log(msg)
            continue
        if kind == "ai":
            ctl["ai_off"] = "off" in tl
            close_issue(it["number"], "AI viral scores turned OFF." if ctl["ai_off"] else "AI viral scores turned ON.")
            log(f"AI scoring {'off' if ctl['ai_off'] else 'on'}")
            continue
        if kind == "manual":
            m = re.match(r"^manual:\s*" + ID_RE + r"\s*$", t, re.I)
            cid = m.group(1) if m else ""
            cid = next((k for k in ready_ids if k == cid or k.startswith(cid) or cid.startswith(k)), cid)
            if cid in ready_ids and cid not in posted_ids:
                c = QUEUE_BY_ID.get(cid, {})
                wj(P / "posted" / f"{cid}.json", {"id": cid, "hook": c.get("hook", ""), "streamer": c.get("streamer", ""),
                                                  "campaign": c.get("campaign", ""), "why": "posted by Nicholas", "manual": True,
                                                  "posted_at": iso(NOW), "tiktok": "", "youtube": ""})
                posted_ids.add(cid)
                ctl["postnow"] = [p for p in ctl["postnow"] if p["id"] != cid]
                close_issue(it["number"], "Got it — marked as posted by you. The bot won't post this clip.")
                log(f"marked as posted manually: {cid}")
            else:
                close_issue(it["number"], "That clip isn't in the queue anymore.")
            continue
        m = re.match(r"^(?:post|postnow):\s*" + ID_RE + r"\s*$", t, re.I)
        cid = m.group(1) if m else ""
        cid = next((k for k in ready_ids if k == cid or k.startswith(cid) or cid.startswith(k)), cid)
        if cid in posted_ids:
            close_issue(it["number"], "Already posted — see the Posted tab.")
        elif cid not in ready_ids:
            close_issue(it["number"], "Couldn't find that clip in the queue (it may have been removed or expired).")
            log(f"post-now: clip not in queue {cid}")
        else:
            if not any(p["id"] == cid for p in ctl["postnow"]):
                ctl["postnow"].append({"id": cid, "at": iso(NOW)})
            close_issue(it["number"], "On it — posting to TikTok and YouTube now.")
            log(f"post-now requested: {cid}")


def blocked_ids() -> set[str]:
    """Clips Nicholas removed or is editing — don't post them even if the site hasn't caught up yet."""
    out = set()
    for it in issues("all", 72):
        m = re.match(r"^(skip|edit):\s*" + ID_RE + r"\s*$", it["title"].strip(), re.I)
        if not m:
            continue
        if m.group(1).lower() == "skip" or it["state"] == "open":
            out.add(m.group(2))
    return out


# ------------------------------------------------------------------ Buffer
CREATE = """mutation Create($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on MutationError { message }
  }
}"""

POSTS_FULL = """query Posts($input: PostsInput!, $first: Int) {
  posts(input: $input, first: $first) { edges { node {
    id status text externalLink sentAt dueAt createdAt channelId
    error { message }
    metrics { type value }
  } } }
}"""
POSTS_LITE = """query Posts($input: PostsInput!, $first: Int) {
  posts(input: $input, first: $first) { edges { node { id status text externalLink sentAt createdAt channelId } } }
}"""


def texts(c: dict) -> tuple[str, str, str]:
    hook = (c.get("hook") or c.get("title") or "").strip()
    login, streamer, tag = c.get("login", ""), c.get("streamer", ""), c.get("tag", "")
    cap = (c.get("caption") or "").strip()
    tt = cap or f"{hook}\n\n🎥 @{login}\n\n{tag} #GTA6 #GTAVI #gaming #fyp".strip()
    yt = cap or f"{hook}\n\nCredit: {streamer} (https://www.twitch.tv/{login})\n\n{tag} #GTA6 #GTAVI #Shorts".strip()
    title = (hook[:88] + " #Shorts").strip()
    return tt, yt, title


COOLDOWN_RE = re.compile(r"24 hours|daily video upload limit|daily posting limit|large number of posts|rate limit|too many", re.I)


def create(service: str, c: dict) -> dict:
    tt, yt, title = texts(c)
    if DRY_RUN:
        return {"state": "uploading", "buffer_id": f"dry-{service}-{c['id'][:8]}", "error": ""}
    inp = {"channelId": CH[service], "schedulingType": "automatic", "mode": "shareNow",
           "text": tt if service == "tiktok" else yt,
           "assets": [{"video": {"url": c["final_abs"]}}]}
    if service == "youtube":
        inp["metadata"] = {"youtube": {"title": title, "categoryId": "20", "privacy": "public", "license": "youtube",
                                       "notifySubscribers": True, "embeddable": True, "madeForKids": False}}
    try:
        d = buffer(CREATE, {"input": inp})["createPost"]
    except Exception as e:  # noqa: BLE001
        return {"state": "error", "error": str(e)[:300]}
    if d.get("__typename") == "PostActionSuccess":
        return {"state": "uploading", "buffer_id": d["post"]["id"], "error": ""}
    return {"state": "error", "error": f"{d.get('__typename')}: {d.get('message', '')}"[:300]}


def recent_buffer_posts() -> list[dict]:
    inp = {"organizationId": ORG, "filter": {"channelIds": list(CH.values()),
                                             "createdAt": {"start": iso(NOW - timedelta(days=45))}}}
    for q in (POSTS_FULL, POSTS_LITE):
        try:
            d = buffer(q, {"input": inp, "first": 100})
            return [e["node"] for e in (d.get("posts") or {}).get("edges", [])]
        except Exception as e:  # noqa: BLE001
            log(f"post list query failed ({'full' if q is POSTS_FULL else 'lite'}): {e}")
    raise RuntimeError("couldn't read posts from Buffer")


def metrics(node: dict) -> dict:
    out = {}
    for m in node.get("metrics") or []:
        out[m.get("type")] = out.get(m.get("type"), 0) + (m.get("value") or 0)
    return {"views": int(out.get("views", 0) or out.get("impressions", 0) or 0),
            "likes": int(out.get("likes", 0) or out.get("favorites", 0) or out.get("reactions", 0) or 0),
            "comments": int(out.get("comments", 0) or out.get("replies", 0) or 0),
            "shares": int(out.get("shares", 0) or 0),
            "reposts": int(out.get("reposts", 0) or out.get("retweets", 0) or 0),
            "saves": int(out.get("saves", 0) or 0)}


FRESH_HOURS = 5    # "newest" = clipped in the last 5 hours
MAX_AGE_HOURS = 9  # never post a clip clipped more than 9 hours ago


def too_old(r: dict) -> bool:
    if r.get("source") == "upload":   # clips from your own videos aren't time-sensitive
        return False
    try:
        return (NOW - parse(r.get("created") or r.get("found") or iso(NOW))) > timedelta(hours=MAX_AGE_HOURS)
    except Exception:  # noqa: BLE001
        return False


def tier(r: dict) -> int:
    """0 = new clip with a real hook, 1 = older clip with a real hook, 2 = new clip still waiting for a hook, 3 = rest."""
    found = r.get("created") or r.get("found") or ""
    try:
        fresh = (NOW - parse(found)) < timedelta(hours=FRESH_HOURS)
    except Exception:  # noqa: BLE001
        fresh = False
    real = not r.get("hook_auto") or r.get("hook_kind") == "title"
    return 0 if (fresh and real) else 1 if real else 2 if fresh else 3


# ------------------------------------------------------------------ your own uploads, spotted on the channels
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
      "Accept-Language": "en-US,en;q=0.9"}
YT_SAMPLE = "https://www.youtube.com/shorts/YuteHoOwHUA"   # one of our Shorts, to find the channel id
TIKTOK_USER = "gtaclip96"


def _get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def channel_videos(ctl: dict) -> list[dict]:
    """Recent videos on YouTube (public feed) and TikTok (public profile, best effort): [{service, link, text}]."""
    out = []
    try:
        cid = ctl.get("yt_channel")
        if not cid:
            m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', _get(YT_SAMPLE))
            cid = m.group(1) if m else ""
            ctl["yt_channel"] = cid
        if cid:
            xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
            for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
                vid = re.search(r"<yt:videoId>([^<]+)", e)
                title = re.search(r"<title>([^<]*)", e)
                desc = re.search(r"<media:description>([^<]*)", e)
                if vid:
                    out.append({"service": "youtube", "link": f"https://youtube.com/shorts/{vid.group(1)}",
                                "text": __import__("html").unescape((title.group(1) if title else "") + " " + (desc.group(1) if desc else ""))})
        ctl["yt_check"] = "ok"
    except Exception as e:  # noqa: BLE001
        ctl["yt_check"] = f"failed: {str(e)[:80]}"
    n, errs = 0, []
    for url in (f"https://www.tiktok.com/@{TIKTOK_USER}", f"https://www.tiktok.com/embed/@{TIKTOK_USER}"):
        try:
            page = _get(url)
        except Exception as e:  # noqa: BLE001
            errs.append(str(e)[:60])
            continue
        found = re.findall(r'"id":"(\d{15,22})","desc":"((?:[^"\\]|\\.)*)"', page)
        found += [(a, b) for b, a in re.findall(r'"desc":"((?:[^"\\]|\\.)*)"[^{}]{0,400}?"id":"(\d{15,22})"', page)]
        for vid, desc in found:
            try:
                text = json.loads(f'"{desc}"')
            except Exception:  # noqa: BLE001
                text = desc
            out.append({"service": "tiktok", "link": f"https://www.tiktok.com/@{TIKTOK_USER}/video/{vid}", "text": text})
            n += 1
        if n:
            break
    ctl["tt_check"] = "ok" if n else ("no videos visible (TikTok hides them from automated checks)" if not errs else f"failed: {errs[0]}")
    return out


def detect_manual_posts(ctl: dict, by_id: dict, recs: dict) -> None:
    """A queued clip whose hook shows up on our TikTok/YouTube = you posted it yourself. Mark it posted."""
    if DRY_RUN or (ctl.get("manual_check") and NOW - parse(ctl["manual_check"]) < timedelta(minutes=14)):
        return
    ctl["manual_check"] = iso(NOW)
    vids = channel_videos(ctl)
    known = {r.get(s) for r in recs.values() for s in ("tiktok", "youtube") if r.get(s)}
    for v in vids:
        if v["link"] in known:
            continue
        t = norm(v["text"])
        for cid, c in by_id.items():
            h = norm(c.get("hook"))
            if len(h) >= 12 and h[:40] in t:
                r = recs.get(cid) or {"id": cid, "hook": c.get("hook", ""), "streamer": c.get("streamer", ""),
                                      "campaign": c.get("campaign", ""), "why": "posted by Nicholas (spotted on the channel)",
                                      "manual": True, "posted_at": iso(NOW), "tiktok": "", "youtube": ""}
                if not r.get(v["service"]):
                    r[v["service"]] = v["link"]
                    recs[cid] = r
                    wj(P / "posted" / f"{cid}.json", r)
                    log(f"spotted your {v['service']} post of {cid}: {v['link']}")
                break


# ------------------------------------------------------------------ schedule
# TikTok blocks accounts that publish too much through the API, so: every 90 minutes (11 a day)
SLOTS = [(7, 7), (8, 37), (10, 7), (11, 37), (13, 7), (14, 37), (16, 7), (17, 37), (19, 7), (20, 37), (22, 7)]
DAILY_CAP = 12   # most posts (slots + Post now) in any 24 hours


def slots_today(on: datetime | None = None) -> list[datetime]:
    d = (on or NOW).astimezone(ET).date()
    return [datetime(d.year, d.month, d.day, h, m, tzinfo=ET) for h, m in SLOTS]


def due_slot() -> datetime | None:
    past = [s for s in slots_today() if s <= NOW]
    if not past:
        return None
    s = past[-1]
    return s if (NOW - s) <= timedelta(minutes=SLOT_GRACE_MIN) else None


def next_slot(after: datetime | None = None) -> datetime | None:
    after = after or NOW
    nxt = [s for s in slots_today(after) if s > after]
    if nxt:
        return nxt[0]
    d = after.astimezone(ET).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, SLOTS[0][0], SLOTS[0][1], tzinfo=ET)


# ------------------------------------------------------------------ main
def main() -> None:
    queue = rj(QUEUE, {"ready": []})
    ready = [r for r in queue.get("ready", []) if r.get("final_abs")]
    by_id = {r["id"]: r for r in queue.get("ready", [])}
    blocked_extra: set[str] = set()
    QUEUE_BY_ID.update(by_id)
    ctl = rj(P / "control.json", None)
    if ctl is None:  # first run: carry over the old Start/Stop setting
        ctl = {"paused": True, "postnow": [], "last_slot": ""}
    ctl.setdefault("postnow", [])
    ctl.setdefault("last_slot", "")
    recs: dict[str, dict] = {f.stem: rj(f, {}) for f in sorted((P / "posted").glob("*.json"))}
    posted_ids = set(recs)

    # a new hook was written but the video isn't re-made yet: wait for it (never post an old caption)
    for f in (P / "hooks").glob("*.json"):
        c = by_id.get(f.stem)
        if c and not c.get("hook_by_user") and rj(f, {}).get("hook", "").strip() != (c.get("hook") or "").strip():
            blocked_extra.add(f.stem)
    for f in (P / "skips").glob("*.json"):
        if f.stem in by_id:
            blocked_extra.add(f.stem)
    if GH_TOKEN and not DRY_RUN:
        handle_requests(ctl, set(by_id), posted_ids)
        recs.update({f.stem: rj(f, {}) for f in (P / "posted").glob("*.json") if f.stem not in recs})
        blocked = blocked_ids()
    else:
        blocked = set()
    blocked |= blocked_extra
    try:
        detect_manual_posts(ctl, by_id, recs)
        posted_ids = set(recs)
    except Exception as e:  # noqa: BLE001
        log(f"channel check failed: {e}")
    ctl["postnow"] = [p for p in ctl["postnow"] if p["id"] not in posted_ids and p["id"] in by_id]

    buffer_ok, buffer_err = False, ""
    if not BUFFER_KEY:
        buffer_err = "Buffer API key missing — add the BUFFER_API_KEY secret in GitHub (see dashboard Settings)."
        log(buffer_err)

    # 0) never post the same clip twice: look at everything Buffer sent / is sending (it retries failed posts itself)
    early_nodes: list[dict] = []
    if BUFFER_KEY and not DRY_RUN:
        try:
            early_nodes = recent_buffer_posts()
        except Exception as e:  # noqa: BLE001
            log(f"duplicate check skipped: {e}")
    buffer_texts = [norm(n.get("text", "")) for n in early_nodes if n.get("status") in ("sent", "sending", "scheduled", "draft", "error")]

    def already_on_buffer(r: dict) -> bool:
        h = norm(r.get("hook"))[:30]
        return len(h) >= 12 and any(h in t for t in buffer_texts)

    # 1) what should go out now?
    to_post: list[tuple[dict, str]] = []
    if ctl["postnow"]:
        cid = ctl["postnow"][0]["id"]
        if by_id.get(cid) and already_on_buffer(by_id[cid]):
            log(f"post now skipped — {cid} is already on Buffer (no duplicates)")
            ctl["postnow"] = ctl["postnow"][1:]
        elif by_id.get(cid, {}).get("final_abs"):
            to_post.append((by_id[cid], "post now"))
    slot = due_slot()
    slot_key = iso(slot) if slot else ""
    last24 = sum(1 for r in recs.values() if r.get("posted_at") and NOW - parse(r["posted_at"]) < timedelta(hours=24)
                 and (r.get("tiktok") or (r.get("status") or {}).get("tiktok", {}).get("state") in ("live", "uploading")))
    if not to_post and slot and not ctl.get("paused") and ctl.get("last_slot") != slot_key and last24 >= DAILY_CAP:
        log(f"daily cap reached ({last24} posts in 24 h) — skipping this slot")
        ctl["last_slot"] = slot_key
    if not to_post and slot and not ctl.get("paused") and ctl.get("last_slot") != slot_key:
        pending_now = {p["id"] for p in ctl["postnow"]}
        ok = [r for r in ready if r["id"] not in posted_ids and r["id"] not in blocked and r["id"] not in pending_now
              and not too_old(r) and not already_on_buffer(r)]
        # clips with a proper hook first; automatic placeholder hooks only if nothing else is left
        last_streamer = next((r.get("streamer") for r in sorted(recs.values(), key=lambda r: r.get("posted_at", ""), reverse=True)), "")
        # newest clips with a real hook first, then the AI viral score (clips rated "skip" go last)
        ok = sorted(ok, key=lambda r: (r.get("viral_verdict") == "skip", tier(r), -(r.get("viral") or 50)))
        pick = (next((r for r in ok[:4] if r.get("streamer") != last_streamer), None)
                or (ok[0] if ok else None)) if ok else None
        if pick:
            to_post.append((pick, f"{slot.astimezone(ET):%-I:%M %p} slot"))
        else:
            log("slot due but no finished clip is ready")
            ctl["last_slot"] = slot_key

    # 2) post it
    if DRY_RUN:
        buffer_ok = True
    elif BUFFER_KEY:
        try:
            buffer("query { account { id } }")
            buffer_ok = True
        except Exception as e:  # noqa: BLE001
            buffer_err = f"Buffer didn't accept the API key: {e}"[:300]
            log(buffer_err)
    if buffer_ok:
        for c, why in to_post:
            cid = c["id"]
            cool = ctl.get("cooldown", {})
            live_svcs = [s for s in ("tiktok", "youtube") if not cool.get(s) or parse(cool[s]) <= NOW]
            if not live_svcs:
                log(f"{why}: TikTok and YouTube are both waiting out an upload limit — holding {cid}")
                break
            log(f"posting {cid} ({why}) to {', '.join(live_svcs)}: {c.get('hook')}")
            res = {s: create(s, c) for s in live_svcs}
            res.update({s: {"state": "error", "error": "skipped — waiting out the platform limit", "tries": 3}
                        for s in ("tiktok", "youtube") if s not in live_svcs})
            ok = any(r["state"] != "error" for r in res.values())
            recs[cid] = {
                "id": cid, "hook": c.get("hook", ""), "streamer": c.get("streamer", ""), "login": c.get("login", ""),
                "campaign": c.get("campaign", ""), "tag": c.get("tag", ""), "caption": c.get("caption", ""), "why": why, "posted_at": iso(NOW), "video": c["final_abs"],
                "poster": f"https://inballin15.github.io/Gtaclip-cloud/{c.get('poster', '')}",
                "tiktok": "", "youtube": "", "status": res,
            }
            for s, r in res.items():
                log(f"  {s}: {r['state']} {r.get('error', '')}")
            if ok:
                wj(P / "posted" / f"{cid}.json", recs[cid])
                if why == "post now":
                    ctl["postnow"] = [p for p in ctl["postnow"] if p["id"] != cid]
                else:
                    ctl["last_slot"] = slot_key
            else:
                recs.pop(cid)
                ctl.setdefault("errors", []).insert(0, {"at": iso(NOW), "id": cid,
                                                         "error": "; ".join(r["error"] for r in res.values())})
                ctl["errors"] = ctl["errors"][:10]
                if why != "post now":
                    ctl["last_slot"] = slot_key  # don't hammer the same slot; next slot tries again

    # 3) follow every post until it's live, and collect stats
    stats_posts = []
    if buffer_ok:
        try:
            nodes = [] if DRY_RUN else recent_buffer_posts()
        except Exception as e:  # noqa: BLE001
            nodes = []
            buffer_err = str(e)[:300]
        by_buf = {n["id"]: n for n in nodes}
        gone: list[str] = []
        svc = {v: k for k, v in CH.items()}
        for cid, r in recs.items():
            changed = False
            for s, st in (r.get("status") or {}).items():
                n = by_buf.get(st.get("buffer_id", ""))
                if not n:
                    continue
                new = {"sent": "live", "error": "error", "sending": "uploading", "scheduled": "uploading"}.get(n["status"], n["status"])
                link = n.get("externalLink") or ""
                err = (n.get("error") or {}).get("message", "") if new == "error" else ""
                if new != st.get("state") or (link and link != r.get(s)) or err != st.get("error", ""):
                    st.update(state=new, error=err)
                    if link:
                        r[s] = link
                    changed = True
            for s, st in (r.get("status") or {}).items():
                fresh = (NOW - parse(st.get("last_try") or r.get("posted_at") or iso(NOW))) < timedelta(hours=2)
                if st.get("state") == "error" and COOLDOWN_RE.search(st.get("error", "")) and fresh and not st.get("cooldown_set"):
                    st["cooldown_set"] = True
                    changed = True
                    if "24 hours" in st["error"]:
                        until = iso(NOW + timedelta(hours=24))
                    elif "daily posting limit" in st["error"]:  # Buffer's rolling 24 h limit: try again in 3 h
                        until = iso(NOW + timedelta(hours=3))
                    elif "daily" in st["error"]:  # YouTube's daily limit resets at midnight Pacific
                        nxt = (NOW + timedelta(hours=16)).replace(hour=8, minute=5, second=0, microsecond=0)
                        until = iso(nxt if nxt > NOW else nxt + timedelta(days=1))
                    else:
                        until = iso(NOW + timedelta(hours=2))
                    if ctl.setdefault("cooldown", {}).get(s, "") < iso(NOW):
                        ctl["cooldown"][s] = until
                        log(f"{s} limit hit — pausing {s} uploads until {until}")
            if changed:
                wj(P / "posted" / f"{cid}.json", r)
            sts = list((r.get("status") or {}).values())
            if sts and all(x.get("state") == "error" for x in sts) and not (r.get("tiktok") or r.get("youtube")) \
                    and not any(by_buf.get(x.get("buffer_id", ""), {}).get("status") in ("sending", "scheduled", "sent") for x in sts):
                (P / "posted" / f"{cid}.json").unlink(missing_ok=True)
                ctl.setdefault("errors", []).insert(0, {"at": iso(NOW), "id": cid, "error": "; ".join(x.get("error", "") for x in sts)})
                ctl["errors"] = ctl["errors"][:10]
                ctl["returned"] = sorted(set(ctl.get("returned", [])) | {cid})
                log(f"{cid} failed everywhere — back in the queue")
                gone.append(cid)
        for g in gone:
            recs.pop(g, None)
        # retry a platform that failed (e.g. YouTube upload limit), up to 3 times, 20 min apart, within 6 h
        for cid, r in recs.items():
            if not r.get("video") or (NOW - parse(r.get("posted_at", iso(NOW)))) > timedelta(hours=6):
                continue
            for s, st in (r.get("status") or {}).items():
                last = st.get("last_try") or r.get("posted_at")
                if st.get("state") != "error" or st.get("tries", 1) >= 3 or (NOW - parse(last)) < timedelta(minutes=20):
                    continue
                if ctl.get("cooldown", {}).get(s, "") > iso(NOW):
                    continue
                info = {**r, "final_abs": r["video"], "tag": r.get("tag") or "#" + re.sub(r"\W", "", r.get("streamer", ""))}
                res = create(s, info)
                log(f"retry {s} for {cid}: {res['state']} {res.get('error', '')}")
                st.update(res, tries=st.get("tries", 1) + 1, last_try=iso(NOW))
                wj(P / "posted" / f"{cid}.json", r)
        hooks = {(r.get("hook") or "")[:40].lower(): (cid, r) for cid, r in recs.items() if r.get("hook")}
        for n in nodes:
            if n.get("status") != "sent":
                continue
            first = (n.get("text") or "").split("\n")[0].strip()
            match = hooks.get(first[:40].lower())
            stats_posts.append({
                "service": svc.get(n.get("channelId"), "?"), "link": n.get("externalLink") or "",
                "sent": n.get("sentAt") or n.get("createdAt"), "hook": first,
                "clip": match[0] if match else "", "streamer": (match[1].get("streamer") if match else "") or guess_streamer(n.get("text") or ""),
                **metrics(n)})

    # 4) live.json for the dashboard
    nxt = next_slot()
    cd = [parse(v) for v in ctl.get("cooldown", {}).values() if v and parse(v) > NOW]
    if len(cd) >= 2:  # both platforms waiting
        nxt = next_slot(min(cd))
    up_next = None
    if not ctl.get("paused"):
        pending_now = {p["id"] for p in ctl["postnow"]}
        ok = sorted([r for r in ready if r["id"] not in recs and r["id"] not in blocked and r["id"] not in pending_now],
                    key=lambda r: (r.get("viral_verdict") == "skip", tier(r), -(r.get("viral") or 50)))
        up = ok[0] if ok else None
        up_next = up["id"] if up else None
    old = rj(P / "live.json", {})
    live = {
        "updated": iso(NOW), "paused": bool(ctl.get("paused")), "switched_at": ctl.get("switched_at", ""),
        "postnow": ctl["postnow"], "next_slot": iso(nxt) if nxt else "", "up_next": up_next,
        "buffer_ok": buffer_ok, "buffer_error": buffer_err, "cooldown": ctl.get("cooldown", {}),
        "ai_off": bool(ctl.get("ai_off")),
        "channel_check": {"at": ctl.get("manual_check", ""), "youtube": ctl.get("yt_check", ""), "tiktok": ctl.get("tt_check", "")}, "errors": ctl.get("errors", []),
        "blocked": sorted(blocked),
        "posts": dict(sorted(recs.items(), key=lambda kv: kv[1].get("posted_at", ""), reverse=True)[:120]),
        "stats": stats_posts if buffer_ok else old.get("stats", []),
        "stats_updated": iso(NOW) if buffer_ok else old.get("stats_updated", ""),
        "log": LOG[-30:],
    }
    wj(P / "live.json", live)
    wj(P / "control.json", ctl)


def guess_streamer(text: str) -> str:
    m = re.search(r"Credit:\s*([^\n(]+)", text) or re.search(r"@([A-Za-z0-9_]+)", text)
    return m.group(1).strip() if m else ""


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(1)

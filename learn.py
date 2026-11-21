"""Engagement learner: reads how every video on our TikTok + YouTube is doing, works out which hook styles,
words, CAPS use, lengths, streamers and posting times get more views, and turns that into
  1) guidance the AI follows when it writes new hooks, and
  2) weights for the hook styles being tested (most posts use what works, some keep trying other styles).
Runs inside the poster every ~2 hours. Pure counting, no AI calls."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Hook styles being tested. The pipeline picks one per clip (weighted by how well each has done) and tells the AI.
VARIANTS = {
    "formula": "The proven formula: [name] [CAPS emotion] after [finding out/realizing] [specific detail].",
    "news": "Breaking-news angle: '[name] just BECAME the FIRST…' / '[name] is OFFICIALLY…' / 'No way [name]…'.",
    "quote": "Lead with the streamer's own most shocking words in quotes-free form, then the context: '[name] said … after …'.",
    "question": "A curiosity question the clip answers: 'Why did [name] …?' / 'How did [name] …?' Keep 2-4 CAPS words.",
    "lowcaps": "Same story but calm spelling: normal sentence case, at most ONE CAPS word, short (25-55 characters).",
}
PRIOR = {"formula": 0.5, "news": 0.15, "quote": 0.1, "question": 0.1, "lowcaps": 0.15}   # start mostly on what's proven
MIN_WEIGHT = 0.08          # every style keeps getting a few tries so the learner can notice when tastes change

EMOTIONS = ["LOSES IT", "SHOCKED", "DEVASTATED", "DISAPPOINTED", "FURIOUS", "CRYING", "SCARED", "CONFUSED", "SPEECHLESS",
            "HYPED", "TERRIFIED", "STUNNED", "MAD", "LAUGHING"]


def features(hook: str, posted: str, seconds, streamer: str, variant: str, service: str) -> dict:
    h = (hook or "").strip()
    words = re.findall(r"[A-Za-z0-9$']+", h)
    caps = [w for w in words if len(w) >= 2 and w.isupper() and not w.isdigit()]
    f = {
        "platform": service,
        "streamer": streamer or "?",
        "hook style": variant or "unknown",
        "hook length": "short (<60)" if len(h) < 60 else "medium (60-90)" if len(h) < 90 else "long (90+)",
        "CAPS words": "0-1" if len(caps) <= 1 else "2-3" if len(caps) <= 3 else "4-6" if len(caps) <= 6 else "7+",
        "has a number": "yes" if re.search(r"\d", h) else "no",
        "has 'FIRST'": "yes" if re.search(r"\bfirst\b", h, re.I) else "no",
        "question": "yes" if "?" in h else "no",
    }
    emo = next((e for e in EMOTIONS if e.lower() in h.lower()), "")
    f["emotion word"] = emo or "none"
    try:
        hr = datetime.fromisoformat(posted.replace("Z", "+00:00")).astimezone(ET).hour
        f["posted"] = "morning (7-11)" if 7 <= hr < 11 else "midday (11-15)" if hr < 15 else "evening (15-19)" if hr < 19 else "night (19+)" if hr >= 19 else "late night"
    except Exception:  # noqa: BLE001
        pass
    if isinstance(seconds, (int, float)) and seconds:
        f["clip length"] = "≤15 s" if seconds <= 15 else "16-22 s" if seconds <= 22 else "23 s+"
    return f


def learn(videos: list[dict], recs: dict[str, dict], now: datetime | None = None) -> dict:
    """videos: channel entries {link, service, text, views, published, clip}. recs: our posted records by clip id."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for v in videos:
        if v.get("views") is None:
            continue
        pub = v.get("published") or v.get("first_seen") or ""
        try:
            age_h = (now - datetime.fromisoformat(pub.replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            continue
        if age_h < 18:          # too new to judge
            continue
        rec = recs.get(v.get("clip") or "", {})
        hook = rec.get("hook") or re.sub(r"[#@]\S+|🎥", "", v.get("text") or "").strip()
        f = features(hook, rec.get("posted_at") or pub, rec.get("seconds"), rec.get("streamer") or v.get("campaign") or v.get("streamer", ""),
                     rec.get("hook_variant", ""), v.get("service", ""))
        # score: views on a log scale, compared within the same platform (YouTube and TikTok differ a lot)
        rows.append((f, math.log1p(v["views"]), v["service"], v["views"], hook))
    out = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "videos": len(rows), "findings": [], "guidance": [],
           "weights": dict(PRIOR), "top": []}
    if len(rows) < 6:
        out["note"] = f"Learning starts after 6 videos are at least 18 h old (now {len(rows)})."
        return out
    # normalise per platform
    by_p: dict[str, list[float]] = {}
    for f, sc, sv, *_ in rows:
        by_p.setdefault(sv, []).append(sc)
    mean_p = {k: sum(v) / len(v) for k, v in by_p.items()}
    norm = [(f, sc - mean_p[sv], views, hook) for f, sc, sv, views, hook in rows]
    # lift for each feature value (needs at least 3 videos)
    stats: dict[tuple[str, str], list[float]] = {}
    for f, d, *_ in norm:
        for k, val in f.items():
            if k == "platform":
                continue
            stats.setdefault((k, val), []).append(d)
    finds = []
    for (k, val), ds in stats.items():
        if len(ds) < 3:
            continue
        m = sum(ds) / len(ds)
        mult = math.exp(m)                       # ~ how many times more views than average
        finds.append({"feature": k, "value": val, "x": round(mult, 2), "n": len(ds)})
    finds.sort(key=lambda r: -abs(math.log(r["x"])))
    out["findings"] = finds[:24]
    # guidance for the AI (only clear differences)
    g = []
    for r in finds:
        if r["n"] < 3 or 0.8 < r["x"] < 1.25:
            continue
        better = r["x"] >= 1.25
        k, val = r["feature"], r["value"]
        if k in ("hook length", "CAPS words", "has a number", "has 'FIRST'", "question", "emotion word", "clip length"):
            g.append(f"{'DO' if better else 'AVOID'}: {k} = {val} ({r['x']}x views, {r['n']} videos)")
    out["guidance"] = g[:8]
    # style weights: average performance, then keep a floor so every style is still tested
    vs = {k: [] for k in VARIANTS}
    for f, d, *_ in norm:
        if f.get("hook style") in vs:
            vs[f["hook style"]].append(d)
    raw = {k: PRIOR[k] * (math.exp(sum(v) / len(v)) if len(v) >= 2 else 1.0) for k, v in vs.items()}
    tot = sum(raw.values())
    w = {k: max(MIN_WEIGHT, raw[k] / tot) for k in raw}
    tot = sum(w.values())
    out["weights"] = {k: round(v / tot, 3) for k, v in w.items()}
    out["style_counts"] = {k: len(v) for k, v in vs.items()}
    out["top"] = [{"hook": h[:120], "views": vw} for _, _, vw, h in sorted(norm, key=lambda r: -r[2])[:5]]
    return out


def guidance_text(ins: dict) -> str:
    """Extra lines for the AI hook prompt, learned from our own channels."""
    g = (ins or {}).get("guidance") or []
    if not g:
        return ""
    return "What works on THIS channel (learned from our own views - follow it):\n" + "\n".join("- " + x for x in g) + "\n"


def pick_variant(ins: dict, seed: str) -> str:
    """Choose a hook style for one clip: mostly what performs, sometimes the others (deterministic per clip)."""
    w = (ins or {}).get("weights") or PRIOR
    import hashlib
    r = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    acc = 0.0
    for k in VARIANTS:
        acc += w.get(k, 0)
        if r < acc:
            return k
    return "formula"

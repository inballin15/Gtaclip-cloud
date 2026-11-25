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
    "formula": "The proven story sentence: [name] [emotional past-tense verb] [after / finding out] [specific detail], 8-15 words, at most 1 CAPS word.",
    "news": "Breaking-news angle: '[name] just BECAME the FIRST…' / '[name] is OFFICIALLY…' / 'No way [name]…'.",
    "quote": "Lead with the streamer's own most shocking words in quotes-free form, then the context: '[name] said … after …'.",
    "question": "A curiosity question the clip answers: 'Why did [name] …?' / 'How did [name] …?' At most 1 CAPS word.",
    "lowcaps": ("NATURAL, like a friend telling you what happened: normal spelling, NO all-caps words (GTA is fine), "
                "casual and specific, 8-15 words. Real top performers on this channel: 'Marlon can't handle a night in "
                "GTA 6' / 'Lacy stalking in gta 6' / 'Adapt tries texting and driving in GTA 6'."),
}
# Starting point = what the biggest GTA clip pages do (110 of their videos studied, 1-7M views each):
# name + CAPS emotion + one concrete detail, 'FIRST'/news angle second. Our own views move it from here.
# Nov 22 clipper study (974 videos): story sentences with 1-2 CAPS words win; all-caps formulas did worse.
PRIOR = {"formula": 0.5, "news": 0.15, "lowcaps": 0.2, "quote": 0.05, "question": 0.1}
LEARN_V = 4      # bump when the rules change -> the poster re-learns right away
EVIDENCE = 8     # our own videos needed before they count as much as the proven starting point   # start mostly on what's proven
MIN_WEIGHT = 0.08          # every style keeps getting a few tries so the learner can notice when tastes change

# Clip-style tests ("dynamic model"): each automatic clip gets one option per dimension; views decide the mix.
STYLE_DIMS = {
    "box": {"white": "white caption box", "black": "black caption box"},   # outline-only text was hard to read on busy streams
    # webcam size: never bigger than today's (a bigger strip would stretch the webcam and cost sharpness)
    "cam": {"small": "smaller webcam (560 px)", "medium": "standard webcam (672 px)"},
    # (clip length + where the big moment sits are no longer tested: Nov 24 TikTok watch data - short clips with the
    #  moment early hold viewers best -> every auto clip is ~14 s with the moment ~4 s in)
    "hooklen": {"tiny": "short hook (max ~60 chars)", "story": "story-sentence hook (8-15 words)"},
    # (subtitles: no longer tested - off unless switched on for a clip in the Studio; Nov 24, they were inaccurate)
    "punch": {"off": "steady webcam", "on": "webcam punches in on the big reaction"},
    # Nov 25: viewers leave early -> does a bar showing how short the clip is keep them to the end?
    "pbar": {"off": "no progress bar", "on": "progress bar along the top"},
}
STYLE_PRIOR = {"box": {"white": 0.75, "black": 0.25}, "cam": {"small": 0.3, "medium": 0.7},   # top pages:
               "hooklen": {"tiny": 0.25, "story": 0.75},
               "punch": {"off": 0.5, "on": 0.5},
               "pbar": {"off": 0.5, "on": 0.5}}   # new tests: no data yet, 50/50   # Nov 22 study: winners peak ~middle of ~25 s clips, ~13-word hooks
CAM_PX = {"small": 560, "medium": 672}

EMOTIONS = ["LOSES IT", "SHOCKED", "DEVASTATED", "DISAPPOINTED", "FURIOUS", "CRYING", "SCARED", "CONFUSED", "SPEECHLESS",
            "HYPED", "TERRIFIED", "STUNNED", "MAD", "LAUGHING"]


def features(hook: str, posted: str, seconds, streamer: str, variant: str, service: str, style: dict | None = None) -> dict:
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
    for dim, arm in (style or {}).items():
        if dim in STYLE_DIMS and arm in STYLE_DIMS[dim]:
            f["style:" + dim] = arm
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
                     rec.get("hook_variant", ""), v.get("service", ""), rec.get("style"))
        # score: views on a log scale, compared within the same platform (YouTube and TikTok differ a lot)
        rows.append((f, math.log1p(v["views"]), v["service"], v["views"], hook))
    out = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "videos": len(rows), "v": LEARN_V, "findings": [], "guidance": [],
           "weights": dict(PRIOR), "style_weights": {d: dict(p) for d, p in STYLE_PRIOR.items()}, "top": []}
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
        if r["n"] < 6 or 0.8 < r["x"] < 1.25 or r["feature"] == "CAPS words":
            continue
        better = r["x"] >= 1.25
        k, val = r["feature"], r["value"]
        if k in ("hook length", "CAPS words", "has a number", "has 'FIRST'", "question", "emotion word"):
            g.append(f"{'DO' if better else 'AVOID'}: {k} = {val} ({r['x']}x views, {r['n']} videos)")
    out["guidance"] = g[:8]
    # style weights: average performance, then keep a floor so every style is still tested
    vs = {k: [] for k in VARIANTS}
    for f, d, *_ in norm:
        if f.get("hook style") in vs:
            vs[f["hook style"]].append(d)
    shrink = lambda v: (sum(v) / len(v)) * len(v) / (len(v) + EVIDENCE) if v else 0.0   # few videos -> small nudge
    raw = {k: PRIOR[k] * math.exp(shrink(v)) for k, v in vs.items()}
    tot = sum(raw.values())
    w = {k: max(MIN_WEIGHT, raw[k] / tot) for k in raw}
    tot = sum(w.values())
    out["weights"] = {k: round(v / tot, 3) for k, v in w.items()}
    out["style_counts"] = {k: len(v) for k, v in vs.items()}
    # clip-style weights per dimension, same idea: what's working gets more clips, the rest keep a floor
    sw, sc = {}, {}
    for dim, arms in STYLE_DIMS.items():
        got = {a: [] for a in arms}
        for f, d, *_ in norm:
            a = f.get("style:" + dim)
            if a in got:
                got[a].append(d)
        raw = {a: STYLE_PRIOR[dim][a] * math.exp(shrink(v)) for a, v in got.items()}
        tot = sum(raw.values())
        w = {a: max(0.12, r / tot) for a, r in raw.items()}
        tot = sum(w.values())
        sw[dim] = {a: round(v / tot, 3) for a, v in w.items()}
        sc[dim] = {a: len(v) for a, v in got.items()}
    out["style_weights"], out["style_tested"] = sw, sc
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


def pick_style(ins: dict, seed: str) -> dict:
    """One option per style dimension for this clip, weighted by what has been getting views."""
    import hashlib
    sw = (ins or {}).get("style_weights") or STYLE_PRIOR
    out = {}
    for dim, arms in STYLE_DIMS.items():
        w = sw.get(dim) or STYLE_PRIOR[dim]
        r = int(hashlib.sha1(f"{seed}:{dim}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        acc, pick = 0.0, next(iter(arms))
        for a in arms:
            acc += w.get(a, 0)
            if r < acc:
                pick = a
                break
        else:
            pick = max(w, key=w.get)
        out[dim] = pick
    return out

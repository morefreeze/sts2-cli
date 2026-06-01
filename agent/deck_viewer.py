#!/usr/bin/env python3
"""deck_viewer.py — Static HTML visualiser for sampled deck builds.

Pulls 100 distinct runs from data/deck_history.jsonl, picks one milestone
per run, joins it with the run's outcome (won / max_floor), computes both
the v1 deck_quality_score and a v2 per-deck mean (each card scored as if
it were the candidate among the rest of the deck), and renders a single
self-contained HTML file with one panel per deck.

Usage:
    .venv/bin/python agent/deck_viewer.py --out decks.html
"""
import argparse
import json
import os
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.card_scoring as _cs
from agent.card_scoring import (
    deck_quality_score,
    _load_predictor,
    _deck_features_v2,
    score_deck_dimensions,
    compute_deck_archetype,
    score_card,
)
# Trigger predictor load so the module-level _PREDICTOR_VERSION is populated.
_load_predictor()


def load_card_db() -> dict[str, dict]:
    """Map game-id (e.g. STRIKE_IRONCLAD) → wiki record (name, type, rarity)."""
    with open("data/ironclad_cards.json") as f:
        cards = json.load(f)["cards"]
    by_slug = {c["id"]: c for c in cards}

    # Build aliases for the runtime ID forms used in deck_history.
    db: dict[str, dict] = {}
    for c in cards:
        slug = c["id"]
        # canonical wiki slug (lowercase, dash-separated)
        db[slug] = c
        # uppercase snake_case used at runtime (strike-ironclad → STRIKE_IRONCLAD)
        db[slug.upper().replace("-", "_")] = c
        # without the _IRONCLAD class suffix
        bare = slug.replace("-ironclad", "")
        db[bare] = c
        db[bare.upper().replace("-", "_")] = c
    # Cover Strike/Defend without character suffix
    for canon in ("strike", "defend"):
        for variant in (f"{canon}-ironclad", canon):
            rec = by_slug.get(variant)
            if rec:
                db[canon.upper()] = rec
                db[f"{canon.upper()}_IRONCLAD"] = rec
    return db


# --------------------------------------------------------------------
# Training-round attribution.
# deck_history.jsonl rows carry only `run_id` (random) + `ts` (unix), not
# a checkpoint label. Bin runs by timestamp into the training round that
# was producing data at that moment, using ckpt-file mtimes as boundaries.
# (Round = a contiguous training process whose CHECKPOINT_DIR setting
# matches.)
TRAINING_ROUNDS = [
    # (label,    start_unix,         end_unix or None=ongoing)
    ("baseline", 0,                   1779028800),  # → 05-29 13:00 (boss start)
    ("boss",     1779028800,          1779043200),  # 05-29 13:00 → 17:00 (boss2 start)
    ("boss2",    1779043200,          1779064800),  # 05-29 17:00 → 23:00
    ("boss3",    1779064800,          1779097200),  # 05-29 23:00 → 05-30 08:00
    ("hpw",      1779166800,          1779182400),  # 06-01 10:00 → 14:15 (fix start)
    ("fix",      1779182400,          None),        # 06-01 14:15 → ongoing
]


def round_from_ts(ts: float | None) -> str:
    """Map a unix timestamp to its training-round label."""
    if not ts:
        return "?"
    for label, s, e in TRAINING_ROUNDS:
        if ts >= s and (e is None or ts < e):
            return label
    return "?"


_DAMAGE_RE = re.compile(r"deal\s+(\d+)\s+damage", re.I)
_BLOCK_RE = re.compile(r"gain\s+(\d+)\s+block", re.I)
_ENERGY_RE = re.compile(r"gain\s+(\d+)\s+energy", re.I)
_DRAW_N_RE = re.compile(r"draw\s+(\d+)\s+cards?", re.I)
_DRAW_A_RE = re.compile(r"draw\s+a\s+card", re.I)


def parse_card_stats(text: str) -> dict:
    """Extract damage/block/energy/draw counts from wiki card text.

    The wiki JSON has `stats=None` for every card; the runtime fills stats
    from the live game state. For an offline viewer we recover the same
    fields by regex over `normal_text` so card_dimensions() works.
    """
    if not text:
        return {}
    s = {}
    if (m := _DAMAGE_RE.search(text)) is not None:
        s["damage"] = int(m.group(1))
    if (m := _BLOCK_RE.search(text)) is not None:
        s["block"] = int(m.group(1))
    if (m := _ENERGY_RE.search(text)) is not None:
        s["energy"] = int(m.group(1))
    if (m := _DRAW_N_RE.search(text)) is not None:
        s["draw"] = int(m.group(1))
    elif _DRAW_A_RE.search(text):
        s["draw"] = 1
    return s


def lookup_card(card_id: str, card_db: dict) -> dict:
    """Build a card dict the scoring pipeline expects."""
    upgraded = card_id.endswith("+")
    base_id = card_id.rstrip("+")
    rec = card_db.get(base_id)
    if not rec:
        # Strip _IRONCLAD suffix and retry
        for sfx in ("_IRONCLAD", "_SILENT", "_DEFECT", "_REGENT", "_NECROBINDER"):
            if base_id.endswith(sfx):
                rec = card_db.get(base_id[: -len(sfx)])
                if rec:
                    break
    if not rec:
        return {
            "card_id": card_id,
            "en_name": base_id.replace("_", " ").title(),
            "zh_name": base_id,
            "type": "?", "rarity": "?", "cost": "?",
            "upgraded": upgraded,
            "stats": {},
            "description": "",
        }
    text = rec.get("upgraded_text" if upgraded else "normal_text") \
           or rec.get("normal_text", "")
    return {
        "card_id": card_id,
        "en_name": rec.get("en_name", base_id),
        "zh_name": rec.get("zh_name", base_id),
        "type": rec.get("type", "?"),
        "rarity": rec.get("rarity", "?"),
        "cost": rec.get("cost", "?"),
        "upgraded": upgraded,
        "stats": parse_card_stats(text),
        "description": text,
    }


def sample_milestones(jsonl_path: str, n: int = 100, seed: int = 42) -> list[dict]:
    """One milestone per run, n distinct runs, ranked by floor then random."""
    by_run: dict[str, list[dict]] = defaultdict(list)
    outcomes: dict[str, dict] = {}
    with open(jsonl_path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = o.get("event")
            rid = o.get("run_id")
            if not rid:
                continue
            if ev == "milestone":
                by_run[rid].append(o)
            elif ev == "outcome":
                outcomes[rid] = o
    rng = random.Random(seed)
    runs = [rid for rid in by_run if rid in outcomes]
    rng.shuffle(runs)
    chosen = []
    for rid in runs[: n * 3]:  # over-sample then prune duplicates
        ms_list = by_run[rid]
        # prefer mid-late milestones to show real builds (not floor 1)
        ms_list = sorted(ms_list, key=lambda m: m.get("floor_crossed", 0))
        if not ms_list:
            continue
        pick = ms_list[-1] if len(ms_list) <= 2 else ms_list[len(ms_list) // 2 + 1]
        pick["_outcome"] = outcomes[rid]
        chosen.append(pick)
        if len(chosen) >= n:
            break
    return chosen


def archetype_label(arch: dict) -> str:
    """Human label for archetype dict."""
    if not arch:
        return "—"
    items = [(k, v) for k, v in arch.items() if k != "n" and v >= 2]
    items.sort(key=lambda x: -x[1])
    if not items:
        return "balanced"
    return " / ".join(f"{k}×{v}" for k, v in items[:2])


def deck_v2_predicted_floor(deck_full: list[dict], floor_at: int) -> float | None:
    """Predict run.max_floor for this deck. v2's deck-level features depend on
    floor_crossed too (it carries a strong signal), so we pass the floor at
    which the snapshot was taken."""
    pipe = _load_predictor()
    if pipe is None or _cs._PREDICTOR_VERSION != "v2" or len(deck_full) < 2:
        return None
    # Use each deck card as the "candidate" feature slot; the deck-level part of
    # the features is invariant, so the mean prediction is the model's expected
    # max_floor when reasoning about this composition.
    try:
        import numpy as _np
        feats = []
        for i, c in enumerate(deck_full):
            rest = deck_full[:i] + deck_full[i + 1:]
            feats.append(_deck_features_v2(rest, c, floor=floor_at))
        preds = pipe.predict(_np.array(feats))
        return float(preds.mean())
    except Exception as e:
        return None


def build_html(decks: list[dict], card_db: dict) -> str:
    """Render the deck panels."""
    rows = []
    for d in decks:
        cards_raw = d.get("cards", [])
        deck_full = [lookup_card(c, card_db) for c in cards_raw]
        v1_q = d.get("deck_quality", 0.0)
        v2_mean = deck_v2_predicted_floor(deck_full,
                                          floor_at=d.get("floor_crossed", 10))
        # Recompute dims off the parsed-stats cards — the milestone's stored
        # `dims` field was computed at runtime against game-state cards
        # whose draw/energy stats were not always populated.
        dims = score_deck_dimensions(deck_full)
        arch = d.get("archetype", {})
        outcome = d.get("_outcome", {})
        won = outcome.get("won", False)
        max_floor = outcome.get("max_floor", "?")
        row = {
            "run_id": d.get("run_id", "?"),
            "floor_at": d.get("floor_crossed", "?"),
            "deck_size": d.get("deck_size", len(deck_full)),
            "v1": v1_q,
            "v2": v2_mean,
            "won": won,
            "max_floor": max_floor,
            "dims": dims,
            "archetype_label": archetype_label(arch),
            "cards": deck_full,
            "round": round_from_ts(d.get("ts")),
            "ts": d.get("ts"),
        }
        rows.append(row)
    # Sort by max_floor desc, then deck_size
    rows.sort(key=lambda r: (-(r["max_floor"] if isinstance(r["max_floor"], int) else 0),
                              -r["deck_size"]))

    return _render(rows, card_db, _read_eval_curves())


def _all_cards_table(card_db: dict) -> list[dict]:
    """All 86 wiki cards with score_card + empirical occurrences. Sorted by
    score desc — leaderboard view."""
    # Empirical occurrences from card_metadata
    occurrences = {}
    try:
        with open("data/card_metadata.json") as f:
            md = json.load(f)
        for cid, rec in md.items():
            occurrences[cid] = rec.get("occurrences", 0)
    except FileNotFoundError:
        pass

    seen_ids = set()
    rows = []
    for cid_key, rec in card_db.items():
        # Dedupe by wiki slug; lookup_card aliases multiple keys to the same rec
        slug = rec.get("id")
        if slug in seen_ids:
            continue
        seen_ids.add(slug)
        runtime_id = slug.upper().replace("-", "_")
        c = lookup_card(runtime_id, card_db)
        # Fall back to runtime-ID flavoured (some have _IRONCLAD suffix)
        for variant in (runtime_id, runtime_id + "_IRONCLAD"):
            if variant in occurrences:
                occ = occurrences[variant]
                break
        else:
            occ = occurrences.get(runtime_id.replace("_IRONCLAD", ""), 0)
        sc = score_card(c)
        rows.append({
            "id": runtime_id,
            "zh": c["zh_name"],
            "en": c["en_name"],
            "type": c["type"],
            "rarity": c["rarity"],
            "cost": c["cost"],
            "stats": c["stats"],
            "score": sc,
            "occ": occ,
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def _read_eval_curves() -> dict[str, list[tuple[float, float]]]:
    """Per training round, return [(rel_step_k, eval_avg_floor)] tuples."""
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    except Exception:
        return {}
    out = {}
    sources = [
        ("baseline", "checkpoints/tb_logs/MaskablePPO_10"),
        ("boss",     "checkpoints_boss/tb_logs/MaskablePPO_0"),
        ("boss2",    "checkpoints_boss2/tb_logs/MaskablePPO_0"),
        ("boss3",    "checkpoints_boss3/tb_logs/MaskablePPO_0"),
        ("hpw",      "checkpoints_hpw/tb_logs/MaskablePPO_0"),
        ("fix",      "checkpoints_fix/tb_logs/MaskablePPO_0"),
    ]
    for label, path in sources:
        if not os.path.isdir(path):
            continue
        try:
            acc = EventAccumulator(path, size_guidance={"scalars": 30000})
            acc.Reload()
            tags = acc.Tags().get("scalars", [])
            if "eval/avg_floor" not in tags:
                continue
            evs = acc.Scalars("eval/avg_floor")
            # Normalise per-round: pick the most recent training session in
            # this TB dir (events within 1 day of the latest wall_time), then
            # rebase x to start at 0 so curves are directly comparable
            # regardless of resume-step offset.
            if not evs:
                continue
            recent = max(e.wall_time for e in evs)
            evs = [e for e in evs if e.wall_time >= recent - 86400]
            if not evs:
                continue
            # Filter the ARM64-dotnet eval-race crash sentinel (avg_floor=1.0
            # over all 5 eval games == "every game crashed on floor 1"). These
            # points carry no training-progress signal — drop them.
            evs = [e for e in evs if e.value > 5.0]
            if not evs:
                continue
            x0 = min(e.step for e in evs)
            out[label] = [((e.step - x0) / 1000.0, float(e.value)) for e in evs]
        except Exception:
            continue
    return out


def _build_training_curves_svg(curves: dict, width: int = 720, height: int = 200) -> str:
    """Inline SVG line chart, no JS. X = relative training step (kilo-steps),
    Y = eval/avg_floor. One line per training round, baseline as reference."""
    if not curves:
        return '<div style="color:#7b8290; font-size:12px;">No TB data available</div>'

    pad = {"l": 36, "r": 12, "t": 12, "b": 24}
    all_x = [x for pts in curves.values() for x, _ in pts]
    all_y = [y for pts in curves.values() for _, y in pts]
    if not all_x:
        return '<div style="color:#7b8290; font-size:12px;">empty curves</div>'
    x_min, x_max = min(all_x + [0]), max(all_x + [300])
    y_min, y_max = min(all_y + [10]) - 0.5, max(all_y + [15]) + 0.5

    def sx(v):
        return pad["l"] + (v - x_min) / max(x_max - x_min, 1) * (width - pad["l"] - pad["r"])
    def sy(v):
        return height - pad["b"] - (v - y_min) / max(y_max - y_min, 1) * (height - pad["t"] - pad["b"])

    colors = {
        "baseline": "#9ca3af", "boss": "#f97316", "boss2": "#eab308",
        "boss3": "#a855f7", "hpw": "#22d3ee", "fix": "#34d399",
    }
    paths = []
    legend = []
    for label, pts in curves.items():
        if not pts:
            continue
        col = colors.get(label, "#fff")
        d = " ".join((f"M{sx(x):.1f},{sy(y):.1f}" if i == 0 else f"L{sx(x):.1f},{sy(y):.1f}")
                     for i, (x, y) in enumerate(pts))
        paths.append(f'<path d="{d}" stroke="{col}" stroke-width="1.5" fill="none"/>')
        # endpoint dot
        x_end, y_end = pts[-1]
        paths.append(f'<circle cx="{sx(x_end):.1f}" cy="{sy(y_end):.1f}" r="3" fill="{col}"/>')
        last_str = f"{y_end:.1f}"
        legend.append((label, col, last_str, len(pts)))

    # Y-axis ticks at integers
    y_ticks = []
    yi = int(y_min)
    while yi <= y_max:
        y_ticks.append(f'<text x="{pad["l"]-6}" y="{sy(yi)+3:.1f}" font-size="10" '
                       f'fill="#666" text-anchor="end">{yi}</text>'
                       f'<line x1="{pad["l"]}" y1="{sy(yi):.1f}" '
                       f'x2="{width-pad["r"]}" y2="{sy(yi):.1f}" '
                       f'stroke="#222" stroke-dasharray="2 4"/>')
        yi += 2
    # X-axis ticks every 50k
    x_ticks = []
    xi = 0
    while xi <= x_max + 0.1:
        x_ticks.append(f'<text x="{sx(xi):.1f}" y="{height-pad["b"]+14}" '
                       f'font-size="10" fill="#666" text-anchor="middle">{xi:.0f}k</text>')
        xi += 50

    legend_html = " ".join(
        f'<span style="color:{c};margin-right:14px;">●&nbsp;<b>{l}</b> '
        f'<span style="color:#7b8290">→ {v} (n={n})</span></span>'
        for l, c, v, n in legend
    )

    svg = f"""<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px; background:#0e1118; border:1px solid #1a1d24; border-radius:6px;">
      {''.join(y_ticks)}
      {''.join(x_ticks)}
      {''.join(paths)}
      <text x="{width/2}" y="{height-4}" font-size="11" fill="#7b8290" text-anchor="middle">training step (relative, kilo)</text>
    </svg>
    <div style="font-size:12px; margin-top:6px;">{legend_html}</div>"""
    return svg


_CARD_TYPE_COLOR = {
    "Attack": "#d97057", "Skill": "#5b9aa6", "Power": "#b48b5d",
    "?": "#888",
}
_RARITY_COLOR = {
    "Basic": "#9e9e9e",
    "Common": "#bdbdbd",
    "Uncommon": "#6fa3d6",
    "Rare": "#d2a23a",
    "?": "#777",
}


# Per-axis 1.0 anchor + dashed reference polygon target. Calibrated from
# actual deck history: cap = p95 of decks that reached floor >= 15 (the
# "boss-room dataset", n≈18k). target = p75 of the same bucket. So 1.0
# means "this axis matches the top-5% of decks that actually reached the
# boss room"; the dashed silhouette shows the typical boss-reach profile
# so a user can see at a glance which axis they're under on.
# Defaults are reasonable values if calibration fails / data is missing.
_DIM_CAP = {"attack": 5.94, "defense": 3.07, "energy": 0.23, "draw": 0.25}
_DIM_TARGET = {"attack": 4.67, "defense": 2.33, "energy": 0.13, "draw": 0.13}
_DIM_LABEL = {"attack": "输出", "defense": "防御",
              "energy": "运转·能量", "draw": "运转·抽牌"}


_CALIB_STATS = {"n_high": 0, "n_total": 0}


def calibrate_dim_caps(jsonl_path: str = "data/deck_history.jsonl",
                       floor_threshold: int = 15) -> None:
    """Walk deck_history once and rewrite _DIM_CAP / _DIM_TARGET to p95 / p75
    of the high-floor bucket. Called from main() so re-renders pick up
    whatever the dataset currently looks like."""
    try:
        import numpy as np
    except ImportError:
        return
    if not os.path.exists(jsonl_path):
        return
    outcomes: dict[str, dict] = {}
    last_milestones: dict[str, dict] = {}
    with open(jsonl_path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = o.get("run_id"); ev = o.get("event")
            if not rid: continue
            if ev == "milestone":
                cur = last_milestones.get(rid)
                if cur is None or o.get("floor_crossed", 0) > cur.get("floor_crossed", 0):
                    last_milestones[rid] = o
            elif ev == "outcome":
                outcomes[rid] = o
    card_db = load_card_db()
    vals: dict[str, list[float]] = {k: [] for k in _DIM_CAP}
    for rid, ms in last_milestones.items():
        out = outcomes.get(rid)
        if not out or out.get("max_floor", 0) < floor_threshold:
            continue
        deck = [lookup_card(c, card_db) for c in ms.get("cards", [])]
        if len(deck) < 2:
            continue
        d = score_deck_dimensions(deck)
        for k in vals:
            vals[k].append(d[k])
    n = len(vals["attack"])
    _CALIB_STATS["n_high"] = n
    _CALIB_STATS["n_total"] = len(outcomes)
    if n < 200:  # not enough data for stable percentiles
        print(f"  dim calibration: only {n} high-floor decks, keeping defaults",
              file=sys.stderr)
        return
    for k in _DIM_CAP:
        a = np.array(vals[k])
        _DIM_CAP[k] = float(round(np.percentile(a, 95), 2))
        _DIM_TARGET[k] = float(round(np.percentile(a, 75), 2))
    print(f"  dim calibration over n={n} f≥{floor_threshold} decks: "
          f"caps={_DIM_CAP}, targets={_DIM_TARGET}", file=sys.stderr)


def _radar_svg(dims: dict, w: int = 200, h: int = 160) -> str:
    """Quadrilateral radar of 4 dims, normalised by _DIM_CAP.

    Wider than tall — 防御 / 抽牌 axis labels sit east/west of the chart and
    each needs ~50 px for "防御 X.XX". cx pushed slightly right of centre so
    the longest right-side label fits inside the viewBox without clipping.
    """
    import math
    cx, cy = w * 0.46, h * 0.5 - 4   # nudge plot left to balance right-side labels
    r_max = min(w, h) * 0.30  # ≈ 48 px radius — room for 4-direction labels
    keys = ["attack", "defense", "energy", "draw"]
    # angle 0 = top, clockwise: attack (top), defense (right), energy (bot), draw (left)
    angles = [-math.pi / 2 + i * math.pi / 2 for i in range(4)]

    # Grid rings (25/50/75/100 %)
    rings = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = [(cx + r_max * frac * math.cos(a), cy + r_max * frac * math.sin(a))
               for a in angles]
        ring = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        rings.append(f'<polygon points="{ring}" fill="none" stroke="#2a2d36" stroke-width="0.6"/>')

    # Axis lines
    axes = []
    for a in angles:
        x = cx + r_max * math.cos(a)
        y = cy + r_max * math.sin(a)
        axes.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                    f'stroke="#2a2d36" stroke-width="0.5"/>')

    # Reference polygon — typical boss-reach deck (p75 of high-floor bucket).
    # Drawn first so the data polygon overlays it cleanly.
    ref_pts = []
    for k, a in zip(keys, angles):
        norm = min(1.0, _DIM_TARGET[k] / _DIM_CAP[k])
        x = cx + r_max * norm * math.cos(a)
        y = cy + r_max * norm * math.sin(a)
        ref_pts.append((x, y))
    ref_poly_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in ref_pts)
    ref_poly = (f'<polygon points="{ref_poly_str}" fill="none" '
                f'stroke="#7b8290" stroke-width="0.8" stroke-dasharray="3 2"/>')

    # Data polygon
    pts = []
    for k, a in zip(keys, angles):
        v = dims.get(k, 0) or 0
        norm = min(1.0, max(0.0, v / _DIM_CAP[k]))
        x = cx + r_max * norm * math.cos(a)
        y = cy + r_max * norm * math.sin(a)
        pts.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    data_poly = (f'<polygon points="{poly}" fill="#fcd34d33" '
                 f'stroke="#fcd34d" stroke-width="1.4"/>')

    # Vertices
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#fcd34d"/>'
                   for x, y in pts)

    # Axis labels around the outside
    labels = []
    for k, a in zip(keys, angles):
        lx = cx + (r_max + 4) * math.cos(a)
        ly = cy + (r_max + 4) * math.sin(a)
        v = dims.get(k, 0) or 0
        anchor = ("middle" if abs(math.cos(a)) < 0.3
                  else ("start" if math.cos(a) > 0 else "end"))
        baseline = "central" if abs(math.sin(a)) < 0.3 else \
                   ("hanging" if math.sin(a) > 0 else "baseline")
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="{baseline}" font-size="10" fill="#9ca3af">'
            f'{_DIM_LABEL[k]}<tspan fill="#fcd34d" dx="3">{v:.2f}</tspan></text>'
        )

    # Inline legend strip — clarifies the dashed polygon without forcing
    # the reader back up to the page header.
    legend = (
        f'<g transform="translate({w*0.04:.0f},{h-8})">'
        f'  <polygon points="0,0 4,4 0,8 -4,4" fill="#fcd34d33" stroke="#fcd34d" stroke-width="1"/>'
        f'  <text x="8" y="6" font-size="9" fill="#7b8290">本 deck</text>'
        f'  <polygon points="50,0 54,4 50,8 46,4" fill="none" stroke="#7b8290" stroke-width="0.8" stroke-dasharray="2 1.5"/>'
        f'  <text x="58" y="6" font-size="9" fill="#7b8290">过 boss 典型</text>'
        f'</g>'
    )
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'style="flex:0 0 {w}px;">'
            + "".join(rings) + "".join(axes) + ref_poly + data_poly + dots
            + "".join(labels) + legend + '</svg>')


_ROUND_COLORS = {
    "baseline": "#9ca3af", "boss": "#f97316", "boss2": "#eab308",
    "boss3": "#a855f7", "hpw": "#22d3ee", "fix": "#34d399", "?": "#555",
}


def _chip_tooltip(c: dict) -> str:
    """Build the per-card hover text shown via title=. score_card breakdown."""
    sc = score_card(c)
    stats = c.get("stats") or {}
    parts = [f"{c['en_name']} / {c['zh_name']}"]
    parts.append(f"{c['type']} · {c['rarity']} · cost {c['cost']}")
    if stats:
        parts.append("stats: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    desc = c.get("description") or ""
    if desc:
        parts.append(desc[:120])
    parts.append(f"score_card = {sc:.2f}")
    return " · ".join(parts)


def _render(rows: list[dict], card_db: dict, curves: dict) -> str:
    n = len(rows)
    won_count = sum(1 for r in rows if r["won"])
    avg_size = sum(r["deck_size"] for r in rows) / max(n, 1)
    avg_floor = sum(r["max_floor"] for r in rows
                    if isinstance(r["max_floor"], int)) / max(n, 1)

    panels = []
    for r in rows:
        won_badge = (
            '<span class="badge won">Won</span>' if r["won"]
            else '<span class="badge died">Died</span>'
        )
        v2_str = f"{r['v2']:.1f}" if r["v2"] is not None else "n/a"
        # Prediction error: predicted - actual max_floor (signed, smaller is better)
        if r["v2"] is not None and isinstance(r["max_floor"], int):
            err = r["v2"] - r["max_floor"]
            err_str = f"{err:+.1f}"
        else:
            err_str = "—"
        v1_str = f"{r['v1']:.2f}"

        # Card chips with hover tooltips (score_card breakdown)
        chips = []
        for c in r["cards"]:
            tcol = _CARD_TYPE_COLOR.get(c["type"], "#888")
            rcol = _RARITY_COLOR.get(c["rarity"], "#888")
            upg = "+" if c["upgraded"] else ""
            cost = c["cost"] if c["cost"] not in ("?", "X") else "·"
            tip = _chip_tooltip(c).replace('"', "&quot;")
            chips.append(
                f'<span class="chip" title="{tip}" style="border-left:3px solid {tcol};'
                f' background:linear-gradient(90deg,{rcol}22,#1a1a1a 60%);">'
                f'<span class="cost">{cost}</span>'
                f'<span class="cname">{c["zh_name"]}{upg}</span>'
                f'</span>'
            )

        # Dim radar (polygon) — 4 axes mapped to the article's 3-port framework
        # 输出/防御/运转(拆抽牌+能量), each normalised by a "strong-deck" cap
        # so a deck around the cap fills the polygon completely, beyond just
        # extends to a soft outer ring.
        dim_html = _radar_svg(r["dims"])

        rd = r.get("round", "?")
        rd_col = _ROUND_COLORS.get(rd, "#555")
        panels.append(f"""
        <div class="deck-panel" data-round="{rd}" data-outcome="{'won' if r['won'] else 'died'}">
          <div class="panel-head">
            <div class="floor-tag">F{r['max_floor']}</div>
            <div class="meta">
              <div class="title">{r['archetype_label']} <span class="round-badge" style="background:{rd_col}22;color:{rd_col};">{rd}</span></div>
              <div class="sub">@F{r['floor_at']} · size {r['deck_size']} · {r['run_id'][:14]}</div>
            </div>
            <div class="scores">
              {won_badge}
              <div class="score-row"><span class="score-label">v1 quality</span><span class="score-val">{v1_str}</span></div>
              <div class="score-row"><span class="score-label">v2 pred fl</span><span class="score-val v2val">{v2_str}</span></div>
              <div class="score-row"><span class="score-label">Δ pred-actual</span><span class="score-val">{err_str}</span></div>
            </div>
          </div>
          <div class="panel-body">
            <div class="cards">{"".join(chips)}</div>
            <div class="dims">{dim_html}</div>
          </div>
        </div>
        """)

    # Round breakdown for summary
    by_round = defaultdict(list)
    for r in rows:
        by_round[r["round"]].append(r)
    round_summary = []
    for rd_name, rs in sorted(by_round.items()):
        m = sum(r["max_floor"] for r in rs if isinstance(r["max_floor"], int)) / max(len(rs), 1)
        w = sum(1 for r in rs if r["won"])
        col = _ROUND_COLORS.get(rd_name, "#555")
        round_summary.append(
            f'<span class="round-pill" style="border-color:{col};color:{col};">'
            f'<b>{rd_name}</b> n={len(rs)} · floor={m:.1f} · won={w}</span>'
        )
    round_summary_html = " ".join(round_summary)

    # Card scoring table (top-30 to keep size tame; rest accessible via expansion)
    card_rows = _all_cards_table(card_db)
    table_rows = []
    for c in card_rows:
        tcol = _CARD_TYPE_COLOR.get(c["type"], "#888")
        rcol = _RARITY_COLOR.get(c["rarity"], "#888")
        stats_str = ", ".join(f"{k}={v}" for k, v in c["stats"].items()) if c["stats"] else "—"
        table_rows.append(
            f'<tr>'
            f'<td style="color:{rcol};">{c["zh"]}</td>'
            f'<td style="color:#9ca3af;">{c["en"]}</td>'
            f'<td><span style="color:{tcol};">{c["type"]}</span></td>'
            f'<td>{c["rarity"]}</td>'
            f'<td style="text-align:center;">{c["cost"]}</td>'
            f'<td style="color:#7b8290; font-size:11px;">{stats_str}</td>'
            f'<td style="text-align:right; color:#fcd34d; font-family:ui-monospace,monospace;">{c["score"]:.2f}</td>'
            f'<td style="text-align:right; color:#9ca3af;">{c["occ"]:,}</td>'
            f'</tr>'
        )

    # Training curves SVG
    curves_svg = _build_training_curves_svg(curves)

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>STS2 — Deck Builds Viewer</title>
<style>
  body {{
    background:#0b0d12; color:#e5e7eb; margin:0;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    line-height:1.4;
  }}
  header {{
    padding:24px 32px; border-bottom:1px solid #222;
    background:linear-gradient(180deg,#171a22,#0b0d12);
    position:sticky; top:0; z-index:5;
  }}
  header h1 {{ margin:0 0 4px; font-size:18px; font-weight:600; letter-spacing:.04em; }}
  header .stats {{ font-size:13px; color:#9ca3af; }}
  header .stats span {{ margin-right:18px; }}
  .legend {{ display:inline-flex; gap:10px; margin-left:24px; font-size:12px; }}
  .legend i {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:middle;}}
  .grid {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(420px, 1fr));
    gap:14px; padding:18px;
  }}
  .deck-panel {{
    background:#13161e; border:1px solid #222; border-radius:8px; padding:12px;
    display:flex; flex-direction:column; min-width:0;
  }}
  .panel-head {{
    display:flex; align-items:center; gap:12px; padding-bottom:10px;
    border-bottom:1px dashed #2a2d36;
  }}
  .floor-tag {{
    flex:0 0 auto; width:46px; height:46px; border-radius:8px;
    background:#1f2532; color:#e5e7eb; font-weight:600; font-size:14px;
    display:flex; align-items:center; justify-content:center;
  }}
  .meta {{ flex:1; min-width:0; }}
  .meta .title {{ font-weight:600; font-size:14px; color:#f3f4f6; }}
  .meta .sub {{ font-size:11px; color:#7b8290; margin-top:2px; }}
  .scores {{ flex:0 0 auto; text-align:right; font-size:12px; }}
  .score-row {{ display:flex; gap:6px; justify-content:flex-end; }}
  .score-label {{ color:#7b8290; }}
  .score-val {{ font-family: ui-monospace, "SF Mono", monospace; color:#e5e7eb; }}
  .v2val {{ color:#fcd34d; }}
  .badge {{
    display:inline-block; padding:1px 8px; border-radius:10px;
    font-size:10px; font-weight:600; letter-spacing:.06em;
    margin-bottom:4px;
  }}
  .badge.won {{ background:#15402a; color:#86efac; }}
  .badge.died {{ background:#3a1a1a; color:#fca5a5; }}
  .panel-body {{ display:flex; gap:14px; padding-top:10px; min-width:0; }}
  .cards {{
    flex:1; display:flex; flex-wrap:wrap; gap:4px; align-content:flex-start;
    min-width:0;
  }}
  .chip {{
    display:inline-flex; align-items:center; gap:5px;
    padding:2px 8px 2px 6px;
    border-radius:3px;
    font-size:11px;
    background:#1a1a1a;
    border:1px solid #25282f;
    color:#e5e7eb;
    max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }}
  .chip .cost {{
    width:14px; height:14px; border-radius:50%;
    background:#3b3f4a; color:#fff;
    display:inline-flex; align-items:center; justify-content:center;
    font-size:10px; flex:0 0 auto;
  }}
  .dims {{
    flex:0 0 auto; font-size:10px; color:#9ca3af;
    display:flex; align-items:center;
  }}

  /* Round badge + filter pill */
  .round-badge {{
    display:inline-block; padding:1px 6px; border-radius:8px;
    font-size:9px; font-weight:600; letter-spacing:.05em;
    margin-left:6px; vertical-align:middle;
  }}
  .round-pill {{
    display:inline-block; border:1px solid; padding:2px 10px; border-radius:12px;
    font-size:11px; margin-right:8px; background:#0f1218;
  }}
  /* Section heads */
  section {{ padding: 18px 22px; border-bottom:1px solid #1a1d24; }}
  section h2 {{ margin:0 0 12px; font-size:13px; color:#9ca3af; font-weight:600; letter-spacing:.05em; text-transform:uppercase;}}

  /* Filter controls */
  .controls {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:10px;}}
  .controls select {{
    background:#1a1d24; color:#e5e7eb; border:1px solid #333; border-radius:4px;
    padding:4px 8px; font-size:12px;
  }}

  /* Card scoring table */
  table.cards-table {{
    width:100%; border-collapse:collapse; font-size:12px;
    background:#13161e;
  }}
  table.cards-table th {{
    text-align:left; padding:6px 8px; border-bottom:2px solid #25282f;
    color:#7b8290; font-weight:500; font-size:11px; text-transform:uppercase;
    position:sticky; top:0; background:#13161e;
  }}
  table.cards-table td {{ padding:5px 8px; border-bottom:1px solid #1a1d24;}}
  table.cards-table tr:hover td {{ background:#1a1d24;}}
  .table-wrap {{ max-height:520px; overflow-y:auto; border:1px solid #1a1d24; border-radius:6px;}}
</style>
</head>
<body>
  <header>
    <h1>STS2 · Deck Build Viewer</h1>
    <div class="stats">
      <span>{n} decks sampled</span>
      <span>Won: {won_count} ({100*won_count/max(n,1):.0f}%)</span>
      <span>avg deck size: {avg_size:.1f}</span>
      <span>avg final floor: {avg_floor:.1f}</span>
    </div>
    <div class="controls">
      <span style="font-size:11px;color:#7b8290;">Filter:</span>
      <select id="round-filter" onchange="filterPanels()">
        <option value="">All rounds</option>
        <option value="baseline">baseline</option>
        <option value="boss">boss</option>
        <option value="boss2">boss2</option>
        <option value="boss3">boss3</option>
        <option value="hpw">hpw</option>
        <option value="fix">fix</option>
      </select>
      <select id="outcome-filter" onchange="filterPanels()">
        <option value="">All outcomes</option>
        <option value="won">Won only</option>
        <option value="died">Died only</option>
      </select>
      <span class="legend" style="margin-left:14px;font-size:11px;">
        <span><i style="background:#d97057"></i>Attack</span>
        <span><i style="background:#5b9aa6"></i>Skill</span>
        <span><i style="background:#b48b5d"></i>Power</span>
        <span style="margin-left:18px;">
          <svg width="14" height="14" style="vertical-align:middle"><polygon points="7,2 12,7 7,12 2,7" fill="#fcd34d33" stroke="#fcd34d" stroke-width="1.2"/></svg>
          deck
        </span>
        <span>
          <svg width="14" height="14" style="vertical-align:middle"><polygon points="7,3 11,7 7,11 3,7" fill="none" stroke="#7b8290" stroke-width="0.8" stroke-dasharray="2 1.5"/></svg>
          target (p75 of f≥15 decks)
        </span>
      </span>
    </div>
    <div style="font-size:11px; color:#7b8290; margin-top:6px;">
      Radar 1.0 anchor = p95 of decks that reached f≥15
      ({_CALIB_STATS['n_high']:,} of {_CALIB_STATS['n_total']:,} runs).
      Closer to 1 on an axis ≈ "you match the top-5% of boss-reach decks on that axis";
      dashed silhouette is the typical (p75) boss-reach profile.
      Caps: 输出={_DIM_CAP['attack']}, 防御={_DIM_CAP['defense']},
      运转·能量={_DIM_CAP['energy']}, 运转·抽牌={_DIM_CAP['draw']}.
    </div>
  </header>

  <section>
    <h2>Training rounds — eval/avg_floor over training step</h2>
    {curves_svg}
    <div style="margin-top:12px;">{round_summary_html}</div>
  </section>

  <section>
    <h2>Sampled decks ({n})</h2>
    <div class="grid" id="deck-grid">
      {''.join(panels)}
    </div>
  </section>

  <section>
    <h2>All 86 cards — score_card + empirical occurrences</h2>
    <p style="font-size:11px;color:#7b8290;margin:0 0 8px;">
      Sorted by score_card desc. Hover any card chip in a panel above to see this same breakdown inline.
    </p>
    <div class="table-wrap">
    <table class="cards-table">
      <thead>
        <tr>
          <th>中文</th><th>EN</th><th>type</th><th>rarity</th>
          <th style="text-align:center;">cost</th><th>stats</th>
          <th style="text-align:right;">score</th>
          <th style="text-align:right;">picks (real)</th>
        </tr>
      </thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
    </div>
  </section>

<script>
function filterPanels() {{
  var r = document.getElementById('round-filter').value;
  var o = document.getElementById('outcome-filter').value;
  var panels = document.querySelectorAll('#deck-grid .deck-panel');
  panels.forEach(function(p) {{
    var ok = (!r || p.dataset.round === r) && (!o || p.dataset.outcome === o);
    p.style.display = ok ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="decks.html",
                   help="output HTML path")
    p.add_argument("--n", type=int, default=100,
                   help="how many distinct runs to sample")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--input", default="data/deck_history.jsonl")
    args = p.parse_args()

    card_db = load_card_db()
    print(f"Card DB: {len({c['id'] for c in [v for v in card_db.values()]})} unique cards", file=sys.stderr)
    calibrate_dim_caps(args.input)
    decks = sample_milestones(args.input, n=args.n, seed=args.seed)
    print(f"Sampled {len(decks)} decks from {args.input}", file=sys.stderr)
    html = build_html(decks, card_db)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()

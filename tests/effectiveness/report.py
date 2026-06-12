"""Aggregation, baseline deltas, and run.json / run.md emission.

Advisory by design: deltas and the 20% regression banner inform, never block.
Errors are first-class rows — a failed cell is excluded from aggregates but
always counted and listed (no silent drops).
"""

from __future__ import annotations

import json
from pathlib import Path

# Direction that means "better" for each headline metric.
METRIC_DIRECTION = {
    "findings_per_task": "lower",
    "verification_rate": "higher",
    "duplication_rate": "lower",
    "cost_usd_mean": "lower",
    "wall_seconds_mean": "lower",
}

_REGRESSION_TOLERANCE_PCT = 20.0


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(cells: list[dict]) -> dict[str, dict]:
    """Per (category, arm) headline metrics over ok cells.

    findings_per_task = mean of (convention violations + crossfile
    broken_exports + crossfile callers_stale) over cells where at least one
    component scored; kept for baseline continuity. The per-component means
    (conv_violations_mean, broken_exports_mean, callers_stale_mean) are what
    run.md reports — a blended sum hides which scorer moved.
    verification_rate uses the transcript-side signal (the only one
    comparable across arms — the off arm has no exec log).
    duplication_rate = share of duplication-scored cells that either added a
    body-hash duplicate or failed to reference the existing helper.
    """
    out: dict[str, dict] = {}
    ok = [c for c in cells if c.get("status") == "ok"]
    for cat, arm in sorted({(c["category"], c["arm"]) for c in ok}):
        group = [c for c in ok if c["category"] == cat and c["arm"] == arm]
        findings: list[float] = []
        conv_vals: list[float] = []
        broken_vals: list[float] = []
        stale_vals: list[float] = []
        verify: list[float] = []
        dup: list[float] = []
        cost: list[float] = []
        wall: list[float] = []
        for c in group:
            s = c.get("scores") or {}
            total = 0
            have = False
            conv = s.get("convention") or {}
            if isinstance(conv.get("violations"), int):
                total += conv["violations"]
                conv_vals.append(float(conv["violations"]))
                have = True
            cf = s.get("crossfile") or {}
            for key, bucket in (("broken_exports", broken_vals), ("callers_stale", stale_vals)):
                if isinstance(cf.get(key), int):
                    total += cf[key]
                    bucket.append(float(cf[key]))
                    have = True
            if have:
                findings.append(float(total))
            ver = s.get("verification") or {}
            if isinstance(ver.get("test_cmd_in_transcript"), bool):
                verify.append(1.0 if ver["test_cmd_in_transcript"] else 0.0)
            d = s.get("duplication") or {}
            if isinstance(d.get("body_hash_duplicates"), int):
                duplicated = d["body_hash_duplicates"] > 0 or d.get("reuse_credit") is False
                dup.append(1.0 if duplicated else 0.0)
            co = s.get("cost") or {}
            if isinstance(co.get("cost_usd"), (int, float)):
                cost.append(float(co["cost_usd"]))
            if isinstance(co.get("wall_seconds"), (int, float)):
                wall.append(float(co["wall_seconds"]))
        out[f"{cat}|{arm}"] = {
            "cells": len(group),
            "findings_per_task": _mean(findings),
            "conv_violations_mean": _mean(conv_vals),
            "broken_exports_mean": _mean(broken_vals),
            "callers_stale_mean": _mean(stale_vals),
            "verification_rate": _mean(verify),
            "duplication_rate": _mean(dup),
            "cost_usd_mean": _mean(cost),
            "wall_seconds_mean": _mean(wall),
        }
    return out


def load_baselines(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def compare_to_baseline(aggregates: dict, baselines_doc: dict, tier: str) -> list[dict]:
    rows: list[dict] = []
    tier_base = ((baselines_doc or {}).get("baselines") or {}).get(tier) or {}
    for key, metrics in sorted(aggregates.items()):
        cat, arm = key.split("|", 1)
        base = (tier_base.get(cat) or {}).get(arm) or {}
        for metric, direction in METRIC_DIRECTION.items():
            cur = metrics.get(metric)
            old = base.get(metric)
            if not isinstance(cur, (int, float)) or not isinstance(old, (int, float)):
                continue
            if old == 0:
                delta_pct = None
                regression = direction == "lower" and cur > 0
            else:
                delta_pct = round((cur - old) / abs(old) * 100.0, 1)
                if direction == "lower":
                    regression = delta_pct > _REGRESSION_TOLERANCE_PCT
                else:
                    regression = delta_pct < -_REGRESSION_TOLERANCE_PCT
            rows.append(
                {
                    "category": cat,
                    "arm": arm,
                    "metric": metric,
                    "baseline": old,
                    "current": cur,
                    "delta_pct": delta_pct,
                    "regression": bool(regression),
                }
            )
    return rows


def render_run_md(
    *,
    run_id: str,
    tier: str,
    arms: list[str],
    model: str,
    toggle: str | None,
    cells: list[dict],
    aggregates: dict,
    deltas: list[dict],
    panel_rows: list[dict],
    total_cost_usd: float,
) -> str:
    errors = [c for c in cells if c.get("status") == "error"]
    skipped = [c for c in cells if c.get("status") == "skipped"]
    regressions = [d for d in deltas if d.get("regression")]

    lines = [f"# Effectiveness run {run_id}", ""]
    if regressions:
        lines += ["**!! REGRESSION vs baseline** (advisory, never blocking):", ""]
        for d in regressions:
            delta = "n/a (baseline 0)" if d["delta_pct"] is None else f"{d['delta_pct']:+.1f}%"
            lines.append(
                f"- {d['category']}/{d['arm']} {d['metric']}: "
                f"{d['baseline']} -> {d['current']} ({delta})"
            )
        lines.append("")
    lines += [
        f"tier: {tier} | arms: {', '.join(arms)} | model: {model} | toggle: {toggle or 'none'}",
        f"cells: {len(cells)} ok: {sum(1 for c in cells if c['status'] == 'ok')} "
        f"errors: {len(errors)} skipped: {len(skipped)} | total cost: ${total_cost_usd:.2f}",
        "",
        "## Aggregates",
        "",
        "| category | arm | cells | conv viol | broken exp | stale callers "
        "| verify rate | dup rate | $ mean | wall s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, m in sorted(aggregates.items()):
        cat, arm = key.split("|", 1)

        def fmt(v):
            return "-" if v is None else f"{v}"

        lines.append(
            f"| {cat} | {arm} | {m['cells']} | {fmt(m.get('conv_violations_mean'))} | "
            f"{fmt(m.get('broken_exports_mean'))} | {fmt(m.get('callers_stale_mean'))} | "
            f"{fmt(m['verification_rate'])} | {fmt(m['duplication_rate'])} | "
            f"{fmt(m['cost_usd_mean'])} | {fmt(m['wall_seconds_mean'])} |"
        )
    if panel_rows:
        lines += [
            "",
            "## Judge panel",
            "",
            "| task | pair | winner | valid votes | $ |",
            "|---|---|---|---|---|",
        ]
        for p in panel_rows:
            lines.append(
                f"| {p['task_id']} | {p['pair'][0]} vs {p['pair'][1]} | "
                f"{p.get('panel_winner', 'unscored')} | "
                f"{p.get('panel_votes_valid', 0)} | {p.get('panel_cost_usd', 0.0)} |"
            )
    if deltas:
        lines += [
            "",
            "## Baseline deltas",
            "",
            "| category | arm | metric | baseline | current | delta | flag |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in deltas:
            delta = "n/a" if d["delta_pct"] is None else f"{d['delta_pct']:+.1f}%"
            flag = "REGRESSION" if d["regression"] else ""
            lines.append(
                f"| {d['category']} | {d['arm']} | {d['metric']} | {d['baseline']} | "
                f"{d['current']} | {delta} | {flag} |"
            )
    else:
        lines += [
            "",
            "_No baseline entries for this tier yet (baselines.json is empty",
            "until the first release-time update)._",
        ]
    if errors or skipped:
        lines += ["", "## Errors and skips (excluded from aggregates, never dropped)", ""]
        for c in errors + skipped:
            lines.append(
                f"- {c['task_id']} | {c['arm']} | repeat {c['repeat']} | "
                f"{c['status']}: {c.get('reason') or 'unknown'}"
            )
    return "\n".join(lines) + "\n"


def write_outputs(run_dir: Path, run_doc: dict) -> None:
    (run_dir / "run.json").write_text(
        json.dumps(run_doc, indent=2, sort_keys=False), encoding="utf-8"
    )
    md = render_run_md(
        run_id=run_doc["run_id"],
        tier=run_doc["tier"],
        arms=run_doc["arms"],
        model=run_doc["model"],
        toggle=run_doc.get("toggle"),
        cells=run_doc["cells"],
        aggregates=run_doc["aggregates"],
        deltas=run_doc["baseline_deltas"],
        panel_rows=run_doc.get("panel") or [],
        total_cost_usd=run_doc["total_cost_usd"],
    )
    (run_dir / "run.md").write_text(md, encoding="utf-8")

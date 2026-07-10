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
        # An arm runs on one model within a run, so the group's model is uniform;
        # default to sonnet for legacy cells that predate per-cell model capture.
        # Kept as an entry field (not folded into the key) so run.json aggregate
        # keys stay `cat|arm` and only the baseline lookup becomes model-aware.
        cell_models = {c.get("model") for c in group if c.get("model")}
        model = next(iter(cell_models)) if len(cell_models) == 1 else "sonnet"
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
            "model": model,
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


def arm_overhead(cells: list[dict]) -> dict[str, dict]:
    """Per-arm turn/cost/wall rollup across every category. Informational.

    turns_mean / cost_usd_mean / wall_seconds_mean average over ok cells (the
    same population as the other aggregates; a legacy cell without a recorded
    num_turns is skipped, never counted as zero). error_max_turns counts every
    cell whose session died at the turn cap regardless of status: a truncated
    cell measures nothing, so the count itself is the signal.
    """
    out: dict[str, dict] = {}
    for arm in sorted({c["arm"] for c in cells if c.get("arm")}):
        group = [c for c in cells if c.get("arm") == arm]
        ok = [c for c in group if c.get("status") == "ok"]
        turns: list[float] = []
        cost: list[float] = []
        wall: list[float] = []
        for c in ok:
            s = c.get("session") or {}
            if isinstance(s.get("num_turns"), (int, float)):
                turns.append(float(s["num_turns"]))
            if isinstance(s.get("cost_usd"), (int, float)):
                cost.append(float(s["cost_usd"]))
            if isinstance(s.get("wall_seconds"), (int, float)):
                wall.append(float(s["wall_seconds"]))
        capped = sum(
            1
            for c in group
            if (c.get("session") or {}).get("result_subtype") == "error_max_turns"
            or "error_max_turns" in (c.get("reason") or "")
        )
        out[arm] = {
            "cells": len(group),
            "ok_cells": len(ok),
            "turns_mean": _mean(turns),
            "error_max_turns": capped,
            "cost_usd_mean": _mean(cost),
            "wall_seconds_mean": _mean(wall),
        }
    return out


def cost_adjusted_rows(preference_rows: list[dict], overhead: dict[str, dict]) -> list[dict]:
    """Per compared arm pair, the judged lift netted against extra spend.

    lift_per_dollar = (preference - 0.5) / (cost_usd_mean_treatment -
    cost_usd_mean_control); lift_per_wall_minute divides by the wall-time
    delta in minutes. A denominator <= 0 (treatment not costlier) yields None;
    the raw preference already answers that case. Informational only.
    """

    def _lift(pref: float, delta: float | None) -> float | None:
        if delta is None or delta <= 0:
            return None
        return round((pref - 0.5) / delta, 4)

    def _delta(a: str, b: str, key: str) -> float | None:
        va = (overhead.get(a) or {}).get(key)
        vb = (overhead.get(b) or {}).get(key)
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            return None
        return vb - va

    rows: list[dict] = []
    for p in preference_rows:
        control, treatment = p.get("control"), p.get("treatment")
        pref = p.get("rate")
        if not control or not treatment or not isinstance(pref, (int, float)):
            continue
        cost_delta = _delta(control, treatment, "cost_usd_mean")
        wall_delta = _delta(control, treatment, "wall_seconds_mean")
        rows.append(
            {
                "control": control,
                "treatment": treatment,
                "preference": pref,
                "cost_usd_delta": cost_delta,
                "wall_seconds_delta": wall_delta,
                "lift_per_dollar": _lift(pref, cost_delta),
                "lift_per_wall_minute": _lift(
                    pref, wall_delta / 60.0 if wall_delta is not None else None
                ),
            }
        )
    return rows


def paired_preference_cis(panel_rows: list[dict]) -> list[dict]:
    """Per arm-pair, the paired cluster-bootstrap CI on the judge's preference for
    the non-'off' (treatment) arm. winner==treatment -> 1, ==control -> 0,
    tie -> 0.5; resampled by TASK so repeats are not pseudo-replicates. A causal
    preference is established only when the CI lower bound > 0.5.
    """
    from tests.effectiveness.stats import group_by_task, paired_bootstrap_ci

    by_pair: dict[tuple, list[dict]] = {}
    for p in panel_rows:
        pair = tuple(p.get("pair") or ())
        if len(pair) != 2:
            continue
        by_pair.setdefault(pair, []).append(p)

    out: list[dict] = []
    for pair, rows in by_pair.items():
        control = "off" if "off" in pair else pair[0]
        treatment = pair[1] if pair[0] == control else pair[0]
        scored: list[dict] = []
        for r in rows:
            w = r.get("panel_winner")
            if w not in (treatment, control, "tie"):
                continue  # unscored / no valid majority
            win = 1.0 if w == treatment else (0.5 if w == "tie" else 0.0)
            scored.append({"task": r["task_id"], "win": win})
        if not scored:
            continue
        ci = paired_bootstrap_ci(group_by_task(scored, task_key="task", win_key="win"))
        out.append({"control": control, "treatment": treatment, **ci})
    return out


def load_baselines(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_arm_baseline(base_arm: dict, model: str) -> dict:
    """The metrics dict for ``model`` under an arm's baseline entry.

    Supports both the model-keyed schema (``{model: {metric: val}}``) and the
    legacy flat schema (``{metric: val}``, all sonnet). A flat entry answers only
    the sonnet model, so an opus/fable arm compared against a sonnet-only baseline
    gets no comparison rather than a spurious cross-model regression.
    """
    if not isinstance(base_arm, dict):
        return {}
    sub = base_arm.get(model)
    if isinstance(sub, dict):
        return sub
    if model == "sonnet" and any(m in base_arm for m in METRIC_DIRECTION):
        return base_arm
    return {}


def compare_to_baseline(aggregates: dict, baselines_doc: dict, tier: str) -> list[dict]:
    rows: list[dict] = []
    tier_base = ((baselines_doc or {}).get("baselines") or {}).get(tier) or {}
    for key, metrics in sorted(aggregates.items()):
        cat, arm = key.split("|", 1)
        model = metrics.get("model") or "sonnet"
        base = _resolve_arm_baseline((tier_base.get(cat) or {}).get(arm) or {}, model)
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
                    "model": model,
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
            arm_label = (
                d["arm"] if d.get("model") in (None, "sonnet") else f"{d['arm']}@{d['model']}"
            )
            lines.append(
                f"- {d['category']}/{arm_label} {d['metric']}: "
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
    overhead = arm_overhead(cells)
    if overhead:
        lines += [
            "",
            "## Per-arm turn overhead (advisory, never blocking)",
            "",
            "turns_mean charges the arm's real turn overhead over ok cells; "
            "error_max_turns counts cells that died at the turn cap (a "
            "truncated cell measures nothing, so the count is the signal).",
            "",
            "| arm | ok cells | turns_mean | error_max_turns | $ mean | wall s mean |",
            "|---|---|---|---|---|---|",
        ]
        for arm, o in sorted(overhead.items()):

            def fmt(v):
                return "-" if v is None else f"{v}"

            lines.append(
                f"| {arm} | {o['ok_cells']} | {fmt(o['turns_mean'])} | "
                f"{o['error_max_turns']} | {fmt(o['cost_usd_mean'])} | "
                f"{fmt(o['wall_seconds_mean'])} |"
            )
    cis = paired_preference_cis(panel_rows) if panel_rows else []
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
        if cis:
            lines += [
                "",
                "### Causal preference (paired cluster-bootstrap 95% CI)",
                "",
                "Preference for the treatment arm over control; resampled by TASK. "
                "A causal win requires the CI lower bound > 0.5.",
                "",
                "| control | treatment | preference | 95% CI | n_tasks | verdict |",
                "|---|---|---|---|---|---|",
            ]
            for c in cis:
                rate = "n/a" if c["rate"] is None else f"{c['rate']:.3f}"
                ci = "n/a" if c["lo"] is None else f"[{c['lo']:.3f}, {c['hi']:.3f}]"
                verdict = (
                    "CAUSAL WIN" if (c["lo"] is not None and c["lo"] > 0.5) else "not established"
                )
                lines.append(
                    f"| {c['control']} | {c['treatment']} | {rate} | {ci} | "
                    f"{c['n_tasks']} | {verdict} |"
                )
    lines += [
        "",
        "## Cost-adjusted lift (advisory, never blocking)",
        "",
        "Nets the judged preference against the treatment arm's extra spend: "
        "lift_per_dollar = (preference - 0.5) / ($ mean treatment - $ mean "
        "control); lift_per_wall_minute divides by the wall-time delta in "
        "minutes.",
        "",
    ]
    ca_rows = cost_adjusted_rows(cis, overhead)
    if ca_rows:
        lines += [
            "| control | treatment | preference | lift_per_dollar | lift_per_wall_minute |",
            "|---|---|---|---|---|",
        ]
        for r in ca_rows:

            def lift(v):
                return "n/a (arm B not costlier)" if v is None else f"{v:.4f}"

            lines.append(
                f"| {r['control']} | {r['treatment']} | {r['preference']:.3f} | "
                f"{lift(r['lift_per_dollar'])} | {lift(r['lift_per_wall_minute'])} |"
            )
    else:
        lines.append("n/a (no judged preference)")
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

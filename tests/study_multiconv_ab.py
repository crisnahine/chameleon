"""Multi-convention migration A/B — the north-star campaign instrument.

Design locked before data (pre-registration discipline). The single-convention
A/B (study_migration_ab.py, published 2026-07-11) ended at parity: chameleon's
conventions.md-via-CLAUDE.md channel = a hand-written CLAUDE.md rule = 100%.
This campaign tests the MULTI-convention case, where the static baseline's real
weakness lives: humans do not write every rule down, and what they write goes
stale as migrations continue.

Per language (TypeScript, Ruby, Python) the fixture is a services directory in
a THREE-migration state — http helper, logger, and date formatting each have an
OLD module (majority: 5 files) and a NEW module (1 recent file), neutral names,
all three taught to chameleon. 10 new-service tasks per language (30 total),
identical prompts across arms; each produced file is scored deterministically
per convention (new form / old form / absent).

Arms:
  off          no guidance (plugin disabled, no CLAUDE.md)
  static_stale CLAUDE.md hand-lists ONLY the http rule (realistic staleness:
               the doc was written for the first migration; the later logger /
               date migrations were never documented), plugin disabled
  static_full  CLAUDE.md hand-lists all three rules (static's best case,
               unrealistically well-maintained), plugin disabled
  chameleon    the shipped architecture: .chameleon/conventions.md mirror
               @-imported from CLAUDE.md + hooks enforcing

Stats: per task, pairwise wins (chameleon vs each baseline; win=1 strictly
higher cell score, 0.5 tie, 0 lower) fed to the repo's OWN coded bar,
tests.effectiveness.stats.paired_bootstrap_ci — the claim requires lo > 0.5.
Deterministic conformance replaces the judge panel (stated substitution: more
objective on this outcome, same statistic).

Spawns real `claude -p` — costs money, local only, never CI. Usage:
    PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_multiconv_ab.py \\
        [n_tasks_per_lang=10] [model=sonnet] [arms=off,static_stale,static_full,chameleon] [langs=ts,rb,py]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from tests.effectiveness.stats import paired_bootstrap_ci
from tests.journey.harness.claude import spawn_claude

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import (
    _compute_repo_id,
    bootstrap_repo,
    get_archetype,
    teach_competing_import,
)

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
_ENTITIES = [
    "shipping",
    "billing",
    "inventory",
    "review",
    "referral",
    "alert",
    "export",
    "audit",
    "quota",
    "session",
]

# (concern, old module spec, new module spec, old callable, new callable)
_CONVENTIONS = {
    "ts": [
        ("http", "./http", "./httpClient", "apiGet", "httpGet"),
        ("log", "./legacyLog", "./logger", "logMsg", "logInfo"),
        ("date", "./dateUtil", "./dates", "fmtDate", "formatDate"),
    ],
    "rb": [
        ("http", "HttpUtil", "HttpClient", "HttpUtil.get", "HttpClient.get"),
        ("log", "LogUtil", "AppLogger", "LogUtil.info", "AppLogger.info"),
        ("date", "DateUtil", "DateFormat", "DateUtil.fmt", "DateFormat.fmt"),
    ],
    "py": [
        ("http", "app.lib.http", "app.lib.http_client", "api_get", "http_get"),
        ("log", "app.lib.log_util", "app.lib.app_logger", "log_msg", "log_info"),
        ("date", "app.lib.date_util", "app.lib.date_format", "fmt_date", "format_date"),
    ],
}


# ---------------------------------------------------------------- fixtures


def _ts_service(name: str, new_form: bool) -> str:
    if new_form:
        return (
            'import { httpGet } from "./httpClient";\n'
            'import { logInfo } from "./logger";\n'
            'import { formatDate } from "./dates";\n\n'
            f"export async function get{name.capitalize()}(id: string) {{\n"
            f'  logInfo("{name} lookup");\n'
            f"  const rec = await httpGet<{{ id: string; at: string }}>(`/{name}s/${{id}}`);\n"
            "  return { ...rec, at: formatDate(rec.at) };\n}\n"
        )
    return (
        'import { apiGet } from "./http";\n'
        'import { logMsg } from "./legacyLog";\n'
        'import { fmtDate } from "./dateUtil";\n\n'
        f"export async function get{name.capitalize()}(id: string) {{\n"
        f'  logMsg("{name} lookup");\n'
        f"  const rec = await apiGet<{{ id: string; at: string }}>(`/{name}s/${{id}}`);\n"
        "  return { ...rec, at: fmtDate(rec.at) };\n}\n"
    )


def _ts_helper(fn: str, extra: str = "") -> str:
    return f"export function {fn}(x: any): any {{ return x; }}\n{extra}"


def _rb_service(name: str, new_form: bool) -> str:
    cap = name.capitalize()
    if new_form:
        return (
            f"class {cap}Service\n"
            f"  def self.fetch(id)\n"
            f'    AppLogger.info("{name} lookup")\n'
            f'    rec = HttpClient.get("/{name}s/#{{id}}")\n'
            f"    rec.merge(at: DateFormat.fmt(rec[:at]))\n"
            f"  end\nend\n"
        )
    return (
        f"class {cap}Service\n"
        f"  def self.fetch(id)\n"
        f'    LogUtil.info("{name} lookup")\n'
        f'    rec = HttpUtil.get("/{name}s/#{{id}}")\n'
        f"    rec.merge(at: DateUtil.fmt(rec[:at]))\n"
        f"  end\nend\n"
    )


def _rb_helper(mod: str, meth: str) -> str:
    return f"module {mod}\n  def self.{meth}(*args)\n    args.first\n  end\nend\n"


def _py_service(name: str, new_form: bool) -> str:
    if new_form:
        return (
            "from app.lib.http_client import http_get\n"
            "from app.lib.app_logger import log_info\n"
            "from app.lib.date_format import format_date\n\n\n"
            f"def get_{name}(record_id: str) -> dict:\n"
            f'    log_info("{name} lookup")\n'
            f'    rec = http_get(f"/{name}s/{{record_id}}")\n'
            '    return {**rec, "at": format_date(rec["at"])}\n'
        )
    return (
        "from app.lib.http import api_get\n"
        "from app.lib.log_util import log_msg\n"
        "from app.lib.date_util import fmt_date\n\n\n"
        f"def get_{name}(record_id: str) -> dict:\n"
        f'    log_msg("{name} lookup")\n'
        f'    rec = api_get(f"/{name}s/{{record_id}}")\n'
        '    return {**rec, "at": fmt_date(rec["at"])}\n'
    )


def _py_helper(fn: str) -> str:
    return f"def {fn}(*args, **kwargs):\n    return args[0] if args else None\n"


_OLD_MAJORITY = ("user", "order", "product", "cart", "payment")
_NEW_MINORITY = "notification"


def build_fixture(lang: str, root: Path) -> tuple[Path, str]:
    """Build the 3-migration fixture. Returns (service_dir, new_file_rel_fmt)."""
    if lang == "ts":
        svc = root / "src" / "services"
        svc.mkdir(parents=True)
        (svc / "http.ts").write_text(_ts_helper("apiGet"))
        (svc / "httpClient.ts").write_text(_ts_helper("httpGet"))
        (svc / "legacyLog.ts").write_text(_ts_helper("logMsg"))
        (svc / "logger.ts").write_text(_ts_helper("logInfo"))
        (svc / "dateUtil.ts").write_text(_ts_helper("fmtDate"))
        (svc / "dates.ts").write_text(_ts_helper("formatDate"))
        for n in _OLD_MAJORITY:
            (svc / f"{n}Service.ts").write_text(_ts_service(n, new_form=False))
        (svc / f"{_NEW_MINORITY}Service.ts").write_text(_ts_service(_NEW_MINORITY, new_form=True))
        rel_fmt = "src/services/{entity}Service.ts"
    elif lang == "rb":
        svc = root / "app" / "services"
        lib = root / "app" / "lib"
        svc.mkdir(parents=True)
        lib.mkdir(parents=True)
        # language detection requires a Ruby signal (Gemfile / gemspec)
        (root / "Gemfile").write_text('source "https://rubygems.org"\n')
        (lib / "http_util.rb").write_text(_rb_helper("HttpUtil", "get"))
        (lib / "http_client.rb").write_text(_rb_helper("HttpClient", "get"))
        (lib / "log_util.rb").write_text(_rb_helper("LogUtil", "info"))
        (lib / "app_logger.rb").write_text(_rb_helper("AppLogger", "info"))
        (lib / "date_util.rb").write_text(_rb_helper("DateUtil", "fmt"))
        (lib / "date_format.rb").write_text(_rb_helper("DateFormat", "fmt"))
        for n in _OLD_MAJORITY:
            (svc / f"{n}_service.rb").write_text(_rb_service(n, new_form=False))
        (svc / f"{_NEW_MINORITY}_service.rb").write_text(_rb_service(_NEW_MINORITY, new_form=True))
        rel_fmt = "app/services/{entity}_service.rb"
    elif lang == "py":
        svc = root / "app" / "services"
        lib = root / "app" / "lib"
        svc.mkdir(parents=True)
        lib.mkdir(parents=True)
        (root / "app" / "__init__.py").write_text("")
        (svc / "__init__.py").write_text("")
        (lib / "__init__.py").write_text("")
        (lib / "http.py").write_text(_py_helper("api_get"))
        (lib / "http_client.py").write_text(_py_helper("http_get"))
        (lib / "log_util.py").write_text(_py_helper("log_msg"))
        (lib / "app_logger.py").write_text(_py_helper("log_info"))
        (lib / "date_util.py").write_text(_py_helper("fmt_date"))
        (lib / "date_format.py").write_text(_py_helper("format_date"))
        for n in _OLD_MAJORITY:
            (svc / f"{n}_service.py").write_text(_py_service(n, new_form=False))
        (svc / f"{_NEW_MINORITY}_service.py").write_text(_py_service(_NEW_MINORITY, new_form=True))
        rel_fmt = "app/services/{entity}_service.py"
    else:
        raise ValueError(lang)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=x", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=x",
            "commit",
            "-qm",
            "multi-migration fixture",
        ],
        check=True,
    )
    return svc, rel_fmt


def prepare_profile(lang: str, root: Path, sample_rel: str) -> None:
    bootstrap_repo(str(root))
    rid = _compute_repo_id(root)
    # trust FIRST: get_archetype is trust-gated and reports none on an
    # untrusted profile
    grant_trust(rid, root / ".chameleon")
    arch = (get_archetype(rid, str(root / sample_rel)).get("data") or {}).get("archetype")
    if not arch:
        raise RuntimeError(f"{lang}: sample service file resolved no archetype")
    for _, old, new, _, _ in _CONVENTIONS[lang]:
        r = teach_competing_import(rid, archetype=arch, preferred=new, over=old)
        st = (r.get("data") or {}).get("status")
        if st != "success":
            raise RuntimeError(f"{lang}: teach {old}->{new} failed: {r}")
    grant_trust(rid, root / ".chameleon")


# ---------------------------------------------------------------- arms

_STATIC_RULE_TEXT = {
    "ts": {
        "http": "- In `src/services`, import the HTTP helper from `./httpClient` "
        "(`httpGet`). Do NOT import from `./http`; it is being retired.",
        "log": "- In `src/services`, import logging from `./logger` (`logInfo`). "
        "Do NOT import from `./legacyLog`; it is being retired.",
        "date": "- In `src/services`, import date formatting from `./dates` "
        "(`formatDate`). Do NOT import from `./dateUtil`; it is being retired.",
    },
    "rb": {
        "http": "- In `app/services`, make HTTP calls via `HttpClient.get`. "
        "Do NOT use `HttpUtil`; it is being retired.",
        "log": "- In `app/services`, log via `AppLogger.info`. "
        "Do NOT use `LogUtil`; it is being retired.",
        "date": "- In `app/services`, format dates via `DateFormat.fmt`. "
        "Do NOT use `DateUtil`; it is being retired.",
    },
    "py": {
        "http": "- In `app/services`, import the HTTP helper from "
        "`app.lib.http_client` (`http_get`). Do NOT import from "
        "`app.lib.http`; it is being retired.",
        "log": "- In `app/services`, import logging from `app.lib.app_logger` "
        "(`log_info`). Do NOT import from `app.lib.log_util`; it is being retired.",
        "date": "- In `app/services`, import date formatting from "
        "`app.lib.date_format` (`format_date`). Do NOT import from "
        "`app.lib.date_util`; it is being retired.",
    },
}


def _write_arm_files(arm: str, lang: str, dest: Path) -> tuple[dict, Path | None]:
    rules = _STATIC_RULE_TEXT[lang]
    if arm == "off":
        return {"CHAMELEON_DISABLE": "1"}, None
    if arm == "static_stale":
        (dest / "CLAUDE.md").write_text("# Project conventions\n\n" + rules["http"] + "\n")
        return {"CHAMELEON_DISABLE": "1"}, None
    if arm == "static_full":
        (dest / "CLAUDE.md").write_text(
            "# Project conventions\n\n"
            + "\n".join(rules[k] for k in ("http", "log", "date"))
            + "\n"
        )
        return {"CHAMELEON_DISABLE": "1"}, None
    if arm == "chameleon":
        # the shipped architecture: the mirror was written by teach at profile
        # prep; CLAUDE.md carries only the @import line
        (dest / "CLAUDE.md").write_text("# Project notes\n\n@.chameleon/conventions.md\n")
        return {"CHAMELEON_ALLOW_TMP_REPO": "1"}, _PLUGIN
    if arm == "chameleon_local":
        # no-team-file variant: the repo's CLAUDE.md exists and is NOT ours to
        # touch; the @import pointer lives in user-local CLAUDE.local.md
        (dest / "CLAUDE.md").write_text("# Team file - not managed by chameleon\n")
        (dest / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n")
        return {"CHAMELEON_ALLOW_TMP_REPO": "1"}, _PLUGIN
    raise ValueError(arm)


# ---------------------------------------------------------------- scoring


def _score_file(lang: str, text: str) -> dict[str, float | None]:
    """Per convention: 1.0 new form, 0.0 old form, None absent (concern unused)."""
    out: dict[str, float | None] = {}
    for concern, old, new, _old_call, _new_call in _CONVENTIONS[lang]:
        if lang == "ts":
            has_new = bool(re.search(rf'from\s+["\']{re.escape(new)}["\']', text))
            has_old = bool(re.search(rf'from\s+["\']{re.escape(old)}["\']', text))
        elif lang == "rb":
            has_new = re.search(rf"\b{re.escape(new)}\b", text) is not None
            has_old = re.search(rf"\b{re.escape(old)}\b", text) is not None
        else:
            # boundary guard: "from app.lib.http" must not match
            # "from app.lib.http_client"
            has_new = re.search(rf"(from|import)\s+{re.escape(new)}(?![\w.])", text) is not None
            has_old = re.search(rf"(from|import)\s+{re.escape(old)}(?![\w.])", text) is not None
        if has_new and not has_old:
            out[concern] = 1.0
        elif has_old:
            out[concern] = 0.0
        else:
            out[concern] = None
    return out


def _cell_score(lang: str, dest: Path, rel: str) -> tuple[float, dict]:
    f = dest / rel
    if not f.is_file():
        return 0.0, {"missing_file": True}
    per = _score_file(lang, f.read_text(errors="replace"))
    vals = [v for v in per.values() if v is not None]
    if not vals:
        return 0.0, {"no_concern_used": True, **per}
    return sum(vals) / len(vals), per


# ---------------------------------------------------------------- driver


def _prompt(lang: str, entity: str) -> str:
    if lang == "ts":
        loc = f"src/services/{entity}Service.ts"
        fn = f"get{entity.capitalize()}Summary"
    elif lang == "rb":
        loc = f"app/services/{entity}_service.rb"
        fn = "fetch_summary"
    else:
        loc = f"app/services/{entity}_service.py"
        fn = f"get_{entity}_summary"
    return (
        f"Add a new file {loc}. It should provide {fn}, which fetches the "
        f"{entity} record for an id from the API endpoint /{entity}s/<id>, logs "
        f"one line about the lookup, and returns the record with its timestamp "
        f"field formatted for display. Follow the conventions used by the other "
        f"service files in this codebase."
    )


def run_cell(src: Path, work: Path, lang: str, arm: str, entity: str, model: str) -> dict:
    dest = work / f"{lang}_{arm}_{entity}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    rel = {
        "ts": f"src/services/{entity}Service.ts",
        "rb": f"app/services/{entity}_service.rb",
        "py": f"app/services/{entity}_service.py",
    }[lang]
    (dest / rel).unlink(missing_ok=True)
    grant_trust(_compute_repo_id(dest), dest / ".chameleon")
    env, plugin_root = _write_arm_files(arm, lang, dest)
    try:
        sess = spawn_claude(
            _prompt(lang, entity),
            dest,
            env,
            work / f"{lang}_{arm}_{entity}.jsonl",
            max_turns=15,
            permission_mode="bypassPermissions",
            model=model,
            plugin_root=plugin_root,
            disallowed_tools=["Agent", "Task", "Skill", "ScheduleWakeup", "Workflow"],
            timeout_s=300,
        )
        cost = getattr(sess, "cost_usd", 0.0)
    except Exception as e:
        return {
            "lang": lang,
            "arm": arm,
            "task": entity,
            "score": 0.0,
            "detail": {"spawn_error": str(e)},
            "cost": 0.0,
        }
    score, detail = _cell_score(lang, dest, rel)
    shutil.rmtree(dest, ignore_errors=True)
    return {
        "lang": lang,
        "arm": arm,
        "task": entity,
        "score": score,
        "detail": detail,
        "cost": cost,
    }


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    model = sys.argv[2] if len(sys.argv) > 2 else "sonnet"
    arms = (
        sys.argv[3].split(",")
        if len(sys.argv) > 3 and sys.argv[3] not in ("all",)
        else ["off", "static_stale", "static_full", "chameleon"]
    )
    langs = sys.argv[4].split(",") if len(sys.argv) > 4 else ["ts", "rb", "py"]
    tmp = Path(tempfile.mkdtemp(prefix="chameleon-mc-"))
    work = tmp / "work"
    work.mkdir()
    fixtures: dict[str, Path] = {}
    for lang in langs:
        src = tmp / f"src_{lang}"
        _svc, rel_fmt = build_fixture(lang, src)
        prepare_profile(lang, src, rel_fmt.format(entity=_OLD_MAJORITY[0]))
        fixtures[lang] = src
        print(f"[{lang}] fixture ready (3 migrations, taught, trusted)", file=sys.stderr)

    rows: list[dict] = []
    for lang in langs:
        for entity in _ENTITIES[:n]:
            for arm in arms:
                r = run_cell(fixtures[lang], work, lang, arm, entity, model)
                rows.append(r)
                print(
                    f"  {lang}/{entity}/{arm}: {r['score']:.2f}  (${r['cost']:.3f})",
                    file=sys.stderr,
                    flush=True,
                )

    # ---- summary + the coded bar
    def _arm_rows(arm):
        return [r for r in rows if r["arm"] == arm]

    print("\n=== per-arm mean conformance (0..1) ===", file=sys.stderr)
    for arm in arms:
        ar = _arm_rows(arm)
        by_lang = {lg: [r["score"] for r in ar if r["lang"] == lg] for lg in langs}
        overall = [r["score"] for r in ar]
        parts = "  ".join(f"{lg}={sum(v) / len(v):.2f}" for lg, v in by_lang.items() if v)
        print(
            f"  {arm:13} overall={sum(overall) / len(overall):.2f}  {parts}",
            file=sys.stderr,
        )

    result: dict = {"rows": rows, "bar": {}}
    if "chameleon" in arms:
        print("\n=== coded bar: paired_bootstrap_ci (win rate must clear 0.5) ===", file=sys.stderr)
        for base in [a for a in arms if a != "chameleon"]:
            wins: dict[str, list[float]] = {}
            for lang in langs:
                for entity in _ENTITIES[:n]:
                    a = next(
                        (
                            r
                            for r in rows
                            if r["arm"] == "chameleon" and r["lang"] == lang and r["task"] == entity
                        ),
                        None,
                    )
                    b = next(
                        (
                            r
                            for r in rows
                            if r["arm"] == base and r["lang"] == lang and r["task"] == entity
                        ),
                        None,
                    )
                    if a is None or b is None:
                        continue
                    w = (
                        1.0
                        if a["score"] > b["score"]
                        else (0.5 if a["score"] == b["score"] else 0.0)
                    )
                    wins[f"{lang}-{entity}"] = [w]
            ci = paired_bootstrap_ci(wins)
            result["bar"][f"chameleon_vs_{base}"] = ci
            met = ci["lo"] is not None and ci["lo"] > 0.5
            print(
                f"  chameleon vs {base:13}: rate={ci['rate']:.3f} "
                f"95% CI [{ci['lo']:.3f}, {ci['hi']:.3f}] n_tasks={ci['n_tasks']} "
                f"-> bar {'MET' if met else 'NOT met'}",
                file=sys.stderr,
            )
    print(f"\n  total cost: ${sum(r['cost'] for r in rows):.2f}", file=sys.stderr)
    print(f"  (fixtures under {tmp}; delete when done)", file=sys.stderr)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

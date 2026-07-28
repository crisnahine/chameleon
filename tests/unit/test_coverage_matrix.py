"""Unit tests for plugin/scripts/coverage_matrix.py.

The matrix answers one question -- does a rule the engine DECLARES for a
language show evidence of actually handling it -- and its whole value rests on
the two errors it must never make. A false `?` sends a reader to a construction
site that was fine, and enough of those train the reader to skim the column. A
false `Y` is worse: it certifies coverage nobody implemented, which is the
vacuous-silence bug the tool exists to catch, wearing the tool's own badge.

These tests pin both directions against synthetic modules, so a future change to
the walk cannot quietly widen or narrow it.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MATRIX = REPO / "plugin" / "scripts" / "coverage_matrix.py"


def _load():
    spec = importlib.util.spec_from_file_location("coverage_matrix", MATRIX)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coverage_matrix"] = mod
    spec.loader.exec_module(mod)
    return mod


cm = _load()


def _observed(tmp_path: Path, source: str) -> tuple[dict, dict]:
    """Run observed() over a synthetic engine laid out at the real paths."""
    for rel in cm.RULE_SOURCES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    (tmp_path / cm.RULE_SOURCES[0]).write_text(source, encoding="utf-8")
    found, agnostic, _ = cm.observed(tmp_path)
    return found, agnostic


# --- the anonymous else arm -------------------------------------------------


def test_else_arm_owns_the_complement(tmp_path):
    # `if ruby / elif python / else` states the third language by NOT stating
    # it, so a name-based scan sees two of three and stops satisfied.
    found, _ = _observed(
        tmp_path,
        """
def lint(content, language):
    if language == "ruby":
        specs = _RUBY_IMPORT_RE.findall(content)
    elif language == "python":
        specs = _PY_IMPORT_RE.findall(content)
    else:
        specs = _TS_IMPORT_RE.findall(content)
    if specs:
        out.append(Violation(rule="import-pref"))
""",
    )
    assert found["import-pref"] == {"ruby", "python", "typescript"}


def test_if_elif_without_else_credits_only_named_arms(tmp_path):
    # No default arm means no unnamed owner -- crediting a complement here
    # would invent coverage for a language nothing routes to.
    found, _ = _observed(
        tmp_path,
        """
def lint(content, language):
    if language == "ruby":
        specs = _RUBY_IMPORT_RE.findall(content)
    elif language == "python":
        specs = _PY_IMPORT_RE.findall(content)
    if specs:
        out.append(Violation(rule="import-pref"))
""",
    )
    assert found["import-pref"] == {"ruby", "python"}


# --- no inflation -----------------------------------------------------------


def test_sibling_arms_do_not_leak_into_a_named_arm(tmp_path):
    # A Ruby-only emission inside a function that also handles TS and Python
    # must stay Ruby-only. Widening the walk to the union broke exactly this.
    found, _ = _observed(
        tmp_path,
        """
def scan(content, language):
    if language == "typescript":
        a = _TS_SQL_RE.findall(content)
    if language == "python":
        b = _PY_SQL_RE.findall(content)
    if language == "ruby":
        for m in _RUBY_SQL_INTERP_RE.finditer(content):
            out.append(Violation(rule="sql-interp"))
""",
    )
    assert found["sql-interp"] == {"ruby"}


# --- a rule named by constant ------------------------------------------------


def test_rule_bound_to_a_module_constant_is_seen(tmp_path):
    # A module that names its rule once and passes the constant thereafter
    # exposes no rule="..." literal at all; keying on literals alone reported
    # every cell as `?` -- absence of a literal read as absence of a rule.
    found, _ = _observed(
        tmp_path,
        """
_RULE = "phantom-import"


def lint(content, language):
    if language == "ruby":
        out.append(Violation(rule=_RULE))
""",
    )
    assert "phantom-import" in found
    assert found["phantom-import"] == {"ruby"}


def test_non_string_constant_is_not_mistaken_for_a_rule_name(tmp_path):
    found, _ = _observed(
        tmp_path,
        """
_RULE = 42


def lint(content, language):
    if language == "ruby":
        out.append(Violation(rule=_RULE))
        out.append(Violation(rule="real-rule"))
""",
    )
    assert list(found) == ["real-rule"]


# --- language-agnostic by construction --------------------------------------


def test_rule_with_no_language_parameter_is_agnostic(tmp_path):
    # A credential token has no language. `scan_secrets(content, *, max_results)`
    # against its sibling `scan_dangerous_sinks(content, language=language)` is
    # the distinction: nothing on this path can discriminate, so "no
    # per-language evidence" is what correct looks like, not a gap.
    found, agnostic = _observed(
        tmp_path,
        """
def scan_secrets(content, *, max_results):
    for hit in _FALLBACK_PATTERNS.finditer(content):
        out.append(Violation(rule="secret-detected"))
""",
    )
    assert agnostic["secret-detected"] is True
    assert found["secret-detected"] == set()


def test_a_language_aware_site_is_not_agnostic(tmp_path):
    _, agnostic = _observed(
        tmp_path,
        """
def scan(content, language):
    if language == "ruby":
        out.append(Violation(rule="r"))
""",
    )
    assert agnostic["r"] is False


def test_agnostic_rule_renders_as_covered_not_unproven(tmp_path):
    # analyze() is what turns the flag into a rendered cell; an agnostic rule
    # declared language-independent must not burn a P0 read assignment.
    (tmp_path / "plugin/mcp/chameleon_mcp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin/mcp/chameleon_mcp/violation_class.py").write_text(
        'BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = {"secret-detected": None}\n',
        encoding="utf-8",
    )
    for rel in cm.RULE_SOURCES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    (tmp_path / cm.RULE_SOURCES[0]).write_text(
        """
def scan_secrets(content, *, max_results):
    out.append(Violation(rule="secret-detected"))
""",
        encoding="utf-8",
    )
    report = cm.analyze(tmp_path)
    row = next(r for r in report["rules"] if r["rule"] == "secret-detected")
    assert row["language_agnostic"] is True
    assert row["unproven"] == []
    assert row["severity"] is None


# --- unusable beats a clean-looking empty matrix -----------------------------


def test_missing_rule_source_raises_rather_than_reporting_clean(tmp_path):
    (tmp_path / "plugin/mcp/chameleon_mcp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin/mcp/chameleon_mcp/violation_class.py").write_text(
        "BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = {}\n", encoding="utf-8"
    )
    with pytest.raises(cm.Unusable):
        cm.observed(tmp_path)


def test_unparseable_source_raises(tmp_path):
    for rel in cm.RULE_SOURCES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    (tmp_path / cm.RULE_SOURCES[0]).write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(cm.Unusable):
        cm.observed(tmp_path)


# --- against the real engine -------------------------------------------------


def test_real_repo_matrix_has_no_unproven_declared_cell():
    # The three cells this suite was written for -- import-preference-violation,
    # secret-detected-in-content and phantom-import -- were each a tool blind
    # spot, not an engine gap. A `?` returning here means either the engine
    # really regressed or the walk lost a form; both are worth stopping for.
    report = cm.analyze(REPO)
    unproven = {
        r["rule"]: r["unjustified_missing"] for r in report["rules"] if r["unjustified_missing"]
    }
    assert unproven == {}


def test_real_repo_keeps_single_language_rules_narrow():
    report = cm.analyze(REPO)
    rows = {r["rule"]: set(r["implemented"]) for r in report["rules"]}
    assert rows["sql-string-interpolation"] == {"ruby"}
    assert rows["jsx-presence-mismatch"] == {"typescript"}
    assert rows["then-without-catch"] == {"typescript"}


# --- runs on the interpreter the docs actually invoke ------------------------


def test_runs_under_a_bare_system_python3():
    """Reading the declaration without importing chameleon is the point, so the
    script has to work on whatever `python3` resolves to -- 3.9 on macOS, not
    the plugin's 3.13 venv. This suite runs under the venv, so a 3.10+ construct
    (`zip(strict=)`, `match`, PEP 604 at runtime) passes every other test here
    and still breaks the documented `python3 plugin/scripts/coverage_matrix.py`.
    """
    python3 = shutil.which("python3")
    if not python3:
        pytest.skip("no system python3 on PATH")
    proc = subprocess.run(
        [python3, str(MATRIX), "--repo", str(REPO)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"{proc.returncode}: {proc.stderr[-800:]}"
    assert "Traceback" not in proc.stderr
    assert "declared" in proc.stdout


# --- the gap path itself ------------------------------------------------------


def _repo(tmp_path: Path, source: str, declaration: str) -> Path:
    """A synthetic tree carrying both halves of the matrix at their real paths."""
    (tmp_path / "plugin/mcp/chameleon_mcp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin/mcp/chameleon_mcp/violation_class.py").write_text(
        declaration, encoding="utf-8"
    )
    for rel in cm.RULE_SOURCES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    (tmp_path / cm.RULE_SOURCES[0]).write_text(source, encoding="utf-8")
    return tmp_path


_RUBY_ONLY = """
def scan(content, language):
    if language == "ruby":
        out.append(Violation(rule="sql-interp"))
"""

_DECLARES_TWO = (
    "BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = "
    '{"sql-interp": frozenset({"ruby", "python"})}\n'
)


def test_a_declared_language_with_no_evidence_is_reported(tmp_path):
    """The whole point of the tool, and the branch nothing else here executes.

    Every other case asserts an EMPTY gap list, which a report that structurally
    cannot populate one satisfies just as well as a correct clean matrix. Drive
    a real gap through analyze, render and the exit code so the clean assertions
    mean something.
    """
    root = _repo(tmp_path, _RUBY_ONLY, _DECLARES_TWO)

    report = cm.analyze(root)
    row = next(r for r in report["rules"] if r["rule"] == "sql-interp")
    assert row["implemented"] == ["ruby"]
    assert row["unjustified_missing"] == ["python"]
    assert row["severity"] == "P1"

    text = cm.render(report)
    assert "1 rule(s) declared for a language with NO evidence" in text
    assert "[P1] sql-interp: no evidence for python" in text

    assert cm.main(["--repo", str(root), "--strict"]) == cm.EXIT_FINDINGS


def test_a_gap_is_advisory_until_strict_is_passed(tmp_path):
    """Soft by default, like the conventions gate: a `?` is a read assignment,
    not a proven absence, so it must not fail a build unless the caller asks."""
    root = _repo(tmp_path, _RUBY_ONLY, _DECLARES_TWO)
    assert cm.main(["--repo", str(root)]) == cm.EXIT_OK


def test_a_justified_absence_is_not_counted_as_a_gap(tmp_path):
    """A construct the language does not have is coverage nobody owes. Pinning
    this alongside the gap case keeps the exemption from silently swallowing a
    real hole."""
    rule = next(iter(cm.JUSTIFIED_ABSENCE))
    excused = next(iter(cm.JUSTIFIED_ABSENCE[rule]))
    root = _repo(
        tmp_path,
        f"""
def scan(content, language):
    if language == "typescript":
        out.append(Violation(rule="{rule}"))
""",
        "BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = "
        f'{{"{rule}": frozenset({{"typescript", "{excused}"}})}}\n',
    )
    row = next(r for r in cm.analyze(root)["rules"] if r["rule"] == rule)
    assert row["unproven"] == [excused]
    assert row["unjustified_missing"] == []
    assert cm.main(["--repo", str(root), "--strict"]) == cm.EXIT_OK


def test_main_translates_unusable_into_exit_2(tmp_path):
    """A matrix that could not be built must never read as a matrix with no
    holes. observed() raising is only half the contract -- main has to turn it
    into 2, and 2 is not the code --strict uses for findings."""
    (tmp_path / "plugin/mcp/chameleon_mcp").mkdir(parents=True)
    (tmp_path / "plugin/mcp/chameleon_mcp/violation_class.py").write_text(
        'BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = {"x": None}\n', encoding="utf-8"
    )
    assert cm.main(["--repo", str(tmp_path), "--strict"]) == cm.EXIT_UNUSABLE


# --- the agnostic credit needs positive evidence ------------------------------


def test_a_deeply_nested_emission_is_not_credited_as_agnostic(tmp_path):
    """`A` on every language is the strongest claim the matrix makes, and it is
    inferred from the ABSENCE of a `language` parameter. Looking for that
    parameter inside the 4-hop evidence window made a construction nested four
    branches deep read as language-blind, so a rule whose own function takes a
    `language` rendered `A A A` with no gap and no severity -- the tool
    certifying coverage it never found, wearing its own badge.
    """
    root = _repo(
        tmp_path,
        """
def lint(content, language):
    if content:
        for item in content:
            while item:
                if item > 1:
                    out.append(Violation(rule="deep-rule"))
""",
        'BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = {"deep-rule": None}\n',
    )
    row = next(r for r in cm.analyze(root)["rules"] if r["rule"] == "deep-rule")
    assert row["language_agnostic"] is False
    assert row["unjustified_missing"] == list(sorted(cm.LANGUAGES))
    assert row["severity"] == "P0"

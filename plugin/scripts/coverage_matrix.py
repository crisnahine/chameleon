#!/usr/bin/env python3
"""Declared-vs-implemented rule coverage across chameleon's supported languages.

The gap this closes is chameleon's own founding invariant, turned on itself: a rule
that flags nothing because nobody implemented it for a language is indistinguishable,
from every consumer's side, from a rule that ran and found the file clean.

``violation_class.BLOCK_RULE_LANGUAGES`` exists precisely because that bug shipped once
(a Ruby profile certified ``jsx-presence-mismatch`` active at fp_rate 0.0 by flagging
nothing VACUOUSLY). This script generalizes that guard to every rule in the engine
rather than the eight block-eligible ones, and makes the check mechanical so a new
language branch cannot silently skip one.

DECLARED    what BLOCK_RULE_LANGUAGES says a rule covers (None == language-independent).
OBSERVED    which language a rule's construction site shows EVIDENCE of handling.

PRECISION -- read this before trusting a cell. The engine expresses language ownership
five different ways, and mixes them inside single functions:

    1. an `if language == "ruby":` guard              (scan_dangerous_sinks)
    2. selection of a language-prefixed constant      (_TS_THEN_RE vs _RUBY_SQL_INTERP_RE)
    3. dispatch to a per-language helper              (_python_naming_violations)
    4. an ANONYMOUS `else:` arm                       (ruby / elif python / else -> TS)
    5. no language at all, by construction            (scan_secrets: a credential
                                                       token has no language)

Forms 4 and 5 are why an evidence scan alone under-reports: an unnamed default arm
offers nothing to match, and a rule that never receives a `language` has nothing to
discriminate on. Both are handled here -- 4 by reading the dispatch chain's tests and
giving the unnamed arm the COMPLEMENT of what its siblings name, 5 by checking whether
any function on the emission path binds a `language` parameter at all.

The walk stays greedy on purpose: the first enclosing block with evidence wins, and only
that block may also contribute a complement. Widening to the union of all four hops does
resolve form 4, but it also credits a Ruby-only emission with the other languages its
enclosing function happens to handle -- trading a `?` that sends someone to read for a
`Y` that certifies coverage nobody wrote, which is the failure this tool exists to catch.

The residual over-report remains: a block referencing several languages' constants
credits them all, as `weak-hash` does. So this script is a NARROWING DEVICE, not an
oracle.

An unproven cell is therefore rendered `?` (no evidence found) and never `X`
(confirmed missing). Turning a `?` into a finding requires a human or an agent to read
the construction site. Reporting `?` as absence would commit the exact error the whole
tool exists to catch: mistaking "I did not see it" for "it is not there".

That mixing is itself the headline finding: BLOCK_RULE_LANGUAGES is hand-maintained,
and nothing in the repo verifies it against the implementation it describes.

Exit codes mirror gate.py deliberately -- a matrix that could not be built must never
be mistaken for a matrix with no holes.
    0  no divergence between DECLARED and OBSERVED
    1  divergence found (with --strict)
    2  could not run (source unparseable, expected symbols absent)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_FINDINGS, EXIT_UNUSABLE = 0, 1, 2

LANGUAGES = ("typescript", "ruby", "python")

# Every module that constructs a Violation. phantom_imports and prewrite_lint live
# OUTSIDE lint_engine deliberately (the former is the one lint that touches the
# filesystem, kept out to preserve lint_engine's no-I/O purity), so a scan of
# lint_engine alone reports their rules as universally missing -- a false P0.
RULE_SOURCES = (
    "plugin/mcp/chameleon_mcp/lint_engine.py",
    "plugin/mcp/chameleon_mcp/phantom_imports.py",
    "plugin/mcp/chameleon_mcp/prewrite_lint.py",
)

# The `_TS_` / `_RUBY_` / `_PY_` constant-naming convention is the most reliable
# ownership signal in the engine. Order matters: _PYTHON_ before _PY_.
LANG_PREFIXES = (
    ("_TS_", "typescript"),
    ("_RUBY_", "ruby"),
    ("_PYTHON_", "python"),
    ("_PY_", "python"),
)

# Rules whose absence for a language is correct by construction, with the reason.
# A rule may only live here if the CONSTRUCT does not exist in that language --
# not merely because nobody got to it. Anything else belongs in the report.
JUSTIFIED_ABSENCE: dict[str, dict[str, str]] = {
    "eval-call": {
        # This walk probes only LANGUAGES above, so a declared language outside
        # that tuple reads as unproven even when the engine really implements it.
        # PHP's bare dynamic-execution builtin IS detected (the C-family stripper
        # blanks its comments and strings first, and the sink then fires), it is
        # simply not probeable here until this matrix grows a PHP arm.
        "php": "declared and implemented, but this matrix probes only ts/ruby/python",
    },
    "then-without-catch": {
        "ruby": "no Promise.then equivalent; Ruby async is blocks/fibers",
        "python": "awaitables have no .then; the analogue is an un-awaited coroutine",
    },
    "jsx-presence-mismatch": {
        "ruby": "JSX is a TS/JS syntax extension",
        "python": "JSX is a TS/JS syntax extension",
    },
    "default-export-kind-mismatch": {
        "ruby": "no default-export concept",
        "python": "no default-export concept",
    },
    "named-export-count-bucket-mismatch": {
        "ruby": "no export statement",
        "python": "no export statement",
    },
}


class Unusable(Exception):
    """The matrix could not be built. Never reported as a clean matrix."""


def _module(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise Unusable(f"cannot parse {path}: {exc}") from exc


def declared(violation_class_py: Path) -> dict[str, frozenset[str] | None]:
    """Read BLOCK_RULE_LANGUAGES without importing chameleon (works on a broken install)."""
    tree = _module(violation_class_py)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        if not (isinstance(node.target, ast.Name) and node.target.id == "BLOCK_RULE_LANGUAGES"):
            continue
        out: dict[str, frozenset[str] | None] = {}
        if not isinstance(node.value, ast.Dict):
            raise Unusable("BLOCK_RULE_LANGUAGES is not a dict literal")
        # No `strict=`: that is 3.10+, and this script must run on a bare
        # `python3` (3.9 on macOS) -- reading the declaration without importing
        # chameleon is the whole point, so it cannot assume the plugin's venv.
        # ast.Dict guarantees len(keys) == len(values) anyway.
        for k, v in zip(node.value.keys, node.value.values):  # noqa: B905
            if not isinstance(k, ast.Constant):
                continue
            if isinstance(v, ast.Constant) and v.value is None:
                out[k.value] = None  # language-independent
            elif isinstance(v, ast.Call):  # frozenset({...})
                langs = {
                    e.value
                    for a in v.args
                    if isinstance(a, ast.Set)
                    for e in a.elts
                    if isinstance(e, ast.Constant)
                }
                out[k.value] = frozenset(langs)
        if not out:
            raise Unusable("BLOCK_RULE_LANGUAGES parsed empty")
        return out
    raise Unusable("BLOCK_RULE_LANGUAGES not found -- did violation_class.py move?")


def _block_langs(seg: str) -> set[str]:
    """Languages a source block shows evidence of handling."""
    out: set[str] = set()
    for prefix, lang in LANG_PREFIXES:
        if re.search(rf"\b{prefix}\w+", seg):
            out.add(lang)
    for g in re.findall(r'language\s*==\s*"(\w+)"', seg):
        if g in LANGUAGES:
            out.add(g)
    return out


def _rule_name_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "rule-name"`` bindings, so ``rule=_RULE`` resolves.

    A module that names its rule once and refers to the constant thereafter
    (``phantom_imports.py``) exposes no ``rule="..."`` literal anywhere, so a
    scan keyed on string constants alone sees zero construction sites and every
    cell for that rule renders `?` -- absence of a literal read as absence of an
    implementation, which is the one inference this tool exists to refuse.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not isinstance(getattr(node, "value", None), ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = node.value.value
    return out


def _binds_language(node: ast.AST) -> bool:
    """Whether a function signature takes a ``language`` parameter.

    A rule emitted from a call chain that never receives a language cannot be
    per-language by construction -- ``scan_secrets(content, *, max_results)``
    against its sibling ``scan_dangerous_sinks(content, language=language)``.
    Distinguishing the two is what separates "covers every language because a
    credential has none" from "covers only the language someone implemented".
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    a = node.args
    names = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return "language" in names


def _enclosing_functions(node: ast.AST, parents: dict) -> list[ast.AST]:
    """Every enclosing function, innermost first, with NO hop cap.

    Deliberately not reusing ``_enclosing_blocks``. That cap bounds the
    language-EVIDENCE scope, where widening credits a site with languages its
    enclosing function merely handles elsewhere. Whether anything on the
    emission path even RECEIVES a language is a different question, and
    answering it inside a truncated window makes a construction nested four
    branches deep read as language-blind. ``analyze`` then credits that rule
    with all three languages on the strength of a parameter nobody looked for --
    a false ``A``, which is the vacuous silence this whole tool exists to catch.
    """
    out: list[ast.AST] = []
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(cur)
    return out


def _enclosing_blocks(node: ast.AST, parents: dict) -> list[ast.AST]:
    """The <=4 nearest enclosing branch/loop/function blocks, innermost first."""
    out: list[ast.AST] = []
    cur = node
    while len(out) < 4:
        block = None
        walk = cur
        while walk in parents:
            par = parents[walk]
            if isinstance(par, (ast.If, ast.For, ast.While, ast.FunctionDef)):
                block = par
                break
            walk = par
        if block is None:
            break
        out.append(block)
        cur = block
    return out


_TEST_LANG_RE = re.compile(r"""language\s*==\s*['"](\w+)['"]""")


def _else_arm_languages(node: ast.AST, parents: dict) -> set[str]:
    """Languages owned by an anonymous ``else:`` closing a language dispatch chain.

    ``if language == "ruby": ... elif language == "python": ... else: <here>``
    is the shape that defeats every name-based signal: the third arm states its
    language by NOT stating it. Reading the chain's tests and taking the
    complement recovers the owner exactly, where widening the search window
    would instead credit the arm with its siblings' languages too.

    Returns empty unless the node really sits in such an arm, so a plain
    ``else:`` under a non-language test contributes nothing.
    """
    named: set[str] = set()
    in_else = False
    cur = node
    while cur in parents:
        par = parents[cur]
        if isinstance(par, ast.If):
            if any(cur is s for s in par.orelse):
                named |= set(_TEST_LANG_RE.findall(ast.unparse(par.test)))
                in_else = True
            elif any(cur is s for s in par.body):
                # Inside a named arm: that arm's own test owns this site.
                break
        cur = par
    if not in_else:
        return set()
    named &= set(LANGUAGES)
    if not named:
        return set()
    return set(LANGUAGES) - named


def _dispatch_complement(block: ast.AST) -> set[str]:
    """Languages a block's own ``language`` dispatch leaves to its ``else:``.

    The emission is often NOT lexically inside the arm that owns it: the arms
    compute (``import_specs``, ``over_used``) and a later sibling ``if`` raises
    the Violation. Reading the chain that the evidence-bearing block CONTAINS
    recovers the unnamed arm's language without widening the search window,
    which would credit this site with unrelated languages the enclosing
    function happens to handle elsewhere.

    Requires a real terminal ``else`` -- an ``if/elif`` with no default arm
    leaves nothing to attribute.
    """
    for node in ast.walk(block):
        if not isinstance(node, ast.If):
            continue
        named: set[str] = set()
        cur = node
        while True:
            named |= set(_TEST_LANG_RE.findall(ast.unparse(cur.test)))
            tail = cur.orelse
            if len(tail) == 1 and isinstance(tail[0], ast.If):
                cur = tail[0]
                continue
            break
        named &= set(LANGUAGES)
        # A terminal else that is not another elif is the anonymous arm.
        if named and cur.orelse:
            rest = set(LANGUAGES) - named
            if rest:
                return rest
    return set()


def observed(root: Path) -> tuple[dict[str, set[str]], dict[str, bool], list[str]]:
    """rule -> languages with EVIDENCE of handling, plus agnostic flags and sources.

    Walks outward from each Violation construction through up to four enclosing
    blocks, UNIONING the evidence found at each. It does not stop at the first
    block that yields any, because a partially-named dispatch chain
    (``if ruby / elif python / else``) puts two languages on the inner block and
    leaves the third in an anonymous ``else``, whose own prefixed constant sits
    a level further out -- halting early reported 2 of 3 and called it complete.
    The 4-hop cap, not the early exit, is what keeps a whole-function scope like
    ``scan_dangerous_sinks`` from marking every rule inside it fully covered.

    When the walk finds nothing, call edges are followed one level: a rule
    emitted from a shared module-level helper has its language dispatch in the
    CALLER, and a caller is not a parent, so the block walk can never reach it.

    The second return value records whether a rule is language-agnostic BY
    CONSTRUCTION -- no function on its emission path takes a ``language`` at
    all. For those, "no per-language evidence" is what a correct implementation
    looks like, not a gap.
    """
    found: dict[str, set[str]] = {}
    agnostic: dict[str, bool] = {}
    scanned: list[str] = []
    for rel in RULE_SOURCES:
        path = root / rel
        if not path.is_file():
            # A moved module is a broken assumption, not an empty result.
            raise Unusable(f"rule source missing: {rel}")
        scanned.append(rel)
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            raise Unusable(f"cannot parse {rel}: {exc}") from exc
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        consts = _rule_name_constants(tree)

        # func name -> the blocks enclosing each call to it, for the call-edge hop.
        call_sites: dict[str, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                call_sites.setdefault(node.func.id, []).append(node)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            rule = None
            for kw in node.keywords:
                if kw.arg != "rule":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    rule = kw.value.value
                elif isinstance(kw.value, ast.Name):
                    rule = consts.get(kw.value.id)
                break
            if not rule:
                continue

            blocks = _enclosing_blocks(node, parents)
            langs: set[str] = set()
            # Greedy: the FIRST enclosing block with evidence wins. Widening to
            # the union would credit a Ruby-only site with the other languages
            # its enclosing function also handles -- a false Y, which is the
            # vacuous-silence bug this tool exists to catch, not a near miss.
            for block in blocks:
                langs = _block_langs(ast.get_source_segment(src, block) or "")
                if langs:
                    # Only the block that supplied the evidence may also supply
                    # the complement; a wider scope reintroduces the inflation.
                    langs |= _dispatch_complement(block)
                    break
            langs |= _else_arm_languages(node, parents)
            enclosing_fns = _enclosing_functions(node, parents)
            takes_language = any(_binds_language(f) for f in enclosing_fns)

            if not langs:
                # The emitting helper is language-blind; its callers may not be.
                enclosing_fn = next(iter(enclosing_fns), None)
                if enclosing_fn is not None:
                    for call in call_sites.get(enclosing_fn.name, []):
                        caller_blocks = _enclosing_blocks(call, parents)
                        # Greedy here too: the innermost block enclosing the CALL
                        # is the dispatch context that decides which languages
                        # reach the helper. A call under `if language == "ruby":`
                        # is reached by ruby alone, whatever else its function
                        # handles further out.
                        for block in caller_blocks:
                            hit = _block_langs(ast.get_source_segment(src, block) or "")
                            if hit:
                                langs |= hit | _dispatch_complement(block)
                                break
                        takes_language = takes_language or any(
                            _binds_language(f) for f in _enclosing_functions(call, parents)
                        )

            found.setdefault(rule, set()).update(langs)
            # Agnostic only while EVERY site agrees; one language-aware site
            # means the rule does discriminate somewhere.
            agnostic[rule] = agnostic.get(rule, True) and not takes_language
    if not found:
        raise Unusable("no rule= constructions found -- did the engine move?")
    return found, agnostic, scanned


def analyze(root: Path) -> dict:
    vc = root / "plugin/mcp/chameleon_mcp/violation_class.py"
    if not vc.is_file():
        raise Unusable("missing plugin/mcp/chameleon_mcp/violation_class.py")

    dec = declared(vc)
    impl, agnostic, scanned = observed(root)
    rows = []
    for rule in sorted(set(dec) | set(impl)):
        have = impl.get(rule, set())
        want = dec.get(rule, "unlisted")
        expected = set(LANGUAGES) if want is None else (want if want != "unlisted" else None)
        # A rule no function on its emission path can discriminate by covers
        # every language it is reached for. Crediting it is not a guess: the
        # absence of a `language` parameter is a structural fact, and leaving
        # it `?` spends a P0 read assignment every run to re-derive the same
        # answer -- the noise that trains a reader to skim the whole column.
        by_construction = bool(agnostic.get(rule)) and not have
        if by_construction:
            have = set(LANGUAGES)
        # `?` not `X`: no evidence found is not evidence of absence.
        unproven = sorted((expected - have) if expected else [])
        justified = JUSTIFIED_ABSENCE.get(rule, {})
        real_gaps = [m for m in unproven if m not in justified]

        severity = None
        if real_gaps:
            # Declared language-independent + block-eligible + calibration-exempt is the
            # worst cell in the matrix: certified active, unmeasured, and silent.
            severity = "P0" if want is None else "P1"
        rows.append(
            {
                "rule": rule,
                "declared": "language-independent"
                if want is None
                else (sorted(want) if want != "unlisted" else "not-block-eligible"),
                "implemented": sorted(have),
                "language_agnostic": by_construction,
                "unproven": unproven,
                "unjustified_missing": real_gaps,
                "justified": {k: v for k, v in justified.items() if k in unproven},
                "severity": severity,
            }
        )
    return {
        "languages": list(LANGUAGES),
        "scanned": scanned,
        "precision": "evidence-based; ? != absent; A = language-agnostic by construction",
        "rules": rows,
    }


def render(report: dict) -> str:
    rows = report["rules"]
    out = [
        "rule                              declared              "
        + "  ".join(f"{lang[:2]:>3}" for lang in report["languages"])
        + "   gap"
    ]
    out.append("-" * 92)
    for r in rows:
        marks = []
        for lang in report["languages"]:
            if r.get("language_agnostic"):
                marks.append("  A")
            elif lang in r["implemented"]:
                marks.append("  Y")
            elif lang in r["justified"]:
                marks.append("  -")
            elif lang in r["unproven"]:
                marks.append("  ?")
            else:
                marks.append("  .")
        dec = r["declared"]
        dec = ",".join(dec)[:20] if isinstance(dec, list) else str(dec)[:20]
        flag = r["severity"] or ""
        out.append(f"{r['rule']:<34}{dec:<22}{''.join(marks)}   {flag}")
    gaps = [r for r in rows if r["unjustified_missing"]]
    out.append("")
    out.append(
        "Y evidence found   A language-agnostic by construction   "
        "? declared but NO evidence (READ IT -- not proven absent)   "
        "- justified   . not declared"
    )
    out.append("")
    if not gaps:
        out.append(
            "No declared/observed divergence. (Absence of evidence for a `.` cell "
            "is not checked -- only DECLARED rules are audited here.)"
        )
        return "\n".join(out)
    out.append(
        f"{len(gaps)} rule(s) declared for a language with NO evidence of handling "
        "-- each needs a read before it counts as a gap:"
    )
    for r in sorted(gaps, key=lambda r: (r["severity"] or "P9", r["rule"])):
        out.append(
            f"  [{r['severity']}] {r['rule']}: no evidence for "
            f"{', '.join(r['unjustified_missing'])}"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coverage_matrix")
    ap.add_argument("--repo", default=".", help="chameleon repo root")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 when gaps are found")
    args = ap.parse_args(argv)
    try:
        report = analyze(Path(args.repo).resolve())
    except Unusable as exc:
        print(f"coverage_matrix: could not run -- {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    print(json.dumps(report, indent=2) if args.json else render(report))
    gaps = any(r["unjustified_missing"] for r in report["rules"])
    return EXIT_FINDINGS if (gaps and args.strict) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

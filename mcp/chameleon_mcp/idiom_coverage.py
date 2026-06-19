"""Idiom coverage map + candidate novelty checking.

Support surface for /chameleon-auto-idiom. The skill derives NEW team idioms
from repo evidence; these helpers guarantee a derived candidate never lands in
idioms.md when chameleon already captures the same guidance somewhere else:

- an existing idiom (same slug, or near-identical text in active/deprecated)
- another candidate in the same batch (the model can repeat itself)
- an auto-derived principle (principles.md)
- a structured convention (competing imports, file-naming casing, inheritance
  bases in conventions.json)
- a lint/format rule (rules.json) — formatting guidance is never idiom-worthy

Everything here is read-only and fails open: a missing or corrupt artifact
skips that one check (reported in ``checks_skipped``) instead of crashing the
call or blocking the skill.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

# Caps mirror teach_profile_structured so a batch that passes here cannot fail
# the eventual write on size grounds.
MAX_CANDIDATES = 32
CANDIDATE_TEXT_CAP = 50 * 1024

# Similarity gate. Containment (overlap over the smaller token set) catches a
# reworded subset; Jaccard catches symmetric paraphrases. The minimum-basis
# guards keep tiny token sets (3 words can't establish sameness) from
# triggering either branch.
_CONTAINMENT_THRESHOLD = 0.7
_CONTAINMENT_MIN_BASIS = 4
_JACCARD_THRESHOLD = 0.5
_JACCARD_MIN_UNION = 8

_SUMMARY_CAP = 300

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Glue words plus the structural vocabulary of idiom blocks themselves and
# path-noise segments — none of these carry pattern identity, and leaving them
# in inflates similarity between unrelated idioms.
_STOPWORDS = frozenset(
    """
    the a an and or but so for of to in on at by with from as is are was were
    be been being it its this that these those they them we you i me my our
    your their not no never always must should shall could would can may might
    will use uses using used prefer preferred instead over rather all any each
    every both either neither some such only also just even still than then
    there here what when where which who whom why how do does did done doing
    go goes going gone get gets got through via per like into onto about
    because if else while during after before
    don t s d ll re ve m isn aren wasn weren
    example counterexample status active deprecated added language archetype
    reason import imports
    lib libs src app www dist index js ts tsx jsx rb
    """.split()
)

# casing-prefix -> spellings a candidate might use for the same convention
_CASING_SYNONYMS = {
    "kebab": ("kebab-case", "kebab case", "kebabcase"),
    "pascal": ("pascalcase", "pascal-case", "pascal case"),
    "camel": ("camelcase", "camel-case", "camel case"),
    "snake": ("snake_case", "snake-case", "snake case", "snakecase"),
}


def _casing_prefix(casing: str) -> str:
    """Reduce a stored casing label ('PascalCase', 'kebab-case') to the bare
    family key used in _CASING_SYNONYMS. The stored labels concatenate or
    hyphenate 'case', so splitting on separators alone leaves 'pascalcase'
    and the synonym lookup silently misses."""
    lowered = casing.lower()
    for sep in ("-", "_", " "):
        lowered = lowered.replace(sep, "")
    if lowered.endswith("case"):
        lowered = lowered[: -len("case")]
    return lowered


# An idiom restates the inheritance convention (which base to inherit from) only
# when it PRESCRIBES the base — not when it names a base class in a subordinate
# clause of an idiom about something else (a timeout, an index). Mirrors the
# file-naming-phrase gate: require an explicit "inherit/extend/subclass from the
# base" construction, not a bare mention.
_INHERITANCE_RULE_PHRASES = (
    "inherit from",
    "inherits from",
    "inheriting from",
    "must inherit",
    "should inherit",
    "must extend",
    "should extend",
    "always extend",
    "extend from",
    "extends from",
    "subclass of",
    "a subclass of",
    "descend from",
    "descends from",
    "base class is",
    "inherit the base",
)

# An idiom is about FILE naming (the convention chameleon derives) only when it
# says so explicitly. The bare word "file" is not enough — an idiom about an
# exported identifier's casing routinely mentions files in passing.
_FILE_NAMING_PHRASES = (
    "file name",
    "filename",
    "file-name",
    "file naming",
    "name the file",
    "files are named",
    "name files",
    "naming files",
    "file should be named",
    "file names",
    "files use",
    "name of the file",
)

# Formatting/lint topic words, matched as whole tokens (not substrings, so
# 'indent' no longer fires inside 'tree-row indentation depth'). A candidate is
# covered-by-lint only when formatting is its actual SUBJECT, measured by the
# share of meaningful tokens that are formatting words — a single passing
# mention of a linter does not reject a real architectural idiom.
_LINT_TOPIC_WORDS = frozenset(
    """
    indent indentation indents semicolon semicolons comma commas quote quotes
    tab tabs whitespace prettier rubocop eslint linter lint formatting format
    2-space 4-space two-space four-space
    """.split()
)
# Multi-word formatting phrases only — single words (incl. 'indentation') live
# in _LINT_TOPIC_WORDS, so listing them here too would double-count and wrongly
# flag a short architectural idiom that merely says 'indentation'.
_LINT_TOPIC_PHRASES = (
    "trailing comma",
    "line length",
    "max line",
    "quote style",
    "single quotes",
    "double quotes",
    "tabs vs spaces",
)
# Formatting must be at least this share of the rationale's meaningful tokens
# before the candidate is judged a restatement of the formatter config. Low
# enough to catch a terse formatting one-liner (2 of 6 meaningful tokens), high
# enough that a single passing mention of a linter in an architectural idiom
# stays novel.
_LINT_DOMINANCE = 0.30


def _stem(token: str) -> str:
    """Suffix-stripping stem so inflection changes (reconciled/reconcile,
    writes/writing, bypass/bypasses) can't evade the similarity gate.
    Consistency matters here, not linguistic correctness — both sides of a
    comparison go through the same mangling, so the only hard requirement
    is that inflection families co-stem (Porter-style step 1a for the
    s-family: sses->ss, ies->y, ss stays, s drops)."""
    if token.endswith("sses"):
        token = token[:-2]
    elif token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    elif token.endswith("ss"):
        pass
    elif token.endswith("s") and len(token) >= 4:
        token = token[:-1]
    for suffix, min_stem in (
        ("ing", 3),
        ("ed", 3),
        # -ly needs a longer stem: reply/supply/apply are not adverbs, and
        # "replies" already co-stems to "reply" via the ies-rule above.
        ("ly", 4),
    ):
        if token.endswith(suffix) and len(token) - len(suffix) >= min_stem:
            token = token[: -len(suffix)]
            break
    if token.endswith("e") and len(token) > 3:
        token = token[:-1]
    return token


def normalize_tokens(text: str) -> frozenset[str]:
    """Lowercased, stemmed word set with camelCase split, 1-char tokens and
    stopwords dropped. The unit of similarity comparison."""
    if not text:
        return frozenset()
    spaced = _CAMEL_SPLIT_RE.sub(" ", text)
    return frozenset(
        _stem(t) for t in _TOKEN_RE.findall(spaced.lower()) if len(t) > 1 and t not in _STOPWORDS
    )


def tokens_similar(
    a: frozenset[str],
    b: frozenset[str],
    *,
    containment: float = _CONTAINMENT_THRESHOLD,
    jaccard: float = _JACCARD_THRESHOLD,
) -> bool:
    """True when two token sets describe the same guidance."""
    if not a or not b:
        return False
    overlap = len(a & b)
    basis = min(len(a), len(b))
    if basis >= _CONTAINMENT_MIN_BASIS and overlap / basis >= containment:
        return True
    union = len(a | b)
    return union >= _JACCARD_MIN_UNION and overlap / union >= jaccard


# Idiom-vs-idiom dedup uses a more lenient containment than the generic
# similarity: a terse one-sentence restatement using the existing idiom's
# load-bearing symbols scores only ~0.5-0.6 of its (small) token set against the
# richer existing rationale. Measured on both real repos, genuinely-novel idioms
# top out at ~0.22 containment against any existing idiom, so 0.50 sits in a wide
# safe gap (>2x margin) — it catches terse restatements without flagging novel
# idioms. Kept separate so covered-by-principle precision is unaffected.
_IDIOM_DUP_CONTAINMENT = 0.50


def idioms_similar(a: frozenset[str], b: frozenset[str]) -> bool:
    """Duplicate-idiom similarity (rationale vs rationale)."""
    return tokens_similar(a, b, containment=_IDIOM_DUP_CONTAINMENT)


def parse_idiom_blocks(text: str) -> list[dict]:
    """Parse idioms.md into blocks: {slug, section, archetype, body, example,
    counterexample}.

    Tolerant of the bootstrap placeholder template and of hand-edited files;
    anything that isn't a ``### slug`` block under a known section header is
    ignored. Fence-aware: heading-looking lines inside ``` code blocks are
    example payload, not structure (mirrors the fence handling in teach's
    _escape_markdown_section_headings, which deliberately leaves ### lines
    in fenced examples untouched). ``example`` / ``counterexample`` capture
    the fenced code following the ``Example:`` / ``Counterexample:`` labels so
    a verbatim code clone can be matched code-against-code, not diluted into
    the whole body.
    """
    blocks: list[dict] = []
    section = "active"
    current: dict | None = None
    body_lines: list[str] = []
    rationale_lines: list[str] = []
    seen_code_label = False  # rationale ends at the first Example/Counterexample
    in_fence = False
    code_label: str | None = None  # "example" | "counterexample" | None
    code_lines: list[str] = []

    def _flush() -> None:
        nonlocal current, body_lines, rationale_lines, seen_code_label
        nonlocal code_label, code_lines
        if current is not None:
            current["body"] = "\n".join(body_lines).strip()
            current["rationale"] = "\n".join(rationale_lines).strip()
            blocks.append(current)
        current = None
        body_lines = []
        rationale_lines = []
        seen_code_label = False
        code_label = None
        code_lines = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            was_in_fence = in_fence
            in_fence = not in_fence
            if current is not None:
                body_lines.append(line)
                if was_in_fence and code_label:
                    # Closing fence: stash the captured code under its label.
                    current[code_label] = "\n".join(code_lines).strip()
                    code_label = None
                    code_lines = []
            continue
        if in_fence:
            if current is not None:
                body_lines.append(line)
                if code_label is not None:
                    code_lines.append(line)
            continue
        if stripped == "## active":
            _flush()
            section = "active"
            continue
        if stripped == "## deprecated":
            _flush()
            section = "deprecated"
            continue
        if stripped.startswith("### "):
            _flush()
            current = {
                "slug": stripped[4:].strip(),
                "section": section,
                "archetype": None,
                "source": None,
                "example": "",
                "counterexample": "",
            }
            continue
        if current is None:
            continue
        if stripped.startswith(("Language:", "Status:")):
            continue
        if stripped.startswith("Archetype:"):
            current["archetype"] = stripped.split(":", 1)[1].strip() or None
            continue
        if stripped.startswith("Source:"):
            # Provenance metadata, not part of the rationale prose; capture it and
            # keep it out of the rationale region like Language/Status/Archetype.
            current["source"] = stripped.split(":", 1)[1].strip() or None
            continue
        # The label line tells the NEXT fence which field to capture into, and
        # marks the end of the rationale region.
        if stripped == "Example:":
            code_label = "example"
            seen_code_label = True
        elif stripped == "Counterexample:":
            code_label = "counterexample"
            seen_code_label = True
        body_lines.append(line)
        if not seen_code_label:
            rationale_lines.append(line)
    _flush()
    return blocks


def extract_principle_lines(text: str) -> list[str]:
    """Numbered principles + anti-hallucination bullets from principles.md."""
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if re.match(r"^\d+\.\s+\S", stripped):
            lines.append(stripped.split(".", 1)[1].strip())
        elif stripped.startswith("- ") and len(stripped) > 2:
            lines.append(stripped[2:].strip())
    return lines


def _read_text(path: Path) -> str | None:
    """Capped, symlink-safe artifact read; None when absent or unreadable."""
    text, _status = _read_text_status(path)
    return text


def _read_text_status(path: Path) -> tuple[str | None, str]:
    """Capped, symlink-safe artifact read that distinguishes WHY a read failed.

    Returns (text, status) where status is "ok" (text is the content),
    "absent" (file genuinely not there — normal for idioms.md), or
    "unreadable" (present but over the 5MB cap, a directory, a symlink, or
    otherwise unreadable). The distinction lets a normally-optional artifact
    (idioms.md) stay silent when absent but still surface a degraded read.
    """
    from chameleon_mcp.safe_open import safe_read_profile_artifact

    try:
        return safe_read_profile_artifact(path), "ok"
    except FileNotFoundError:
        return None, "absent"
    except Exception:
        return None, "unreadable"


def _load_artifacts(profile_dir: Path) -> tuple[dict, list[str]]:
    """Best-effort load of every artifact the coverage/novelty logic uses.

    Returns ({idiom_blocks, principles, conventions, rules, archetypes,
    language}, checks_skipped). A missing idioms.md is normal (zero idioms);
    every other absent/corrupt artifact is reported in checks_skipped. An
    idioms.md that is PRESENT but unreadable (over-cap / directory / corrupt)
    is reported so the novelty gate never silently runs blind.
    """
    skipped: list[str] = []

    idioms_text, idioms_status = _read_text_status(profile_dir / "idioms.md")
    if idioms_status == "unreadable":
        skipped.append("idioms.md unreadable (over-cap, directory, or corrupt)")
    idiom_blocks = parse_idiom_blocks(idioms_text) if idioms_text else []
    # A readable idioms.md with real content but NO recognizable structure (no
    # section header, no blocks) was hand-replaced with non-idiom prose — the
    # dup check would silently run blind. The legitimate empty-profile
    # placeholder always carries a "## active" header, so it is not flagged.
    if (
        idioms_status == "ok"
        and idioms_text
        and idioms_text.strip()
        and not idiom_blocks
        and "## active" not in idioms_text
        and "## deprecated" not in idioms_text
    ):
        skipped.append("idioms.md present but unparseable (no idiom blocks or section headers)")

    principles_text = _read_text(profile_dir / "principles.md")
    if principles_text is None:
        principles: list[str] = []
        skipped.append("principles.md missing or unreadable")
    else:
        principles = extract_principle_lines(principles_text)

    conventions: dict = {}
    conv_text = _read_text(profile_dir / "conventions.json")
    if conv_text is None:
        skipped.append("conventions.json missing or unreadable")
    else:
        try:
            loaded = json.loads(conv_text)
            if isinstance(loaded, dict):
                conventions = loaded.get("conventions", {})
            else:
                skipped.append("conventions.json unreadable (not a JSON object)")
        except (json.JSONDecodeError, AttributeError):
            skipped.append("conventions.json unreadable (invalid JSON)")

    rules: dict = {}
    rules_text = _read_text(profile_dir / "rules.json")
    if rules_text is None:
        skipped.append("rules.json missing or unreadable")
    else:
        try:
            loaded = json.loads(rules_text)
            if isinstance(loaded, dict):
                rules = loaded.get("rules", {})
            else:
                skipped.append("rules.json unreadable (not a JSON object)")
        except (json.JSONDecodeError, AttributeError):
            skipped.append("rules.json unreadable (invalid JSON)")

    archetypes: list[str] = []
    arch_text = _read_text(profile_dir / "archetypes.json")
    if arch_text is None:
        skipped.append("archetypes.json missing or unreadable")
    else:
        try:
            loaded = json.loads(arch_text)
            if isinstance(loaded, dict):
                arch_map = loaded.get("archetypes", {})
                archetypes = sorted(arch_map) if isinstance(arch_map, dict) else []
            else:
                skipped.append("archetypes.json unreadable (not a JSON object)")
        except (json.JSONDecodeError, AttributeError):
            skipped.append("archetypes.json unreadable (invalid JSON)")

    language = "any"
    profile_text = _read_text(profile_dir / "profile.json")
    if profile_text is not None:
        try:
            loaded = json.loads(profile_text)
            if isinstance(loaded, dict):
                language = loaded.get("language", "any") or "any"
        except json.JSONDecodeError:
            skipped.append("profile.json unreadable (invalid JSON)")
    else:
        skipped.append("profile.json missing or unreadable")

    return (
        {
            "idiom_blocks": idiom_blocks,
            "principles": principles,
            "conventions": conventions if isinstance(conventions, dict) else {},
            "rules": rules if isinstance(rules, dict) else {},
            "archetypes": archetypes,
            "language": language,
        },
        skipped,
    )


def _competing_pairs(conventions: dict) -> list[dict]:
    pairs: list[dict] = []
    imports = conventions.get("imports", {})
    if not isinstance(imports, dict):
        return pairs
    for arch, data in imports.items():
        if not isinstance(data, dict):
            continue
        for pair in data.get("competing") or []:
            if isinstance(pair, dict) and pair.get("preferred") and pair.get("over"):
                pairs.append(
                    {
                        "archetype": arch,
                        "preferred": str(pair["preferred"]),
                        "over": str(pair["over"]),
                    }
                )
    return pairs


def _naming_casings(conventions: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    naming = conventions.get("naming", {})
    if not isinstance(naming, dict):
        return out
    for arch, data in naming.items():
        if not isinstance(data, dict):
            continue
        casing = (data.get("file_naming") or {}).get("casing")
        if isinstance(casing, str) and casing:
            out[arch] = casing
    return out


def _inheritance_bases(conventions: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    inheritance = conventions.get("inheritance", {})
    if not isinstance(inheritance, dict):
        return out
    for arch, data in inheritance.items():
        if not isinstance(data, dict):
            continue
        dominant = data.get("dominant_base")
        known = data.get("known_bases") or []
        if dominant or known:
            out[arch] = {
                "dominant_base": dominant,
                "known_bases": [str(b) for b in known if b],
            }
    return out


def _class_contract(conventions: dict) -> dict[str, dict]:
    """Per-archetype DSL/decorator/required-method contract, for the dedup map."""
    out: dict[str, dict] = {}
    cc = conventions.get("class_contract", {})
    if not isinstance(cc, dict):
        return out
    for arch, data in cc.items():
        if isinstance(data, dict) and (
            data.get("dsl_macros") or data.get("decorators") or data.get("required_methods")
        ):
            out[arch] = {
                "dsl_macros": [str(m) for m in data.get("dsl_macros") or []],
                "decorators": [str(d) for d in data.get("decorators") or []],
                "required_methods": [str(m) for m in data.get("required_methods") or []],
                "base": data.get("base"),
            }
    return out


def _sanitize(text: str) -> str:
    """Neutralize tag-boundary / bidi / control-char tokens before relaying
    profile-derived prose into model context — idioms.md and principles.md are
    attacker-controllable committed content, exactly the surface
    sanitize_for_chameleon_context defends. Mirrors every sibling read tool."""
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    return sanitize_for_chameleon_context(text)


def build_coverage(profile_dir: Path) -> tuple[dict, list[str]]:
    """Assemble the already-covered map the auto-idiom skill reads before
    drafting candidates. Returns (data, checks_skipped)."""
    artifacts, skipped = _load_artifacts(profile_dir)

    active: list[dict] = []
    deprecated: list[dict] = []
    for block in artifacts["idiom_blocks"]:
        entry = {
            "slug": _sanitize(block["slug"]),
            "archetype": block["archetype"],
            "summary": _sanitize(block["body"][:_SUMMARY_CAP]),
        }
        if block["section"] == "active":
            active.append(entry)
        else:
            deprecated.append({"slug": _sanitize(block["slug"])})

    conventions = artifacts["conventions"]
    import_preferences: dict[str, list[str]] = {}
    imports = conventions.get("imports", {})
    if isinstance(imports, dict):
        for arch, data in imports.items():
            if not isinstance(data, dict):
                continue
            modules = [
                str(p.get("module"))
                for p in data.get("preferred") or []
                if isinstance(p, dict) and p.get("module")
            ]
            if modules:
                import_preferences[arch] = modules

    error_handling: dict[str, str] = {}
    eh = conventions.get("error_handling", {})
    if isinstance(eh, dict):
        for arch, data in eh.items():
            if not isinstance(data, dict):
                continue
            if "rescues" in data:
                error_handling[arch] = "rescues"
            elif "try_catch" in data:
                error_handling[arch] = "try_catch"

    def _kind_nonempty(kind: str, body: object) -> bool:
        if not isinstance(body, dict):
            return False
        if kind == "layering":
            return bool(body.get("forbidden_upward_edges"))
        return any(bool(v) for v in body.values())

    convention_kinds = sorted(
        kind for kind, body in conventions.items() if _kind_nonempty(kind, body)
    )

    data = {
        "language": artifacts["language"],
        "existing_idioms": {
            "active": active,
            "active_count": len(active),
            "deprecated": deprecated,
        },
        "covered": {
            "principles": [_sanitize(p) for p in artifacts["principles"]],
            "import_preferences": import_preferences,
            "competing_imports": _competing_pairs(conventions),
            "naming": _naming_casings(conventions),
            "inheritance": _inheritance_bases(conventions),
            "class_contract": _class_contract(conventions),
            "error_handling": error_handling,
            "convention_kinds": convention_kinds,
            "lint_sources": sorted(artifacts["rules"]),
            "archetypes": artifacts["archetypes"],
        },
    }
    return data, skipped


def _candidate_text(candidate: dict) -> str:
    return " ".join(
        str(candidate.get(key) or "") for key in ("rationale", "example", "counterexample")
    )


def _is_formatting_idiom(rationale: str) -> bool:
    """True when formatting/lint config is the candidate's actual SUBJECT, not
    a passing mention. Measured by the share of the rationale's meaningful
    tokens that are formatting words (whole-token match, so 'indent' no longer
    fires inside 'indentation depth' in an architectural idiom)."""
    low = rationale.lower()
    phrase_hits = sum(1 for p in _LINT_TOPIC_PHRASES if p in low)
    word_tokens = [t for t in re.findall(r"[a-z0-9-]+", low) if len(t) > 2 and t not in _STOPWORDS]
    if not word_tokens:
        return False
    fmt_hits = sum(1 for t in word_tokens if t in _LINT_TOPIC_WORDS) + phrase_hits
    return fmt_hits / len(word_tokens) >= _LINT_DOMINANCE


_CONTRACT_VERB_PHRASES = (
    "must define",
    "should define",
    "must implement",
    "should implement",
    "typed filter",
)


def _mentions_contract(text_lower: str, contract: dict) -> bool:
    """True when the idiom text adds DSL/required-method contract content.

    A pure ``inherit from X`` idiom restates the inheritance convention and is
    covered. One that also names the base's DSL macros or required methods adds a
    contract the inheritance section never captures, so it stays novel.
    """
    if not contract:
        return any(p in text_lower for p in _CONTRACT_VERB_PHRASES)
    for token in (contract.get("dsl_macros") or []) + (contract.get("required_methods") or []):
        if token and token.lower() in text_lower:
            return True
    return any(p in text_lower for p in _CONTRACT_VERB_PHRASES)


def _covered_reasons(
    candidate: dict,
    text_lower: str,
    tokens: frozenset[str],
    artifacts: dict,
    principle_tokens: list[frozenset[str]],
) -> list[str]:
    """Deterministic restates-an-auto-derived-artifact checks.

    ``principle_tokens`` is the pre-tokenized principle set, computed once per
    batch by the caller — tokenizing it here per candidate is O(P x C) and
    blows up on a large principles.md.
    """
    reasons: list[str] = []

    for i, p_tokens in enumerate(principle_tokens, 1):
        if tokens_similar(tokens, p_tokens):
            reasons.append(f"covered-by-principle:{i}")

    conventions = artifacts["conventions"]

    for pair in _competing_pairs(conventions):
        if pair["preferred"].lower() in text_lower and pair["over"].lower() in text_lower:
            reasons.append(f"covered-by-competing-import:{pair['preferred']}-over-{pair['over']}")

    candidate_arch = candidate.get("archetype")
    # Only an idiom that is explicitly about FILE naming restates the
    # file-naming-casing convention. The bare word "file" is not enough — an
    # export-identifier-casing rule mentions files in passing.
    about_file_naming = any(p in text_lower for p in _FILE_NAMING_PHRASES)
    if about_file_naming:
        for arch, casing in _naming_casings(conventions).items():
            if candidate_arch and arch != candidate_arch:
                continue
            prefix = _casing_prefix(casing)
            synonyms = _CASING_SYNONYMS.get(prefix, (casing.lower(),))
            if any(s in text_lower for s in synonyms):
                reasons.append(f"covered-by-naming:{arch}")

    if any(p in text_lower for p in _INHERITANCE_RULE_PHRASES):
        contract = _class_contract(conventions)
        for arch, data in _inheritance_bases(conventions).items():
            if candidate_arch and arch != candidate_arch:
                continue
            bases = [data.get("dominant_base") or ""] + list(data.get("known_bases") or [])
            if any(b and b.lower() in text_lower for b in bases):
                # Suppress only a PURE "inherit from X" idiom; one that also names
                # the base's DSL macros or required methods stays novel.
                if _mentions_contract(text_lower, contract.get(arch, {})):
                    continue
                reasons.append(f"covered-by-inheritance:{arch}")

    rationale = str(candidate.get("rationale") or "")
    if artifacts["rules"] and _is_formatting_idiom(rationale):
        reasons.append("covered-by-lint-rules")

    return reasons


def check_candidates(profile_dir: Path, candidates: list) -> dict:
    """Judge a batch of idiom candidates for novelty.

    Returns {status, results, novel_count, checks_skipped}; results align
    1:1 with the input order. Verdict precedence per candidate:
    invalid > duplicate > covered > novel.
    """
    if not isinstance(candidates, list) or not candidates:
        return {
            "status": "failed",
            "error": "candidates must be a non-empty list of objects",
        }
    if len(candidates) > MAX_CANDIDATES:
        return {
            "status": "failed",
            "error": f"at most {MAX_CANDIDATES} candidates per call (got {len(candidates)})",
        }

    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE

    artifacts, skipped = _load_artifacts(profile_dir)

    # Pre-tokenize once per batch (not per candidate): block bodies AND
    # principle lines. Re-tokenizing a large principles.md per candidate is the
    # O(P x C) blowup the bounty surfaced.
    existing_slugs: dict[str, str] = {}
    existing_tokens: list[dict] = []
    for block in artifacts["idiom_blocks"]:
        existing_slugs.setdefault(block["slug"], block["section"])
        # Dedup compares GUIDANCE to guidance: the candidate's rationale against
        # the existing idiom's rationale (the prose before its Example block).
        # Using the rationale on BOTH sides — not the full body — means a novel
        # idiom whose example reuses the codebase's mandated boilerplate
        # (const/async/request/params, shared by every query idiom here) stays
        # novel, because the example code never enters the comparison. The
        # rationale keeps the load-bearing API symbols (invalidateQueries,
        # render_data, ApplicationService), so a genuine reword is still caught.
        # The rationale falls back to the full body for hand-written idioms that
        # have no Example block.
        rationale = block.get("rationale") or block["body"]
        existing_tokens.append(
            {
                "slug": block["slug"],
                "section": block["section"],
                "prose": normalize_tokens(rationale),
            }
        )
    principle_tokens = [normalize_tokens(p) for p in artifacts["principles"]]

    results: list[dict] = []
    accepted: list[tuple[str, frozenset[str]]] = []  # in-batch novel candidates
    accepted_slugs: set[str] = set()
    novel_count = 0

    for raw in candidates:
        if not isinstance(raw, dict):
            results.append(
                {
                    "slug": None,
                    "verdict": "invalid",
                    "reasons": ["not-an-object"],
                    "quality_warnings": [],
                }
            )
            continue

        slug = raw.get("slug")
        rationale = raw.get("rationale")
        archetype = raw.get("archetype")
        example = raw.get("example")
        counterexample = raw.get("counterexample")
        invalid_reasons: list[str] = []
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            invalid_reasons.append(f"slug-must-match-{SLUG_RE.pattern}")
        if not isinstance(rationale, str) or not rationale.strip():
            invalid_reasons.append("rationale-is-required")
        # Optional fields must match the shape teach_profile_structured enforces,
        # so a gate-blessed 'novel' candidate never fails (or crashes) the write.
        if archetype is not None and (
            not isinstance(archetype, str) or not ARCHETYPE_NAME_RE.match(archetype)
        ):
            invalid_reasons.append("archetype-must-be-a-name-string")
        if example is not None and not isinstance(example, str):
            invalid_reasons.append("example-must-be-a-string")
        if counterexample is not None and not isinstance(counterexample, str):
            invalid_reasons.append("counterexample-must-be-a-string")
        text = _candidate_text(raw)
        if len(text) > CANDIDATE_TEXT_CAP:
            invalid_reasons.append("exceeds-50kb-cap")
        if invalid_reasons or not isinstance(slug, str) or not isinstance(rationale, str):
            results.append(
                {
                    "slug": slug if isinstance(slug, str) else None,
                    "verdict": "invalid",
                    "reasons": invalid_reasons,
                    "quality_warnings": [],
                }
            )
            continue

        tokens = normalize_tokens(text)
        text_lower = text.lower()
        # Guidance tokens = the candidate's rationale, the dedup comparison unit.
        rationale_tokens = normalize_tokens(rationale)

        duplicate_reasons: list[str] = []
        if slug in existing_slugs:
            duplicate_reasons.append(f"slug-exists-in-{existing_slugs[slug]}")
        if slug in accepted_slugs:
            duplicate_reasons.append("duplicate-slug-in-batch")
        for blk in existing_tokens:
            if idioms_similar(rationale_tokens, blk["prose"]):
                tag = ":deprecated" if blk["section"] == "deprecated" else ""
                duplicate_reasons.append(f"similar-to-idiom:{_sanitize(blk['slug'])}{tag}")
        for other_slug, other_tokens in accepted:
            if idioms_similar(rationale_tokens, other_tokens):
                duplicate_reasons.append(f"similar-to-candidate:{other_slug}")

        covered_reasons = _covered_reasons(raw, text_lower, tokens, artifacts, principle_tokens)

        quality_warnings: list[str] = []
        if not str(raw.get("example") or "").strip():
            quality_warnings.append("missing-example")
        if not str(raw.get("counterexample") or "").strip():
            quality_warnings.append("missing-counterexample")
        if len(rationale.strip()) < 40:
            quality_warnings.append("short-rationale")

        if duplicate_reasons:
            verdict = "duplicate"
        elif covered_reasons:
            verdict = "covered"
        else:
            verdict = "novel"
            novel_count += 1
            accepted.append((slug, rationale_tokens))
            accepted_slugs.add(slug)

        results.append(
            {
                "slug": slug,
                "verdict": verdict,
                "reasons": duplicate_reasons + covered_reasons,
                "quality_warnings": quality_warnings,
            }
        )

    return {
        "status": "ok",
        "results": results,
        "novel_count": novel_count,
        "checks_skipped": skipped,
    }


def looks_like_idioms_markdown(text: str) -> bool:
    """Heuristic: is this idioms.md (markdown), not a profile JSON artifact?
    Used by the merge driver to route idioms.md through the union merge rather
    than the JSON merge. Profile artifacts are JSON objects (start with '{')."""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    # Tolerate a hand-edited header (`# Idioms`, any case) and the canonical
    # section markers; also accept a file carrying real `### slug` blocks even
    # without section headers, so a hand-maintained idioms.md still routes
    # through the union merge instead of falling into the JSON parser (which
    # makes the merge driver bail and git fall back to a raw markdown conflict).
    lowered = text.lower()
    if re.match(r"#\s*idioms\b", stripped, re.IGNORECASE):
        return True
    if "## active" in lowered or "## deprecated" in lowered:
        return True
    return re.search(r"(?m)^###\s+\S", text) is not None


def _parse_idioms_for_merge(text: str) -> dict[str, dict[str, str]]:
    """Parse idioms.md into {section: {slug: raw_block_text}} preserving the
    raw rendered block (so a merge re-emits each idiom byte-for-byte). Insertion
    order is preserved via the dict. Fence-aware so a `### slug` line inside an
    example is not mistaken for a real block boundary."""
    sections: dict[str, dict[str, str]] = {"active": {}, "deprecated": {}}
    section = "active"
    slug: str | None = None
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal slug, buf
        if slug is not None and section in sections:
            sections[section][slug] = "\n".join(buf).rstrip()
        slug = None
        buf = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if slug is not None:
                buf.append(line)
            continue
        if in_fence:
            if slug is not None:
                buf.append(line)
            continue
        if stripped == "## active":
            flush()
            section = "active"
            continue
        if stripped == "## deprecated":
            flush()
            section = "deprecated"
            continue
        if stripped.startswith("### "):
            flush()
            slug = stripped[4:].strip()
            buf = [line]
            continue
        if slug is not None:
            buf.append(line)
    flush()
    return sections


# Placeholder lines the empty-section template emits; never user content.
_IDIOM_PLACEHOLDERS = frozenset({"_(no idioms yet)_", "_(none)_"})


def _parse_loose_for_merge(text: str) -> dict[str, list[str]]:
    """Per-section lines that live OUTSIDE any ``### slug`` block.

    idioms.md is user-authored; hand-written bullets without a ``### slug``
    header are plausible and must survive a merge — silently dropping them
    is data loss. Fence-aware like the block parser; placeholders, blank
    lines, and the section/file headers themselves are not loose content.
    """
    loose: dict[str, list[str]] = {"active": [], "deprecated": []}
    section = "active"
    in_block = False
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if not in_block:
                loose[section].append(line)
            continue
        if in_fence:
            if not in_block:
                loose[section].append(line)
            continue
        if stripped == "## active":
            section = "active"
            in_block = False
            continue
        if stripped == "## deprecated":
            section = "deprecated"
            in_block = False
            continue
        if stripped.startswith("### "):
            in_block = True
            continue
        if in_block:
            continue
        if not stripped or stripped in _IDIOM_PLACEHOLDERS:
            continue
        if re.match(r"#\s*(team\s+)?idioms\b", stripped, re.IGNORECASE):
            continue
        loose[section].append(line)
    return loose


def merge_idioms_markdown(base_text: str, ours_text: str, theirs_text: str) -> str:
    """Three-way union merge for idioms.md, by slug, per section.

    git's built-in `merge=union` driver corrupts idioms.md because it
    line-unions fenced code blocks and unbalances the ``` fences. This merges
    structurally instead: union the ``### slug`` blocks per section, never
    losing an idiom either branch added. Order: base blocks first (in their
    original order), then blocks ours added, then blocks theirs added. For a
    slug present on both sides, ours wins (deterministic; the next
    /chameleon-auto-idiom run reconciles any redundancy).
    """
    base = _parse_idioms_for_merge(base_text)
    ours = _parse_idioms_for_merge(ours_text)
    theirs = _parse_idioms_for_merge(theirs_text)
    base_loose = _parse_loose_for_merge(base_text)
    ours_loose = _parse_loose_for_merge(ours_text)
    theirs_loose = _parse_loose_for_merge(theirs_text)

    out_parts: list[str] = ["# idioms", ""]
    for section in ("active", "deprecated"):
        out_parts.append(f"## {section}")
        out_parts.append("")
        # Hand-written content outside ### blocks unions first (same
        # never-lose rule as the slugs; line-deduped, order preserved).
        loose_merged: list[str] = []
        loose_seen: set[str] = set()
        for source_loose in (base_loose[section], ours_loose[section], theirs_loose[section]):
            for line in source_loose:
                if line not in loose_seen:
                    loose_seen.add(line)
                    loose_merged.append(line)
        if loose_merged:
            out_parts.extend(loose_merged)
            out_parts.append("")
        merged_order: list[str] = []
        seen: set[str] = set()
        for source in (base[section], ours[section], theirs[section]):
            for slug in source:
                if slug not in seen:
                    seen.add(slug)
                    merged_order.append(slug)
        if not merged_order:
            if not loose_merged:
                out_parts.append("_(none)_" if section == "deprecated" else "_(no idioms yet)_")
                out_parts.append("")
            continue
        for slug in merged_order:
            block = ours[section].get(slug) or theirs[section].get(slug) or base[section].get(slug)
            out_parts.append(block or f"### {slug}")
            out_parts.append("")
    return "\n".join(out_parts).rstrip() + "\n"

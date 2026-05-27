# Smart Injection v0.9.0 MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-derive import and naming conventions from the codebase at bootstrap time, inject them before every edit, and lint violations after every edit.

**Architecture:** Two new extractors (import frequency + naming patterns) run during bootstrap over existing ParsedFile data, producing a new `conventions.json` artifact. SessionStart injects a convention summary block. Tier 1 pointers echo top conventions. PostToolUse lint checks for import-preference and naming-convention violations.

**Tech Stack:** Python 3.11+, chameleon_mcp (FastMCP), existing ts_dump.mjs/prism_dump.rb extractors (no changes needed for MVP), pytest.

**Spec:** `docs/superpowers/specs/2026-05-27-smart-injection-v0.9.0-design.md`

---

### Task 1: conventions.json schema + profile integration

**Files:**
- Create: `mcp/chameleon_mcp/conventions.py`
- Modify: `mcp/chameleon_mcp/profile/loader.py:103-118` (LoadedProfile dataclass)
- Modify: `mcp/chameleon_mcp/profile/trust.py:132-139` (_HASHED_ARTIFACTS)
- Modify: `mcp/chameleon_mcp/bootstrap/transaction.py:38-47` (_PROTOCOL_FILES)
- Test: `tests/unit/test_conventions.py`

- [ ] **Step 1: Write failing test for conventions.json round-trip**

```python
# tests/unit/test_conventions.py
"""Unit tests for chameleon_mcp.conventions — schema, serialization, extraction."""
from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.conventions import (
    CONVENTIONS_SCHEMA_VERSION,
    empty_conventions,
    serialize_conventions,
)


class TestConventionsSchema:
    def test_empty_conventions_has_schema_version(self):
        c = empty_conventions(generation=42)
        assert c["schema_version"] == CONVENTIONS_SCHEMA_VERSION
        assert c["generation"] == 42
        assert c["conventions"]["imports"] == {}
        assert c["conventions"]["naming"] == {}

    def test_serialize_round_trip(self):
        c = empty_conventions(generation=1)
        c["conventions"]["imports"]["model"] = {
            "preferred": [{"module": "useCustomQuery", "source": "@/hooks", "frequency": 47, "total": 52}],
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        c["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = serialize_conventions(c)
        parsed = json.loads(text)
        assert parsed["conventions"]["imports"]["model"]["preferred"][0]["module"] == "useCustomQuery"
        assert parsed["conventions"]["naming"]["component"]["interface_prefix"]["consistency"] == 0.999
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'chameleon_mcp.conventions'`

- [ ] **Step 3: Implement conventions.py**

```python
# mcp/chameleon_mcp/conventions.py
"""Convention schema, serialization, and extraction for Smart Injection v0.9.0."""
from __future__ import annotations

import json

CONVENTIONS_SCHEMA_VERSION = 1
MIN_SAMPLE_SIZE = 10
MIN_SAMPLE_SIZE_NAMING = 5


def empty_conventions(*, generation: int) -> dict:
    return {
        "schema_version": CONVENTIONS_SCHEMA_VERSION,
        "generation": generation,
        "min_sample_size": MIN_SAMPLE_SIZE,
        "conventions": {
            "imports": {},
            "naming": {},
        },
    }


def serialize_conventions(conventions: dict) -> str:
    return json.dumps(conventions, indent=2, sort_keys=False, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py -v`
Expected: PASS

- [ ] **Step 5: Add conventions.json to trust hash**

In `mcp/chameleon_mcp/profile/trust.py`, add `"conventions.json"` to `_HASHED_ARTIFACTS` (alphabetical order, between `"canonicals.json"` and `"idioms.md"`):

```python
_HASHED_ARTIFACTS: tuple[str, ...] = (
    ".archetype_renames.json",
    "archetypes.json",
    "canonicals.json",
    "conventions.json",
    "idioms.md",
    "profile.json",
    "rules.json",
)
```

- [ ] **Step 6: Add conventions.json to transaction protocol files**

In `mcp/chameleon_mcp/bootstrap/transaction.py`, add `"conventions.json"` to `_PROTOCOL_FILES`:

```python
_PROTOCOL_FILES = frozenset({
    COMMITTED_SENTINEL,
    "profile.json",
    "archetypes.json",
    "canonicals.json",
    "conventions.json",
    "rules.json",
    "idioms.md",
    "profile.summary.md",
    "renames.json",
})
```

- [ ] **Step 7: Add conventions to LoadedProfile**

In `mcp/chameleon_mcp/profile/loader.py`, add `conventions: dict` field to `LoadedProfile` dataclass (after `rules`):

```python
@dataclass
class LoadedProfile:
    profile: dict
    archetypes: dict
    canonicals: dict
    rules: dict
    conventions: dict          # NEW: conventions.json contents (empty dict if absent)
    idioms_text: str
    generation: int
    profile_dir: Path
    mtime_token: str = ""
    archetype_names: list[str] = field(default_factory=list)
```

In `load_profile_dir()`, load conventions.json (fail-open to empty dict):

```python
# After reading rules.json, before reading idioms.md
conventions_path = profile_dir / "conventions.json"
conventions: dict = {}
if conventions_path.is_file():
    try:
        conventions = json.loads(
            safe_read_profile_artifact_text(conventions_path)
        )
    except (json.JSONDecodeError, OSError, UnsafeFileError):
        conventions = {}
```

Pass `conventions=conventions` to the LoadedProfile constructor.

- [ ] **Step 8: Write test for trust hash including conventions.json**

Add to `tests/unit/test_trust.py`:

```python
def test_conventions_json_changes_hash(self, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    repo_root = tmp_path / "repo"
    profile_dir = _make_profile_dir(repo_root)
    h1 = hash_profile(profile_dir)

    (profile_dir / "conventions.json").write_text(
        '{"schema_version": 1, "conventions": {}}', encoding="utf-8"
    )
    h2 = hash_profile(profile_dir)
    assert h1 != h2
```

- [ ] **Step 9: Run all tests**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ -v --tb=short`
Expected: All pass (existing 404 + new tests)

- [ ] **Step 10: Commit**

```bash
git add mcp/chameleon_mcp/conventions.py mcp/chameleon_mcp/profile/loader.py mcp/chameleon_mcp/profile/trust.py mcp/chameleon_mcp/bootstrap/transaction.py tests/unit/test_conventions.py tests/unit/test_trust.py
git commit -m "Add conventions.json schema, profile integration, and trust hash"
```

---

### Task 2: Import frequency extractor with competing detection

**Files:**
- Modify: `mcp/chameleon_mcp/conventions.py`
- Test: `tests/unit/test_conventions.py`

- [ ] **Step 1: Write failing test for import frequency extraction**

Add to `tests/unit/test_conventions.py`:

```python
from chameleon_mcp.conventions import extract_import_conventions
from chameleon_mcp.extractors._base import ParsedFile
from pathlib import Path


def _make_parsed_file(path: str, imports: list[tuple[str, str]]) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=tuple(imports),
        has_jsx=False,
    )


class TestImportFrequencyExtractor:
    def test_detects_preferred_import(self):
        files = [
            _make_parsed_file(f"src/hooks/use{i}.ts", [("@/lib/api", "named")]) for i in range(15)
        ]
        result = extract_import_conventions(files)
        preferred = [p["module"] for p in result.get("preferred", [])]
        assert "@/lib/api" in preferred

    def test_skips_below_min_sample_size(self):
        files = [_make_parsed_file(f"src/f{i}.ts", [("react", "named")]) for i in range(5)]
        result = extract_import_conventions(files)
        assert result == {"preferred": [], "competing": []}

    def test_detects_competing_imports(self):
        files = []
        for i in range(20):
            if i < 15:
                files.append(_make_parsed_file(f"src/h{i}.ts", [("useCustomQuery", "named")]))
            else:
                files.append(_make_parsed_file(f"src/u{i}.ts", [("somethingElse", "named")]))
        result = extract_import_conventions(files, competing_pairs=[("useCustomQuery", "useQuery")])
        competing = result.get("competing", [])
        assert len(competing) == 1
        assert competing[0]["preferred"] == "useCustomQuery"
        assert competing[0]["over"] == "useQuery"

    def test_excludes_framework_mandatory(self):
        files = [
            _make_parsed_file(f"src/f{i}.ts", [("react", "namespace"), ("@/lib/api", "named")])
            for i in range(20)
        ]
        result = extract_import_conventions(files)
        preferred_modules = [p["module"] for p in result.get("preferred", [])]
        assert "react" not in preferred_modules
        assert "@/lib/api" in preferred_modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestImportFrequencyExtractor -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement extract_import_conventions**

Add to `mcp/chameleon_mcp/conventions.py`:

```python
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.extractors._base import ParsedFile

# Framework imports present in >80% of files within an archetype are noise.
_FRAMEWORK_THRESHOLD = 0.80
# Minimum absolute occurrences to surface as "preferred."
_MIN_PREFERRED_COUNT = 10
# Minimum absolute occurrences for competing detection.
_MIN_COMPETING_COUNT = 5


def extract_import_conventions(
    files: list[ParsedFile],
    *,
    competing_pairs: list[tuple[str, str]] | None = None,
) -> dict:
    """Extract import frequency conventions from a cluster of ParsedFiles.

    Returns {"preferred": [...], "competing": [...]}.
    """
    if len(files) < MIN_SAMPLE_SIZE:
        return {"preferred": [], "competing": []}

    total = len(files)
    module_counts: Counter[str] = Counter()
    for f in files:
        seen_in_file: set[str] = set()
        for module, _kind in f.import_specifiers:
            if module not in seen_in_file:
                module_counts[module] += 1
                seen_in_file.add(module)

    # Competing detection runs on raw counts BEFORE filtering.
    competing: list[dict] = []
    if competing_pairs:
        for preferred_mod, over_mod in competing_pairs:
            p_count = module_counts.get(preferred_mod, 0)
            o_count = module_counts.get(over_mod, 0)
            if p_count >= _MIN_COMPETING_COUNT and o_count <= 2:
                competing.append({
                    "preferred": preferred_mod,
                    "over": over_mod,
                    "preferred_count": p_count,
                    "over_count": o_count,
                })
    else:
        # Auto-detect: find modules where a substring-match pair exists
        # with asymmetric counts (one dominant, one near-zero).
        sorted_modules = sorted(module_counts.keys())
        for i, mod_a in enumerate(sorted_modules):
            for mod_b in sorted_modules[i + 1:]:
                if _is_wrapper_pair(mod_a, mod_b):
                    a_count = module_counts[mod_a]
                    b_count = module_counts[mod_b]
                    if a_count >= _MIN_COMPETING_COUNT and b_count <= 2:
                        competing.append({
                            "preferred": mod_a, "over": mod_b,
                            "preferred_count": a_count, "over_count": b_count,
                        })
                    elif b_count >= _MIN_COMPETING_COUNT and a_count <= 2:
                        competing.append({
                            "preferred": mod_b, "over": mod_a,
                            "preferred_count": b_count, "over_count": a_count,
                        })

    # Preferred list: exclude framework-mandatory (>80% of this archetype)
    # and low-count (<10 occurrences).
    preferred: list[dict] = []
    for module, count in module_counts.most_common():
        if count / total > _FRAMEWORK_THRESHOLD:
            continue
        if count < _MIN_PREFERRED_COUNT:
            continue
        preferred.append({
            "module": module,
            "source": module,
            "frequency": count,
            "total": total,
        })

    return {"preferred": preferred, "competing": competing}


def _is_wrapper_pair(a: str, b: str) -> bool:
    """Heuristic: two modules are a wrapper pair if one's basename contains the other's."""
    base_a = a.rsplit("/", 1)[-1]
    base_b = b.rsplit("/", 1)[-1]
    return (
        len(base_a) > 3
        and len(base_b) > 3
        and (base_a in base_b or base_b in base_a)
        and base_a != base_b
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestImportFrequencyExtractor -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/chameleon_mcp/conventions.py tests/unit/test_conventions.py
git commit -m "Add import frequency extractor with competing detection"
```

---

### Task 3: Naming pattern extractor

**Files:**
- Modify: `mcp/chameleon_mcp/conventions.py`
- Test: `tests/unit/test_conventions.py`

- [ ] **Step 1: Write failing test for naming extractor**

Add to `tests/unit/test_conventions.py`:

```python
from chameleon_mcp.conventions import extract_naming_conventions


class TestNamingExtractor:
    def test_detects_interface_i_prefix(self):
        files = [
            _make_parsed_file(f"src/types/{i}.ts", [])
            for i in range(10)
        ]
        declarations = [
            "IUserProps", "IChartData", "IListingData", "IApiResponse",
            "ITableRow", "IFormValues", "IModalProps", "ISearchParams",
            "IFilterState", "IConfig",
        ]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert result["interface_prefix"]["pattern"] == "I"
        assert result["interface_prefix"]["consistency"] >= 0.95

    def test_no_prefix_when_inconsistent(self):
        declarations = ["IFoo", "Bar", "IBaz", "Qux", "Hello"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert "interface_prefix" not in result or result.get("interface_prefix", {}).get("consistency", 0) < 0.6

    def test_detects_type_t_prefix(self):
        declarations = ["TTheme", "TRoute", "TConfig", "TState", "TProps", "TData"]
        result = extract_naming_conventions(declarations={"type": declarations})
        assert result["type_prefix"]["pattern"] == "T"

    def test_skips_below_min_sample(self):
        declarations = ["IFoo", "IBar"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert result == {}

    def test_no_prefix_convention_for_bulletproof_style(self):
        declarations = ["UserProps", "ChartData", "ListingData", "ApiResponse", "TableRow", "FormValues"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert "interface_prefix" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestNamingExtractor -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement extract_naming_conventions**

Add to `mcp/chameleon_mcp/conventions.py`:

```python
import re

_PREFIX_RE = re.compile(r"^([A-Z])[a-z]")
_ENFORCE_THRESHOLD = 0.95
_STRONG_THRESHOLD = 0.60


def extract_naming_conventions(
    *,
    declarations: dict[str, list[str]],
) -> dict:
    """Extract naming prefix conventions from declaration names.

    Args:
        declarations: {"interface": ["IFoo", "IBar"], "type": ["TBaz"], "enum": ["EQux"]}

    Returns dict with keys like interface_prefix, type_prefix, enum_prefix.
    Each value: {"pattern": "I", "consistency": 0.999, "sample_size": N}
    Only included if consistency >= STRONG_THRESHOLD (0.60).
    """
    result: dict = {}
    type_to_key = {"interface": "interface_prefix", "type": "type_prefix", "enum": "enum_prefix"}

    for decl_type, names in declarations.items():
        if len(names) < MIN_SAMPLE_SIZE_NAMING:
            continue
        key = type_to_key.get(decl_type)
        if not key:
            continue

        prefix_counts: Counter[str] = Counter()
        for name in names:
            m = _PREFIX_RE.match(name)
            if m:
                prefix_counts[m.group(1)] += 1

        if not prefix_counts:
            continue

        most_common_prefix, count = prefix_counts.most_common(1)[0]
        consistency = count / len(names)
        if consistency >= _STRONG_THRESHOLD:
            result[key] = {
                "pattern": most_common_prefix,
                "consistency": round(consistency, 3),
                "sample_size": len(names),
            }

    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestNamingExtractor -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp/chameleon_mcp/conventions.py tests/unit/test_conventions.py
git commit -m "Add naming pattern extractor with prefix detection"
```

---

### Task 4: Wire extractors into bootstrap pipeline

**Files:**
- Modify: `mcp/chameleon_mcp/bootstrap/orchestrator.py`
- Modify: `mcp/chameleon_mcp/conventions.py` (add orchestration function)
- Test: `tests/unit/test_conventions.py`

- [ ] **Step 1: Write failing test for convention orchestration**

Add to `tests/unit/test_conventions.py`:

```python
from chameleon_mcp.conventions import extract_all_conventions


class TestExtractAllConventions:
    def test_produces_conventions_dict(self):
        files_by_archetype = {
            "component": [
                _make_parsed_file(f"src/c{i}.tsx", [("react", "namespace"), ("@/hooks/useCustomQuery", "named")])
                for i in range(15)
            ],
        }
        declarations_by_archetype = {
            "component": {"interface": [f"I{chr(65+i)}Props" for i in range(10)]},
        }
        result = extract_all_conventions(
            files_by_archetype=files_by_archetype,
            declarations_by_archetype=declarations_by_archetype,
            generation=42,
        )
        assert result["schema_version"] == CONVENTIONS_SCHEMA_VERSION
        assert result["generation"] == 42
        assert "component" in result["conventions"]["imports"]
        assert "component" in result["conventions"]["naming"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestExtractAllConventions -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement extract_all_conventions**

Add to `mcp/chameleon_mcp/conventions.py`:

```python
def extract_all_conventions(
    *,
    files_by_archetype: dict[str, list[ParsedFile]],
    declarations_by_archetype: dict[str, dict[str, list[str]]],
    generation: int,
) -> dict:
    """Run all convention extractors and produce a conventions.json dict."""
    conventions = empty_conventions(generation=generation)

    for archetype, files in files_by_archetype.items():
        import_conv = extract_import_conventions(files)
        if import_conv["preferred"] or import_conv["competing"]:
            conventions["conventions"]["imports"][archetype] = import_conv

    for archetype, declarations in declarations_by_archetype.items():
        naming_conv = extract_naming_conventions(declarations=declarations)
        if naming_conv:
            conventions["conventions"]["naming"][archetype] = naming_conv

    return conventions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestExtractAllConventions -v`
Expected: PASS

- [ ] **Step 5: Wire into bootstrap orchestrator**

In `mcp/chameleon_mcp/bootstrap/orchestrator.py`, find the section after canonical selection and before the atomic profile write. Add convention extraction there.

The orchestrator needs to:
1. Group ParsedFile objects by their assigned archetype cluster
2. Extract declaration names from each file's `top_level_node_kinds` (interfaces are `InterfaceDeclaration` nodes in TS)
3. Call `extract_all_conventions()`
4. Write `conventions.json` in the atomic transaction block

The exact insertion point and code depends on the orchestrator's internal structure. The implementer should search for where `canonicals.json` is written in the txn block and add `conventions.json` alongside it using `serialize_conventions()`.

- [ ] **Step 6: Run full test suite**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ tests/journey/harness/tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add mcp/chameleon_mcp/conventions.py mcp/chameleon_mcp/bootstrap/orchestrator.py tests/unit/test_conventions.py
git commit -m "Wire convention extractors into bootstrap pipeline"
```

---

### Task 5: SessionStart convention injection

**Files:**
- Modify: `mcp/chameleon_mcp/hook_helper.py` (session_start function)
- Modify: `mcp/chameleon_mcp/conventions.py` (add formatting function)
- Test: `tests/unit/test_conventions.py`

- [ ] **Step 1: Write failing test for convention formatting**

Add to `tests/unit/test_conventions.py`:

```python
from chameleon_mcp.conventions import format_conventions_for_session


class TestFormatConventionsForSession:
    def test_formats_import_competing(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["component"] = {
            "preferred": [],
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        text = format_conventions_for_session(conventions)
        assert "useCustomQuery" in text
        assert "not useQuery" in text
        assert "Follow these" in text

    def test_formats_naming_enforced(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = format_conventions_for_session(conventions)
        assert "I" in text
        assert "interface" in text.lower()

    def test_empty_conventions_returns_empty(self):
        conventions = empty_conventions(generation=1)
        text = format_conventions_for_session(conventions)
        assert text == ""

    def test_skips_below_60_percent(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["component"] = {
            "enum_prefix": {"pattern": "E", "consistency": 0.55, "sample_size": 8},
        }
        text = format_conventions_for_session(conventions)
        assert text == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestFormatConventionsForSession -v`
Expected: FAIL

- [ ] **Step 3: Implement format_conventions_for_session**

Add to `mcp/chameleon_mcp/conventions.py`:

```python
def format_conventions_for_session(conventions: dict) -> str:
    """Format conventions for SessionStart injection block.

    Uses imperative framing for >=95% consistency, context framing for 60-95%.
    Skips conventions below 60%.
    """
    lines: list[str] = []
    conv = conventions.get("conventions", {})

    # Import conventions (cross-archetype: collect all competing pairs)
    import_lines: list[str] = []
    seen_competing: set[str] = set()
    for _arch, data in conv.get("imports", {}).items():
        for c in data.get("competing", []):
            key = f"{c['preferred']}>{c['over']}"
            if key not in seen_competing:
                seen_competing.add(key)
                import_lines.append(f"- Use {c['preferred']}, not {c['over']}")

    # Naming conventions (collect unique patterns across archetypes)
    naming_lines: list[str] = []
    seen_naming: set[str] = set()
    for _arch, data in conv.get("naming", {}).items():
        for key in ("interface_prefix", "type_prefix", "enum_prefix"):
            entry = data.get(key)
            if not entry or key in seen_naming:
                continue
            consistency = entry.get("consistency", 0)
            if consistency < _STRONG_THRESHOLD:
                continue
            seen_naming.add(key)
            type_name = key.replace("_prefix", "").replace("_", " ")
            pattern = entry["pattern"]
            pct = f"{consistency:.0%}"
            if consistency >= _ENFORCE_THRESHOLD:
                naming_lines.append(f"- Prefix {type_name}s with {pattern} ({pct}, enforced)")
            else:
                naming_lines.append(f"- Prefix {type_name}s with {pattern} ({pct})")

    if not import_lines and not naming_lines:
        return ""

    lines.append("<chameleon-conventions>")
    lines.append("Follow these on every edit. Auto-derived from this codebase.")
    lines.append("")
    if import_lines:
        lines.append("IMPORTS (enforce):")
        lines.extend(import_lines)
        lines.append("")
    if naming_lines:
        lines.append("NAMING:")
        lines.extend(naming_lines)
        lines.append("")
    lines.append("</chameleon-conventions>")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestFormatConventionsForSession -v`
Expected: PASS

- [ ] **Step 5: Wire into session_start**

In `mcp/chameleon_mcp/hook_helper.py`, in the `session_start()` function, after the skill_content is read and before the drift_banner check (around line 651-658), add conventions loading and injection:

```python
    # Convention injection (v0.9.0)
    conventions_block = ""
    try:
        from chameleon_mcp.conventions import format_conventions_for_session
        from chameleon_mcp.profile.loader import find_repo_root, load_profile_dir

        conv_root = find_repo_root(Path.cwd())
        if conv_root and (conv_root / ".chameleon" / "conventions.json").is_file():
            loaded = load_profile_dir(conv_root / ".chameleon")
            conventions_block = format_conventions_for_session(loaded.conventions)
    except Exception:
        pass

    wrapped_parts = [
        "<chameleon-context>",
        # ... existing skill content ...
    ]
    if conventions_block:
        wrapped_parts.append("")
        wrapped_parts.append(conventions_block)
    # ... rest of wrapped_parts assembly ...
```

The implementer should insert the conventions_block into the wrapped_parts list after the skill content and before the drift banner, matching the existing pattern.

- [ ] **Step 6: Run full test suite**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ tests/journey/harness/tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add mcp/chameleon_mcp/conventions.py mcp/chameleon_mcp/hook_helper.py tests/unit/test_conventions.py
git commit -m "Add SessionStart convention injection with imperative framing"
```

---

### Task 6: Tier 1 convention echo in PreToolUse

**Files:**
- Modify: `mcp/chameleon_mcp/hook_helper.py` (preflight_and_advise Tier 1 block)
- Modify: `mcp/chameleon_mcp/conventions.py` (add compact formatter)
- Test: `tests/unit/test_conventions.py`

- [ ] **Step 1: Write failing test for compact convention echo**

Add to `tests/unit/test_conventions.py`:

```python
from chameleon_mcp.conventions import format_conventions_echo


class TestFormatConventionsEcho:
    def test_compact_echo(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["hook"] = {
            "preferred": [],
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        conventions["conventions"]["naming"]["hook"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 100},
        }
        text = format_conventions_echo(conventions, archetype="hook")
        assert "useCustomQuery" in text
        assert "I-prefix" in text
        assert len(text) < 200

    def test_empty_returns_empty(self):
        conventions = empty_conventions(generation=1)
        text = format_conventions_echo(conventions, archetype="hook")
        assert text == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestFormatConventionsEcho -v`
Expected: FAIL

- [ ] **Step 3: Implement format_conventions_echo**

Add to `mcp/chameleon_mcp/conventions.py`:

```python
def format_conventions_echo(conventions: dict, *, archetype: str) -> str:
    """Compact one-line convention echo for Tier 1 PreToolUse pointer.

    Returns something like: "Imports: useCustomQuery. Naming: I-prefix."
    Max ~30 tokens. Empty string if no conventions for this archetype.
    """
    parts: list[str] = []
    conv = conventions.get("conventions", {})

    # Import competing for this archetype
    arch_imports = conv.get("imports", {}).get(archetype, {})
    for c in arch_imports.get("competing", [])[:2]:
        parts.append(f"Imports: {c['preferred']}")

    # Naming for this archetype
    arch_naming = conv.get("naming", {}).get(archetype, {})
    for key in ("interface_prefix", "type_prefix"):
        entry = arch_naming.get(key)
        if entry and entry.get("consistency", 0) >= _STRONG_THRESHOLD:
            parts.append(f"Naming: {entry['pattern']}-prefix")
            break

    return ". ".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestFormatConventionsEcho -v`
Expected: PASS

- [ ] **Step 5: Wire into Tier 1 pointer in preflight_and_advise**

In `mcp/chameleon_mcp/hook_helper.py`, find the Tier 1 pointer assembly (the lightweight ~50 token block). After the archetype summary line, append the convention echo:

```python
# In the Tier 1 block, after summary_text:
conv_echo = ""
try:
    from chameleon_mcp.conventions import format_conventions_echo
    if loaded_conventions:
        conv_echo = format_conventions_echo(loaded_conventions, archetype=archetype_name)
except Exception:
    pass

# Append to the Tier 1 context block:
if conv_echo:
    tier1_parts.append(conv_echo)
```

The implementer should load conventions from the profile (same `loaded` object from get_pattern_context) and call `format_conventions_echo` with the current archetype name.

- [ ] **Step 6: Run full test suite**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ tests/journey/harness/tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add mcp/chameleon_mcp/conventions.py mcp/chameleon_mcp/hook_helper.py tests/unit/test_conventions.py
git commit -m "Add Tier 1 convention echo to counter attention decay"
```

---

### Task 7: PostToolUse convention lint rules

**Files:**
- Modify: `mcp/chameleon_mcp/lint_engine.py`
- Modify: `mcp/chameleon_mcp/tools.py` (lint_file function)
- Test: `tests/unit/test_lint_engine.py`

- [ ] **Step 1: Write failing tests for convention lint**

Add to `tests/unit/test_lint_engine.py`:

```python
from chameleon_mcp.lint_engine import lint_conventions


class TestConventionLint:
    def test_import_preference_violation(self):
        content = 'import { useQuery } from "@tanstack/react-query";\n'
        conventions = {
            "imports": {
                "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "import-preference-violation"
        assert "useCustomQuery" in violations[0].message

    def test_no_violation_when_correct_import(self):
        content = 'import { useCustomQuery } from "@/hooks/useCustomQuery";\n'
        conventions = {
            "imports": {
                "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_naming_convention_violation(self):
        content = 'interface UserProps {\n  name: string;\n}\n'
        conventions = {
            "naming": {
                "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "naming-convention-violation"

    def test_no_naming_violation_with_correct_prefix(self):
        content = 'interface IUserProps {\n  name: string;\n}\n'
        conventions = {
            "naming": {
                "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_chameleon_ignore_suppresses_rule(self):
        content = '// chameleon-ignore import-preference\nimport { useQuery } from "@tanstack/react-query";\n'
        conventions = {
            "imports": {
                "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_ruby_no_ts_naming_violations(self):
        content = "class User < ApplicationRecord\nend\n"
        conventions = {
            "naming": {
                "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 100},
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_lint_engine.py::TestConventionLint -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement lint_conventions**

Add to `mcp/chameleon_mcp/lint_engine.py`:

```python
import re as _re

_CHAMELEON_IGNORE_RE = _re.compile(r"//\s*chameleon-ignore\s+([\w-]+)")
_TS_IMPORT_RE = _re.compile(r"import\s+.*?\bfrom\s+['\"]([^'\"]+)['\"]", _re.MULTILINE)
_TS_INTERFACE_RE = _re.compile(r"\binterface\s+([A-Z]\w*)")


def lint_conventions(
    content: str,
    conventions: dict,
    *,
    language: str | None = None,
) -> list[Violation]:
    """Check file content against convention rules. Returns violations."""
    if not conventions:
        return []

    # Collect chameleon-ignore directives
    ignored_rules: set[str] = set()
    for m in _CHAMELEON_IGNORE_RE.finditer(content):
        ignored_rules.add(m.group(1))

    violations: list[Violation] = []

    # Import preference check (TS + Ruby)
    if "import-preference" not in ignored_rules:
        for competing in conventions.get("imports", {}).get("competing", []):
            over_mod = competing["over"]
            preferred_mod = competing["preferred"]
            # Check if the file imports the non-preferred module
            for m in _TS_IMPORT_RE.finditer(content):
                import_source = m.group(1)
                if over_mod in import_source and preferred_mod not in content:
                    violations.append(Violation(
                        rule="import-preference-violation",
                        expected=preferred_mod,
                        actual=over_mod,
                        severity="warning",
                        message=(
                            f"IMPORT: {over_mod} imported - replace with "
                            f"{preferred_mod} (all usages)"
                        ),
                    ))
                    break

    # Naming convention check (TS only)
    if language == "typescript" and "naming-convention" not in ignored_rules:
        naming = conventions.get("naming", {})
        prefix_entry = naming.get("interface_prefix")
        if prefix_entry and prefix_entry.get("consistency", 0) >= 0.60:
            expected_prefix = prefix_entry["pattern"]
            for m in _TS_INTERFACE_RE.finditer(content):
                name = m.group(1)
                if not name.startswith(expected_prefix) or (
                    len(name) > 1 and name[1].islower()
                ):
                    violations.append(Violation(
                        rule="naming-convention-violation",
                        expected=f"{expected_prefix}-prefix",
                        actual=name,
                        severity="warning",
                        message=(
                            f"NAMING: interface {name} should use "
                            f"{expected_prefix}-prefix ({prefix_entry['consistency']:.0%} convention)"
                        ),
                    ))

    return violations
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_lint_engine.py::TestConventionLint -v`
Expected: PASS

- [ ] **Step 5: Wire into lint_file in tools.py**

In `mcp/chameleon_mcp/tools.py`, in the `lint_file` function, after the existing structural lint and before the return, add convention lint:

```python
# After: best_ast_violations = ...
# Add convention lint
convention_violations: list[dict] = []
try:
    from chameleon_mcp.lint_engine import lint_conventions as _lint_conventions
    conv_data = loaded.conventions.get("conventions", {})
    arch_conv = {}
    if conv_data.get("imports", {}).get(archetype):
        arch_conv["imports"] = conv_data["imports"][archetype]
    if conv_data.get("naming", {}).get(archetype):
        arch_conv["naming"] = conv_data["naming"][archetype]
    if arch_conv:
        convention_violations = [
            v.to_dict() for v in _lint_conventions(working_content, arch_conv, language=language)
        ]
except Exception:
    pass

violations = secret_violations + best_ast_violations + convention_violations
```

- [ ] **Step 6: Run full test suite**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ tests/journey/harness/tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add mcp/chameleon_mcp/lint_engine.py mcp/chameleon_mcp/tools.py tests/unit/test_lint_engine.py
git commit -m "Add PostToolUse convention lint for imports and naming"
```

---

### Task 8: Integration test against real repos

**Files:**
- Test: `tests/unit/test_conventions.py` (add integration tests)

- [ ] **Step 1: Write integration test using ef-client data**

Add to `tests/unit/test_conventions.py`:

```python
import os

class TestIntegrationEfClient:
    """Integration test: verify conventions are extracted from a real repo."""

    def test_ef_client_conventions_if_available(self):
        repo = "/Users/crisn/Documents/Projects/Testing Apps/ef-client"
        if not os.path.isdir(repo):
            import pytest
            pytest.skip("ef-client repo not available")

        conventions_path = os.path.join(repo, ".chameleon", "conventions.json")
        if not os.path.isfile(conventions_path):
            import pytest
            pytest.skip("ef-client not bootstrapped with conventions")

        import json
        conventions = json.load(open(conventions_path))
        assert conventions["schema_version"] == CONVENTIONS_SCHEMA_VERSION

        # ef-client should have I-prefix naming convention
        naming = conventions.get("conventions", {}).get("naming", {})
        has_i_prefix = any(
            v.get("interface_prefix", {}).get("pattern") == "I"
            for v in naming.values()
        )
        assert has_i_prefix, f"Expected I-prefix convention in ef-client, got: {naming}"
```

- [ ] **Step 2: Bootstrap ef-client with new convention extractors and verify**

Run:
```bash
mcp/.venv/bin/python -c "
import sys; sys.path.insert(0, 'mcp')
from chameleon_mcp.tools import bootstrap_repo
r = bootstrap_repo(path='/Users/crisn/Documents/Projects/Testing Apps/ef-client', mode='full', force=True)
print(f'Bootstrap: {r[\"data\"][\"status\"]}')

import json
conv = json.load(open('/Users/crisn/Documents/Projects/Testing Apps/ef-client/.chameleon/conventions.json'))
print(f'Schema: {conv[\"schema_version\"]}')
print(f'Import archetypes: {list(conv[\"conventions\"][\"imports\"].keys())[:5]}')
print(f'Naming archetypes: {list(conv[\"conventions\"][\"naming\"].keys())[:5]}')
for arch, data in conv['conventions']['naming'].items():
    if 'interface_prefix' in data:
        print(f'  {arch}: I-prefix consistency={data[\"interface_prefix\"][\"consistency\"]}')
        break
"
```

Expected: conventions.json created with I-prefix naming convention detected.

- [ ] **Step 3: Run the integration test**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/test_conventions.py::TestIntegrationEfClient -v`
Expected: PASS

- [ ] **Step 4: Run full test suite one final time**

Run: `PYTHONPATH=. mcp/.venv/bin/python -m pytest tests/unit/ tests/journey/harness/tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 5: Version bump + commit**

```bash
bash scripts/bump-version.sh 0.9.0
# Update CHANGELOG.md with v0.9.0 entry
git add -A
git commit -m "Release v0.9.0: Smart Injection MVP - auto-derived import and naming conventions"
git tag v0.9.0
git push origin main v0.9.0
```

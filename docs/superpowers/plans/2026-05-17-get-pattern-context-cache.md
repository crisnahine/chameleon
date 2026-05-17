# get_pattern_context Latency: Dedup + Excerpt Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut `get_pattern_context` per-call latency by removing redundant in-call profile I/O and memoizing the expensive sanitized canonical excerpt at the long-lived daemon, without ever serving a stale exemplar.

**Architecture:** Two layers. (1) A pure refactor that collapses the double `load_profile_dir` + triple `profile.json` parse inside one call. (2) A daemon-process-global LRU memo of the post-sanitize witness excerpt keyed on the witness file's own mtime — the key the original analysis got wrong. The cache lives inside `get_pattern_context`, so both the daemon dispatch and the in-process hook fallback inherit it with zero changes to `daemon.py`/`hook_helper.py`.

**Tech Stack:** Python 3 (stdlib only — `os`, `collections.OrderedDict`), FastMCP server, custom `unittest`-style test harness run via `.venv/bin/python ../tests/<file>.py`.

---

## Spec

### Problem

`get_pattern_context(file_path)` (`mcp/chameleon_mcp/tools.py:798-907`) is the per-edit hot path. The daemon (`daemon.py:264-268`) and the hook in-process fallback (`hook_helper.py:252-254`) both route through it. Verified facts (two review rounds, spot-checked against source):

1. **Redundant in-call I/O.** One call runs `load_profile_dir` twice — once at `tools.py:843`, again inside `get_archetype` at `tools.py:570` — and JSON-parses `profile.json` a third time for a corruption probe at `tools.py:823-831`. Profile-artifact syscalls/parses are ~2× what one call needs.
2. **No memoization anywhere.** No `lru_cache`, no daemon cache (`daemon.py:264-268` is a straight pass-through), no session memo. 10 edits of the same file = 10 full resolutions including the 200 KB witness read + NFC normalize + ~25 sanitizer `str.replace` passes.
3. **The obvious cache key is wrong.** `mtime_token` (`loader.py:259-264, 322`) fingerprints only the 4 profile JSON files. The expensive payload is the *witness source file's current on-disk content* (`tools.py:864`, read fresh from a repo path in `canonicals.json`). A cache keyed on `mtime_token` serves a **stale exemplar** after any in-place edit of the witness file, with `trust_state` still `trusted` (the trust hash covers profile artifacts, not repo source — verified `trust.py:135-185`). `idioms.md` is also excluded from `mtime_token`; `/chameleon-teach` writes only `idioms.md` in place (`tools.py:2448, 2546`), no JSON/generation bump (verified).

### Design decision

- **#0 (refactor, do first):** Deduplicate. Pure win, deterministic, zero invalidation surface (same call, same profile on disk). Delete the corruption probe — proven redundant: `load_profile_dir` raises a `ValueError`-subclass / `ProfileLoadError` on a corrupt `profile.json`, and the existing `except` at `tools.py:844-847` maps it to the identical `profile_corrupted` envelope. Safe because the intervening trust calls cannot raise on corrupt JSON: `trust_state_for` reads only `.trust` (`trust.py:188-195`); `is_material_change` → `hash_profile` is `hashlib.sha256(read_bytes())`, no JSON parse (`trust.py:178-185`).
- **#1 (excerpt memo, do second):** Daemon-process-global LRU dict, key `(witness_abs_path, witness_st_mtime_ns, CONTEXT_TRANSFORM_VERSION)` → `(sanitized_content, truncated)`. The witness mtime is the mandatory-correct invalidation signal the original design missed. One extra `os.stat` (~1-5 µs) to save a 200 KB read + unicode normalize + ~25 replaces (~100 µs-1 ms+). No lock — the daemon accept loop is serial (`daemon.py:386-418`, one `_handle_connection` at a time).
- **#2 (session hook memo): dropped.** Once the excerpt memo exists, the repeated-file case already collapses at the daemon, cross-session, exactly mtime-keyed. A per-session layer would either reintroduce the staleness trap or only memoize the already-microsecond archetype resolution in a hook subprocess that rarely outlives one call. Strictly worse. Not in scope.
- **#3 (native AST extractor): out of scope.** Confirmed bootstrap/refresh-only — the per-edit path uses regex heuristics (`lint_engine.py:4-21`), never `ts_dump.mjs`. Tracked separately.

### Non-goals / explicit exclusions

- No change to `daemon.py`, `daemon_client.py`, or `hook_helper.py`. The cache is inside `get_pattern_context`; both callers inherit it.
- No change to the public `get_archetype` MCP contract (3 external callers: `server.py:46`, `daemon.py:276-281`, protocol/direct-call tests). Its `(repo, file_path) -> dict` signature and all five return envelopes stay byte-identical.
- No Tier A "profile memo" in the core scope. After #0 the in-call double-load is gone; a cross-call profile re-parse memo is a smaller, optional, measurement-gated follow-up (Task 5, explicitly optional).
- No TTL. Exact mtime invalidation only — the spec forbids ever serving a stale excerpt.

### Risks & mitigations

- *Filesystem mtime granularity:* two fast writes could collide on `st_mtime_ns` on some filesystems. Mitigation: correctness tests set mtime explicitly via `os.utime` with distinct timestamps, not write-timing.
- *Test cross-contamination from process-global state:* module-global dicts persist across cases in one test process. Mitigation: `_excerpt_cache.clear()` in every test `setUp`.
- *Cache outlives a chameleon code change:* a daemon kept alive across an upgrade could serve stale-shape sanitized content. Mitigation: `CONTEXT_TRANSFORM_VERSION` in the key; bump on any change to `sanitize_for_chameleon_context` or the 3200 truncation rule. Comments planted at both sites.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `mcp/chameleon_mcp/tools.py` | `get_pattern_context`, `get_archetype` | Modify: extract `_content_signal_for_path` + `_get_archetype_with_loaded`; rewire `get_pattern_context` to reuse its `loaded` and call the excerpt cache; delete the redundant probe |
| `mcp/chameleon_mcp/_excerpt_cache.py` | Process-global LRU memo + `CONTEXT_TRANSFORM_VERSION` + `clear()` | Create |
| `mcp/chameleon_mcp/sanitization.py` | Tag-boundary sanitizer | Modify: add a one-line comment coupling it to `CONTEXT_TRANSFORM_VERSION` |
| `tests/get_pattern_context_cache_test.py` | All tests for #0 + #1 | Create |

---

## Task 1: Extract `_content_signal_for_path` (no behavior change)

**Files:**
- Modify: `mcp/chameleon_mcp/tools.py:480-577` (top of `get_archetype`)
- Test: `tests/get_pattern_context_cache_test.py`

- [ ] **Step 1: Write the failing test**

Create `tests/get_pattern_context_cache_test.py` with this content:

```python
"""Tests for get_pattern_context dedup refactor (#0) + excerpt cache (#1).

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _write_profiled_repo(repo: Path, witness_rel: str, witness_body: str) -> None:
    """Plant a minimal committed profile whose single archetype's canonical
    witness points at `witness_rel` (a real file we also create)."""
    (repo / "package.json").write_text("{}")
    wpath = repo / witness_rel
    wpath.parent.mkdir(parents=True, exist_ok=True)
    wpath.write_text(witness_body)

    pd = repo / ".chameleon"
    pd.mkdir(parents=True, exist_ok=True)
    base = {"engine_min_version": "0.1.0", "generation": 1, "schema_version": 1}
    bucket = "src/components"
    (pd / "profile.json").write_text(json.dumps({**base, "language": "typescript"}))
    (pd / "archetypes.json").write_text(json.dumps({
        **base,
        "archetypes": {"widget": {"paths_pattern": bucket, "cluster_size": 1}},
    }))
    (pd / "canonicals.json").write_text(json.dumps({
        **base,
        "canonicals": {"widget": [{
            "witness": {"path": witness_rel, "sha_hint": "deadbeef"},
            "normative_shape": {"ast_query": {}},
        }]},
    }))
    (pd / "rules.json").write_text(json.dumps({**base, "rules": {}}))
    (pd / "idioms.md").write_text("# idioms\n\n## active\n\n## deprecated\n")
    (pd / "COMMITTED").write_text("committed-at: 2026-01-01T00:00:00Z\n")


class DedupRefactorTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>hi</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_content_signal_helper_matches_inline_logic(self):
        from chameleon_mcp.tools import _content_signal_for_path
        f = self.repo / "src" / "components" / "Widget.tsx"
        self.assertEqual(_content_signal_for_path(f), "none")
        missing = self.repo / "nope.tsx"
        self.assertEqual(_content_signal_for_path(missing), "none")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print(
        f"\nSummary: {_result.testsRun} run, "
        f"{len(_result.failures)} failed, {len(_result.errors)} errored"
    )
    sys.exit(0 if _result.wasSuccessful() else 1)
```

The `loadTestsFromModule` footer auto-discovers every case class added by
later tasks, so `python ../tests/get_pattern_context_cache_test.py` works
from Task 1 onward.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py`
Expected: FAIL — `ImportError: cannot import name '_content_signal_for_path' from 'chameleon_mcp.tools'`

- [ ] **Step 3: Add the helper to `tools.py`**

Insert this function immediately *above* `def get_archetype(` (currently `tools.py:480`):

```python
def _content_signal_for_path(p: Path) -> str:
    """Read up to 200 bytes of `p` and classify the content signal.

    Extracted from get_archetype (v0.5.2 Bug 3 logic) so the public
    get_archetype and get_pattern_context's inlined archetype resolution
    share one implementation. Returns one of
    {"none","use_client","use_server","shebang","ts_pragma"}; never None.
    """
    from chameleon_mcp.signatures import content_signal_match_for

    file_head: str | None = None
    if p.is_file():
        try:
            file_head = p.read_bytes()[:200].decode("utf-8", errors="replace")
        except OSError:
            file_head = None
    value = content_signal_match_for(file_head) if file_head is not None else "none"
    return value if value is not None else "none"
```

Then replace the inline block in `get_archetype` (`tools.py:541-557`, from `file_head: str | None = None` through the `if content_signal_value is None: content_signal_value = "none"` lines) with:

```python
    content_signal_value: str = _content_signal_for_path(p)
```

Leave the `from chameleon_mcp.signatures import (content_signal_match_for, path_pattern_bucket_for)` import in `get_archetype` as-is for now (Task 2 prunes the unused name).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py`
Expected: PASS — final line `Summary: 1 run, 0 failed, 0 errored`, exit 0

- [ ] **Step 5: Run the smoke suite to confirm no regression**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/smoke_test.py`
Expected: final line `Fail: 0` (or `0 failed`)

- [ ] **Step 6: Commit**

```bash
git add mcp/chameleon_mcp/tools.py tests/get_pattern_context_cache_test.py
git commit -m "Extract _content_signal_for_path from get_archetype"
```

---

## Task 2: Extract `_get_archetype_with_loaded`; rewire `get_pattern_context`; delete the redundant probe

**Files:**
- Modify: `mcp/chameleon_mcp/tools.py` (`get_archetype` body, `get_pattern_context`)
- Test: `tests/get_pattern_context_cache_test.py`

> **Line-number warning:** Task 1 replaced a ~16-line inline block in
> `get_archetype` with one line, so every pre-refactor `tools.py:NNN`
> anchor below is now shifted up by ~16. **Locate every edit site by the
> quoted code/symbol, not the number.** The numbers are orientation only.

- [ ] **Step 1: Write the failing tests**

Append this case class to `tests/get_pattern_context_cache_test.py`:

```python
class ArchetypeReuseTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>hi</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_public_get_archetype_contract_unchanged(self):
        from chameleon_mcp.tools import _compute_repo_id, get_archetype
        target = self.repo / "src" / "components" / "Other.tsx"
        target.write_text("export const Other = () => null;\n")
        repo_id = _compute_repo_id(self.repo.resolve())
        r = get_archetype(repo_id, str(target))["data"]
        self.assertEqual(
            set(r.keys()),
            {"archetype", "alternatives", "content_signal_match", "confidence_band"},
        )
        self.assertEqual(r["archetype"], "widget")

    def test_get_pattern_context_resolves_archetype_and_excerpt(self):
        from chameleon_mcp.tools import get_pattern_context
        target = self.repo / "src" / "components" / "Other.tsx"
        target.write_text("export const Other = () => null;\n")
        d = get_pattern_context(str(target))["data"]
        self.assertEqual(d["archetype"]["archetype"], "widget")
        self.assertIn("hi", d["canonical_excerpt"]["content"])
        self.assertEqual(d["repo"]["trust_state"], "untrusted")

    def test_corrupt_profile_json_still_profile_corrupted(self):
        # Probe deletion regression guard: a corrupt profile.json must still
        # yield profile_corrupted via the load_profile_dir except handler.
        (self.repo / ".chameleon" / "profile.json").write_text("{ not json")
        from chameleon_mcp.tools import get_pattern_context
        target = self.repo / "src" / "components" / "Other.tsx"
        target.write_text("x\n")
        d = get_pattern_context(str(target))["data"]
        self.assertEqual(d["repo"]["profile_status"], "profile_corrupted")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python -m unittest get_pattern_context_cache_test.ArchetypeReuseTest -v`
Expected: `test_get_pattern_context_resolves_archetype_and_excerpt` may pass already; `test_corrupt_profile_json_still_profile_corrupted` PASSES (current probe handles it); proceed — these guard behavior preservation during the refactor. If all pass now, that is the green baseline the refactor must keep green.

- [ ] **Step 3: Extract `_get_archetype_with_loaded`**

In `tools.py`, define a new private function. Its body is the **scoring tail of `get_archetype`, moved verbatim and unmodified** — the contiguous block that begins with the comment `# Compute the file's bucket via the same function clustering used.` and ends with the function's final `return _envelope({ ... "content_signal_match": final_signal, ... })`. That block already references only `p`, `repo_root`, `loaded`, `content_signal_value`, and module-level helpers. Signature and the import block it needs:

```python
def _get_archetype_with_loaded(
    p: Path,
    repo_root: Path,
    loaded: LoadedProfile,
    content_signal_value: str,
) -> dict:
    """Archetype scoring tail shared by get_archetype and
    get_pattern_context. Assumes repo_root + a successfully loaded
    profile; does no find_repo_root / load_profile_dir of its own.
    """
    from chameleon_mcp.lint_engine import (
        canonical_confidence,
        detect_language,
        extract_dimensions,
    )
    from chameleon_mcp.signatures import path_pattern_bucket_for

    # >>> begin verbatim move of the pre-refactor get_archetype scoring tail <<<
    # (the block starting "# Compute the file's bucket via the same
    #  function clustering used." through the final `return _envelope({...})`)
```

Cut that exact block (`# Compute the file's bucket ...` through the final `return _envelope({...})`) from `get_archetype` and paste it unchanged into the helper body after the imports.

**Annotation resolution (do NOT use a quoted string or a body-local import here).** The bare `loaded: LoadedProfile` parameter annotation is evaluated in *module* scope, not the helper's body scope, so a function-local `from chameleon_mcp.profile.loader import LoadedProfile` does NOT satisfy it (ruff `F821`), and `from __future__ import annotations` (already at the top of `tools.py`) makes such a body-local import `F401`-dead. Add a module-level type-only import at the top of `tools.py` instead:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: keeps the MCP cold-start budget intact (the real import
    # stays function-local in get_archetype) while letting the bare
    # `loaded: LoadedProfile` signature annotation resolve under
    # `from __future__ import annotations`.
    from chameleon_mcp.profile.loader import LoadedProfile
```

This costs zero runtime import (preserving the module's deliberate function-local-import / cold-start convention).

- [ ] **Step 4: Rewrite public `get_archetype` to delegate**

In `get_archetype`, everything from `repo_root = find_repo_root(p)` down to the end of the function (this is exactly: the repo-id guard, the `load_profile_dir` try/except, and the scoring block you just cut in Step 3) is replaced by the following. The scoring block now lives in the helper, so what remains is the load + a delegate call:

```python
    repo_root = find_repo_root(p)
    if repo_root is None or _compute_repo_id(repo_root) != repo:
        return _envelope({
            "archetype": None,
            "alternatives": [],
            "content_signal_match": content_signal_value,
            "confidence_band": "low",
        })

    profile_dir = repo_root / ".chameleon"
    try:
        loaded: LoadedProfile = load_profile_dir(profile_dir)
    except Exception:
        return _envelope({
            "archetype": None,
            "alternatives": [],
            "content_signal_match": content_signal_value,
            "confidence_band": "low",
        })

    return _get_archetype_with_loaded(p, repo_root, loaded, content_signal_value)
```

In `get_archetype`'s function-local import block, keep only `from chameleon_mcp.profile.loader import find_repo_root, load_profile_dir`. Remove now-unused names: `canonical_confidence`, `detect_language`, `extract_dimensions`, `path_pattern_bucket_for`, `content_signal_match_for` (moved into the helpers), AND `LoadedProfile` (it now resolves via the module-level `TYPE_CHECKING` block from Step 3 — leaving it in the function-local import is `F401`-dead since `from __future__ import annotations` stringifies `get_archetype`'s own `loaded: LoadedProfile` body annotation).

- [ ] **Step 5: Rewire `get_pattern_context` — reuse `loaded`, drop the probe**

In `get_pattern_context`: delete the corruption probe block at `tools.py:821-831` (the `# BUG-021/022` comment, the `try: import json as _json ... except (OSError, ValueError): return _envelope(_empty_pattern_envelope(repo_id, "profile_corrupted", "n/a"))`). The missing-file guard at `:816-819` and the `load_profile_dir` except at `:842-847` already cover both cases.

Replace the call at `tools.py:850` `arch_response = get_archetype(repo_id, file_path)` with:

```python
    content_signal_value = _content_signal_for_path(p)
    arch_response = _get_archetype_with_loaded(
        p, repo_root, loaded, content_signal_value
    )
```

(`p` is `tools.py:806`, `repo_root` `:807`, `loaded` `:843`.) Leave `arch_data = arch_response["data"]` and everything below unchanged.

- [ ] **Step 6: Run the refactor guard tests**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python -m unittest get_pattern_context_cache_test -v`
Expected: `OK` — all `DedupRefactorTest` + `ArchetypeReuseTest` cases pass, including `test_corrupt_profile_json_still_profile_corrupted` (probe-deletion safety) and `test_public_get_archetype_contract_unchanged`.

- [ ] **Step 7: Run the full isolation suite**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py`
Expected: final line `ALL ORDERS PASSED` (or `0` failures across all 4 orders). Also run the MCP-contract test directly:
Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/mcp_protocol_test.py`
Expected: `Fail: 0`

- [ ] **Step 8: Commit**

```bash
git add mcp/chameleon_mcp/tools.py tests/get_pattern_context_cache_test.py
git commit -m "Dedup profile load in get_pattern_context"
```

---

## Task 3: Create the excerpt cache module

**Files:**
- Create: `mcp/chameleon_mcp/_excerpt_cache.py`
- Modify: `mcp/chameleon_mcp/sanitization.py` (one comment line)
- Test: `tests/get_pattern_context_cache_test.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/get_pattern_context_cache_test.py`:

```python
class ExcerptCacheModuleTest(unittest.TestCase):
    def setUp(self):
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def test_get_miss_then_hit(self):
        from chameleon_mcp import _excerpt_cache
        calls = []

        def build():
            calls.append(1)
            return ("SANITIZED", False)

        k = ("/abs/Witness.tsx", 12345, _excerpt_cache.CONTEXT_TRANSFORM_VERSION)
        self.assertEqual(_excerpt_cache.get_or_build(k, build), ("SANITIZED", False))
        self.assertEqual(_excerpt_cache.get_or_build(k, build), ("SANITIZED", False))
        self.assertEqual(len(calls), 1, "second call must be a cache hit")

    def test_distinct_keys_are_independent(self):
        from chameleon_mcp import _excerpt_cache
        a = _excerpt_cache.get_or_build(("a", 1, 1), lambda: ("A", False))
        b = _excerpt_cache.get_or_build(("b", 1, 1), lambda: ("B", True))
        self.assertEqual(a, ("A", False))
        self.assertEqual(b, ("B", True))

    def test_lru_eviction_at_cap(self):
        from chameleon_mcp import _excerpt_cache
        cap = _excerpt_cache._CAP
        for i in range(cap + 5):
            _excerpt_cache.get_or_build((f"k{i}", 0, 1), lambda i=i: (str(i), False))
        # Oldest (k0) evicted; rebuild must run again.
        rebuilt = []
        _excerpt_cache.get_or_build(
            ("k0", 0, 1), lambda: rebuilt.append(1) or ("0", False)
        )
        self.assertEqual(rebuilt, [1])

    def test_clear_empties_cache(self):
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.get_or_build(("x", 0, 1), lambda: ("X", False))
        _excerpt_cache.clear()
        n = []
        _excerpt_cache.get_or_build(("x", 0, 1), lambda: n.append(1) or ("X", False))
        self.assertEqual(n, [1])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python -m unittest get_pattern_context_cache_test.ExcerptCacheModuleTest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chameleon_mcp._excerpt_cache'`

- [ ] **Step 3: Create `mcp/chameleon_mcp/_excerpt_cache.py`**

```python
"""Process-global LRU memo for the sanitized canonical excerpt.

Lives in the long-lived daemon process (and the MCP stdio server). The
hook in-process fallback runs in a short-lived subprocess where this is
a per-invocation no-op — harmless, no cross-process sharing.

Key: (witness_abs_path: str, witness_st_mtime_ns: int,
      CONTEXT_TRANSFORM_VERSION: int).

The witness mtime is the load-bearing invalidation signal: editing the
witness source file in place changes st_mtime_ns and busts the entry,
which mtime_token (4 profile JSONs only) does NOT detect.

No lock: daemon.serve_forever handles one connection at a time
(daemon.py:386-418). If the daemon ever becomes multi-threaded, wrap
get_or_build in a threading.Lock.

CONTEXT_TRANSFORM_VERSION: bump on ANY change to the value-shaping
transform applied between read and cache store — i.e. any change to
chameleon_mcp.sanitization.sanitize_for_chameleon_context OR the 3200
truncation rule in tools.get_pattern_context.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable

CONTEXT_TRANSFORM_VERSION = 1

_CAP = 64
_CACHE: "OrderedDict[tuple, tuple[str, bool]]" = OrderedDict()


def get_or_build(
    key: tuple, build: Callable[[], "tuple[str, bool]"]
) -> "tuple[str, bool]":
    """Return the cached (content, truncated) for `key`, or build, store,
    and return it. LRU: most-recent key moves to the end; oldest evicted
    when over _CAP."""
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    value = build()
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    if len(_CACHE) > _CAP:
        _CACHE.popitem(last=False)
    return value


def clear() -> None:
    """Drop all entries. Used by tests and any future explicit
    invalidation hook."""
    _CACHE.clear()
```

- [ ] **Step 4: Plant the version-coupling comment in `sanitization.py`**

In `mcp/chameleon_mcp/sanitization.py`, immediately above `def sanitize_for_chameleon_context(content: str) -> str:`, add:

```python
# NOTE: changing this transform (token list, bidi set, order, NFC step)
# is a cache-visible change. Bump _excerpt_cache.CONTEXT_TRANSFORM_VERSION.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python -m unittest get_pattern_context_cache_test.ExcerptCacheModuleTest -v`
Expected: `OK` (4 tests)

- [ ] **Step 6: Commit**

```bash
git add mcp/chameleon_mcp/_excerpt_cache.py mcp/chameleon_mcp/sanitization.py tests/get_pattern_context_cache_test.py
git commit -m "Add process-global excerpt cache module"
```

---

## Task 4: Wire the excerpt cache into `get_pattern_context`

**Files:**
- Modify: `mcp/chameleon_mcp/tools.py` (witness block `:853-878`)
- Test: `tests/get_pattern_context_cache_test.py` (+ file footer)

- [ ] **Step 1: Write the failing tests + add the harness footer**

Append to `tests/get_pattern_context_cache_test.py`:

```python
class ExcerptCacheIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>ORIGINAL</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()
        self.target = self.repo / "src" / "components" / "Other.tsx"
        self.target.write_text("export const Other = () => null;\n")

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_repeated_call_hits_cache(self):
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        get_pattern_context(str(self.target))
        sentinel = {"built": 0}
        real = _excerpt_cache.get_or_build

        def spy(key, build):
            def wrapped():
                sentinel["built"] += 1
                return build()
            return real(key, wrapped)

        _excerpt_cache.get_or_build = spy
        try:
            d = get_pattern_context(str(self.target))["data"]
        finally:
            _excerpt_cache.get_or_build = real
        self.assertIn("ORIGINAL", d["canonical_excerpt"]["content"])
        self.assertEqual(sentinel["built"], 0, "witness re-read despite warm cache")

    def test_in_place_witness_edit_busts_cache(self):
        # The bug the original mtime_token design would have shipped.
        from chameleon_mcp.tools import get_pattern_context
        w = self.repo / "src" / "components" / "Widget.tsx"
        d1 = get_pattern_context(str(self.target))["data"]
        self.assertIn("ORIGINAL", d1["canonical_excerpt"]["content"])
        w.write_text("export const Widget = () => <div>EDITED</div>;\n")
        os.utime(w, ns=(2_000_000_000, 2_000_000_000))  # distinct mtime
        d2 = get_pattern_context(str(self.target))["data"]
        self.assertIn("EDITED", d2["canonical_excerpt"]["content"])
        self.assertNotIn("ORIGINAL", d2["canonical_excerpt"]["content"])

    def test_transform_version_bump_busts_cache(self):
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        get_pattern_context(str(self.target))
        _excerpt_cache.CONTEXT_TRANSFORM_VERSION += 1
        try:
            rebuilt = []
            real = _excerpt_cache.get_or_build

            def spy(key, build):
                def wrapped():
                    rebuilt.append(1)
                    return build()
                return real(key, wrapped)

            _excerpt_cache.get_or_build = spy
            try:
                get_pattern_context(str(self.target))
            finally:
                _excerpt_cache.get_or_build = real
            self.assertEqual(rebuilt, [1], "version bump must force rebuild")
        finally:
            _excerpt_cache.CONTEXT_TRANSFORM_VERSION -= 1
```

Append the `ExcerptCacheIntegrationTest` class **above** the existing
`if __name__ == "__main__":` footer that Task 1 already wrote — do not
add a second footer (two `__main__` blocks would run the suite twice and
double-`sys.exit`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py`
Expected: FAIL — `test_repeated_call_hits_cache` and `test_transform_version_bump_busts_cache` fail (cache not wired; `sentinel["built"]` is 0 only because `get_or_build` is never called). `test_in_place_witness_edit_busts_cache` passes (no cache yet = always fresh).

- [ ] **Step 3: Wire the cache into the witness block**

In `get_pattern_context`, replace the witness read block (`tools.py:859-878`, the `if witness_rel:` body that does `safe_read_text` → truncate → sanitize → `canonical_data = {...}`) with:

```python
            if witness_rel:
                try:
                    import os as _os

                    from chameleon_mcp import _excerpt_cache
                    from chameleon_mcp.safe_open import (
                        UnsafeFileError,
                        safe_open,
                    )
                    from chameleon_mcp.sanitization import (
                        sanitize_for_chameleon_context,
                    )

                    safe_path = safe_open(
                        repo_root, witness_rel, max_size_bytes=200_000
                    )
                    mtime_ns = _os.stat(safe_path).st_mtime_ns
                    key = (
                        str(safe_path),
                        mtime_ns,
                        _excerpt_cache.CONTEXT_TRANSFORM_VERSION,
                    )

                    def _build() -> "tuple[str, bool]":
                        raw = safe_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        is_trunc = len(raw) > 3200
                        body = (
                            raw[:3200] + "\n... [truncated]"
                            if is_trunc
                            else raw
                        )
                        return sanitize_for_chameleon_context(body), is_trunc

                    content, truncated = _excerpt_cache.get_or_build(
                        key, _build
                    )
                    canonical_data = {
                        "content": content,
                        "witness_path": witness_rel,
                        "truncated": truncated,
                        "sha_hint": first.get("witness", {}).get("sha_hint"),
                    }
                except (UnsafeFileError, FileNotFoundError, OSError):
                    pass
```

This preserves the exact fallback contract (any `UnsafeFileError`/`FileNotFoundError`/`OSError` leaves `canonical_data` at its default) and the exact truncation rule (`> 3200` → first 3200 + `\n... [truncated]`). `sha_hint` stays read from `first` (frozen metadata, not the cached transform — unchanged).

- [ ] **Step 4: Run the integration tests**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py`
Expected: `Summary: N run, 0 failed, 0 errored`, exit 0. Specifically `test_in_place_witness_edit_busts_cache` PASSES (the witness-mtime key works) and `test_repeated_call_hits_cache` PASSES (warm hit, no rebuild).

- [ ] **Step 5: Daemon wire-equivalence regression**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/daemon_test.py`
Expected: final `Fail: 0`. (Confirms "get_pattern_context over the socket matches the in-process result" still holds with the cache — a hit returns identical content.)

- [ ] **Step 6: Full isolation suite + teach roundtrip**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py`
Expected: all 4 orders pass, 0 failures.
Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/teach_roundtrip_test.py`
Expected: `Fail: 0` if `CHAMELEON_TEST_TS_REPO` is set; clean skip otherwise.

- [ ] **Step 7: Commit**

```bash
git add mcp/chameleon_mcp/tools.py tests/get_pattern_context_cache_test.py
git commit -m "Memoize sanitized canonical excerpt by witness mtime"
```

---

## Task 5 (OPTIONAL — measurement-gated): cross-call profile-load memo

**Do not implement unless a measured before/after shows `load_profile_dir` JSON parsing is still a material fraction of warm-call latency after Tasks 1-4.** After #0 the in-call double-load is gone; this only saves re-parsing 4 JSON files on a *subsequent* daemon call when the profile is unchanged, at the cost of 5 `stat()`s + an idioms-mtime key term. Smaller win, real added invalidation surface.

**Files:**
- Modify: `mcp/chameleon_mcp/_excerpt_cache.py` (add a second `OrderedDict` + `load_profile_cached`)
- Modify: `mcp/chameleon_mcp/tools.py:843` (`get_pattern_context` profile load)
- Test: `tests/get_pattern_context_cache_test.py`

- [ ] **Step 1: Measure first**

Run a 200-iteration timing of `get_pattern_context` on a warm profile (cache from Task 4 active), with and without a stubbed `load_profile_dir`, using the repo built by `ExcerptCacheIntegrationTest.setUp`. Record p50/p99. Proceed only if removing the JSON parse moves p99 by a margin worth the invalidation surface (rule of thumb: >15% of warm p99). Document the numbers in the commit body if you proceed; otherwise stop and commit nothing.

- [ ] **Step 2: Write the failing tests**

Append:

```python
class ProfileMemoTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>hi</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_idioms_change_busts_profile_memo(self):
        from chameleon_mcp import _excerpt_cache
        pd = self.repo / ".chameleon"
        first = _excerpt_cache.load_profile_cached(pd)
        (pd / "idioms.md").write_text("# idioms\n\n## active\n\n### x\nNEW\n")
        os.utime(pd / "idioms.md", ns=(3_000_000_000, 3_000_000_000))
        second = _excerpt_cache.load_profile_cached(pd)
        self.assertIn("NEW", second.idioms_text)
        self.assertNotIn("NEW", first.idioms_text)

    def test_unchanged_profile_is_cached(self):
        from chameleon_mcp import _excerpt_cache
        pd = self.repo / ".chameleon"
        a = _excerpt_cache.load_profile_cached(pd)
        b = _excerpt_cache.load_profile_cached(pd)
        self.assertIs(a, b, "unchanged profile must return the same object")
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python -m unittest get_pattern_context_cache_test.ProfileMemoTest -v`
Expected: FAIL — `AttributeError: module 'chameleon_mcp._excerpt_cache' has no attribute 'load_profile_cached'`

- [ ] **Step 4: Add `load_profile_cached` to `_excerpt_cache.py`**

```python
import os as _os
from pathlib import Path as _Path

_PROFILE_CACHE: "OrderedDict[tuple, object]" = OrderedDict()
_PROFILE_JSONS = ("profile.json", "archetypes.json", "rules.json", "canonicals.json")


def _mtime_ns(p: "_Path") -> int:
    try:
        return _os.stat(p).st_mtime_ns
    except OSError:
        return 0


def load_profile_cached(profile_dir: "_Path"):
    """Memoized load_profile_dir keyed on the 4 JSON mtimes PLUS idioms.md
    mtime (which mtime_token excludes — /chameleon-teach writes only
    idioms.md). Stat-only precheck; full load only on miss."""
    from chameleon_mcp.profile.loader import load_profile_dir

    key = (
        str(profile_dir),
        tuple(_mtime_ns(profile_dir / n) for n in _PROFILE_JSONS),
        _mtime_ns(profile_dir / "idioms.md"),
    )
    hit = _PROFILE_CACHE.get(key)
    if hit is not None:
        _PROFILE_CACHE.move_to_end(key)
        return hit
    loaded = load_profile_dir(profile_dir)
    _PROFILE_CACHE[key] = loaded
    _PROFILE_CACHE.move_to_end(key)
    if len(_PROFILE_CACHE) > _CAP:
        _PROFILE_CACHE.popitem(last=False)
    return loaded
```

Extend `clear()` to also `_PROFILE_CACHE.clear()`.

- [ ] **Step 5: Use it in `get_pattern_context`**

At `tools.py:842-847`, replace `loaded = load_profile_dir(profile_dir)` with `loaded = _excerpt_cache.load_profile_cached(profile_dir)` (add `from chameleon_mcp import _excerpt_cache` at the top of that try). Keep the `except Exception: -> profile_corrupted` handler unchanged — `load_profile_cached` propagates `load_profile_dir`'s exceptions on a miss.

- [ ] **Step 6: Run tests + full suite**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py`
Expected: `Summary: N run, 0 failed, 0 errored`
Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py`
Expected: all 4 orders, 0 failures.

- [ ] **Step 7: Commit (only if Step 1 justified it)**

```bash
git add mcp/chameleon_mcp/_excerpt_cache.py mcp/chameleon_mcp/tools.py tests/get_pattern_context_cache_test.py
git commit -m "Memoize profile load across daemon calls"
```

---

## Task 6: Final verification

- [ ] **Step 1: Full suite, all orders**

Run: `cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py`
Expected: every order passes, 0 failures.

- [ ] **Step 2: Targeted regression set**

Run each, expect `Fail: 0` / `0 failed`:
```bash
cd mcp
PYTHONPATH=.:../tests .venv/bin/python ../tests/smoke_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/mcp_protocol_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/daemon_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_2_clustering_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_corrupted_profile_test.py
PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py
```

- [ ] **Step 3: Confirm no daemon/hook files were touched**

Run: `git diff --stat main -- mcp/chameleon_mcp/daemon.py mcp/chameleon_mcp/daemon_client.py mcp/chameleon_mcp/hook_helper.py`
Expected: empty output (non-goal upheld — both callers inherit the cache via `get_pattern_context`).

- [ ] **Step 4: Manual latency sanity check**

In a Python REPL with the env from `ExcerptCacheIntegrationTest`, time 100 consecutive `get_pattern_context` calls on the same target. Confirm calls 2..100 are materially faster than call 1 (warm excerpt hit). Record observed p50 cold vs warm in the final commit body or the PR description.

- [ ] **Step 5: Final commit (if any verification-only fixups)**

```bash
git add -A
git commit -m "Finalize get_pattern_context cache verification"
```

---

## Self-Review

**1. Spec coverage:**
- #0 dedup → Tasks 1-2 (extract helpers, reuse `loaded`, delete probe). ✓
- Probe-deletion safety (corrupt profile.json still `profile_corrupted`) → Task 2 Step 1 `test_corrupt_profile_json_still_profile_corrupted`. ✓
- Public `get_archetype` contract preserved → Task 2 Step 1 `test_public_get_archetype_contract_unchanged` + Task 6 `mcp_protocol_test.py`. ✓
- #1 excerpt memo, correct witness-mtime key → Tasks 3-4, esp. `test_in_place_witness_edit_busts_cache` (the bug the original design would have shipped). ✓
- `CONTEXT_TRANSFORM_VERSION` guard → Task 3 (constant + sanitization.py comment) + Task 4 `test_transform_version_bump_busts_cache`. ✓
- No daemon/hook changes → Task 6 Step 3 git-diff assertion. ✓
- Daemon wire-equivalence → Task 4 Step 5 `daemon_test.py`. ✓
- #2 dropped, #3 out of scope → stated in Spec non-goals; no tasks (correct). ✓
- Tier A optional/measured → Task 5 explicitly gated on Step 1 measurement. ✓
- idioms.md excluded from mtime_token risk → Task 5 `test_idioms_change_busts_profile_memo` (only relevant if Task 5 runs). ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". The one verbatim code move (`get_archetype` scoring tail → `_get_archetype_with_loaded`) is a precise cut/paste anchored by a unique start comment (`# Compute the file's bucket ...`) and the function's final `return _envelope({...})`, with the new signature shown in full — moved unchanged code, not a placeholder. A line-number warning at the top of Task 2 tells the executor to anchor by code, not the shifted numbers.

**3. Type consistency:** `get_or_build(key, build) -> tuple[str, bool]`, `clear()`, `CONTEXT_TRANSFORM_VERSION`, `_CAP`, `load_profile_cached(profile_dir)` used identically across Tasks 3/4/5 and all test cases. `_content_signal_for_path(p: Path) -> str` and `_get_archetype_with_loaded(p, repo_root, loaded, content_signal_value) -> dict` consistent across Tasks 1/2 and `get_pattern_context`. Cache value is `(content, truncated)` everywhere.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-17-get-pattern-context-cache.md`. Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, two-stage review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Which approach?

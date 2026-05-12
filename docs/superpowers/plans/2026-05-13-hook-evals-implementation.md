# Hook Eval Scenario Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fast deterministic synthetic-file scenario suite for chameleon's pattern advisory, fillable gap between unit tests (per-function) and the calibration harness (slow, gated, per-developer).

**Architecture:** Two checked-in fixture repos with their `.chameleon/` profiles committed; scenarios assert on `get_pattern_context` output (default mode) or pipe through `hooks/preflight-and-advise` (`--full` opt-in mode); runner integrates as a 6th entry in `tests/run_all_orders.py`.

**Tech Stack:** Python 3.11+ (matches `mcp/.venv`), stdlib-only test pattern matching other suites in `tests/`, bash for refresh script.

**Spec:** `docs/superpowers/specs/2026-05-12-hook-evals-design.md`. Read it first.

---

## File Structure

**Created:**

- `tests/fixtures/eval_repos/ts_minimal/` (entire fixture repo, ~15 source files + `package.json` + checked-in `.chameleon/`)
- `tests/fixtures/eval_repos/ruby_minimal/` (entire fixture repo, ~15 source files + `Gemfile` + checked-in `.chameleon/`)
- `tests/hook_evals/runner.py` (main entry, scenario discovery, per-scenario execution, output)
- `tests/hook_evals/runner_test.py` (unit tests for the runner)
- `tests/hook_evals/README.md` (usage docs)
- `tests/hook_evals/scenarios/ts/*.json` (5 scenarios)
- `tests/hook_evals/scenarios/ruby/*.json` (5 scenarios)
- `tests/hook_evals/scenarios/cross/*.json` (3 cross-cutting scenarios)
- `scripts/refresh_eval_fixtures.sh` (refresh script with `--check`/`--apply`)
- `tests/now_threading_test.py` (verifies the `now=` plumbing)

**Modified:**

- `mcp/chameleon_mcp/bootstrap/orchestrator.py:814,991,1286` (thread `now=`)
- `mcp/chameleon_mcp/tools.py:2030` (thread `now=`)
- `tests/run_all_orders.py:17-23` (add 6th entry)

---

## Task 1: Thread `now=` parameter through bootstrap chain

**Files:**

- Create: `tests/now_threading_test.py`
- Modify: `mcp/chameleon_mcp/bootstrap/orchestrator.py:814,991,1286`
- Modify: `mcp/chameleon_mcp/tools.py:2030`

- [ ] **Step 1: Write the failing test**

Create `tests/now_threading_test.py`:

```python
"""Verify `now=` threads from tools.bootstrap_repo down to select_canonicals.

Enables refresh_eval_fixtures.sh to pin time for deterministic witness
selection. Seam already existed at canonical.py:152; this test guards
the plumbing.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp import tools
import chameleon_mcp.bootstrap.canonical as canonical_mod


class NowThreadingTest(unittest.TestCase):
    def test_bootstrap_repo_threads_now_to_select_canonicals(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text("{}")
            (repo / "src").mkdir()
            for i in range(6):
                (repo / "src" / f"util_{i}.ts").write_text(
                    f"export const v{i} = {i};\n"
                )

            real_select = canonical_mod.select_canonicals
            with patch.object(
                canonical_mod, "select_canonicals", wraps=real_select
            ) as mock_select:
                tools.bootstrap_repo(str(repo), now=12345.0)

            self.assertTrue(mock_select.called)
            now_values = [
                call.kwargs.get("now") for call in mock_select.call_args_list
            ]
            self.assertIn(12345.0, now_values)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/now_threading_test.py
```

Expected: FAIL with `TypeError: bootstrap_repo() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Add `now=` to `orchestrator._bootstrap_single`**

In `mcp/chameleon_mcp/bootstrap/orchestrator.py`, change line 991-996 from:

```python
def _bootstrap_single(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
) -> BootstrapReport:
```

to:

```python
def _bootstrap_single(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
    now: float | None = None,
) -> BootstrapReport:
```

Change line 1286 from:

```python
    selection = select_canonicals(clustering.dense_clusters, repo_root)
```

to:

```python
    selection = select_canonicals(clustering.dense_clusters, repo_root, now=now)
```

- [ ] **Step 4: Add `now=` to `orchestrator.bootstrap_repo`**

In the same file, change line 814-819 from:

```python
def bootstrap_repo(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
) -> BootstrapReport:
```

to:

```python
def bootstrap_repo(
    repo_root: Path,
    *,
    paths_glob: str | None = None,
    profile_dir_name: str = ".chameleon",
    now: float | None = None,
) -> BootstrapReport:
```

Find every `_bootstrap_single(...)` call inside `bootstrap_repo`'s body (search for `_bootstrap_single(`) and pass `now=now` to each.

- [ ] **Step 5: Add `now=` to `tools.bootstrap_repo`**

In `mcp/chameleon_mcp/tools.py`, change line 2030-2035 from:

```python
def bootstrap_repo(
    path: str,
    mode: str = "full",
    paths_glob: str | None = None,
    force: bool = False,
) -> dict:
```

to:

```python
def bootstrap_repo(
    path: str,
    mode: str = "full",
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
) -> dict:
```

Find the `_bootstrap(...)` call inside this function (imported as `from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo as _bootstrap`) and pass `now=now` to it.

- [ ] **Step 6: Run test to verify it passes**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/now_threading_test.py
```

Expected: PASS.

- [ ] **Step 7: Run full suite to ensure no regression**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py
```

Expected: all 5 existing suites pass across all randomized orders.

- [ ] **Step 8: Commit**

```bash
git add mcp/chameleon_mcp/bootstrap/orchestrator.py mcp/chameleon_mcp/tools.py tests/now_threading_test.py
git commit -m "Thread now= from bootstrap_repo down to select_canonicals

Required so refresh_eval_fixtures.sh can pin time for deterministic
witness selection. The seam already existed at canonical.py:152; this
adds the plumbing through orchestrator and the MCP-facing wrapper."
```

---

## Task 2: Create `ts_minimal` fixture source files

**Files:**

- Create: `tests/fixtures/eval_repos/ts_minimal/package.json`
- Create: `tests/fixtures/eval_repos/ts_minimal/src/utils/format_date.ts` (and 4 more utility files)
- Create: `tests/fixtures/eval_repos/ts_minimal/src/components/Button.tsx` (and 4 more component files)
- Create: `tests/fixtures/eval_repos/ts_minimal/src/types/api.ts` (and 4 more type files)

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p tests/fixtures/eval_repos/ts_minimal/src/utils
mkdir -p tests/fixtures/eval_repos/ts_minimal/src/components
mkdir -p tests/fixtures/eval_repos/ts_minimal/src/types
```

- [ ] **Step 2: Create `package.json`**

`tests/fixtures/eval_repos/ts_minimal/package.json`:

```json
{
  "name": "ts-minimal-fixture",
  "version": "0.0.0",
  "private": true,
  "description": "chameleon eval-fixture: TS repo for hook_evals scenarios"
}
```

- [ ] **Step 3: Create 5 utility files**

Each utility file follows the same shape: a single named arrow function export, no JSX. This gives them the same cluster key so they form one archetype.

`tests/fixtures/eval_repos/ts_minimal/src/utils/format_date.ts`:

```typescript
export const formatDate = (d: Date): string => {
  return d.toISOString();
};
```

`tests/fixtures/eval_repos/ts_minimal/src/utils/format_number.ts`:

```typescript
export const formatNumber = (n: number): string => {
  return n.toFixed(2);
};
```

`tests/fixtures/eval_repos/ts_minimal/src/utils/format_string.ts`:

```typescript
export const formatString = (s: string): string => {
  return s.trim().toLowerCase();
};
```

`tests/fixtures/eval_repos/ts_minimal/src/utils/format_json.ts`:

```typescript
export const formatJson = (obj: object): string => {
  return JSON.stringify(obj, null, 2);
};
```

`tests/fixtures/eval_repos/ts_minimal/src/utils/format_html.ts`:

```typescript
export const formatHtml = (s: string): string => {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
};
```

- [ ] **Step 4: Create 5 component files**

Each component file is a named arrow function returning JSX. Same cluster key as siblings, different from utils (JSX present).

`tests/fixtures/eval_repos/ts_minimal/src/components/Button.tsx`:

```typescript
export const Button = (props: { label: string }) => {
  return <button>{props.label}</button>;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/components/Input.tsx`:

```typescript
export const Input = (props: { value: string }) => {
  return <input value={props.value} />;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/components/Card.tsx`:

```typescript
export const Card = (props: { title: string }) => {
  return <div className="card">{props.title}</div>;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/components/Modal.tsx`:

```typescript
export const Modal = (props: { open: boolean }) => {
  return props.open ? <div className="modal" /> : null;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/components/Alert.tsx`:

```typescript
export const Alert = (props: { level: string }) => {
  return <div className={`alert-${props.level}`} />;
};
```

- [ ] **Step 5: Create 5 type-only files**

Each declares a type alias. Different cluster key (no value export, type export only).

`tests/fixtures/eval_repos/ts_minimal/src/types/api.ts`:

```typescript
export type ApiResponse = {
  status: number;
  body: string;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/types/user.ts`:

```typescript
export type User = {
  id: string;
  email: string;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/types/events.ts`:

```typescript
export type Event = {
  name: string;
  at: number;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/types/forms.ts`:

```typescript
export type FormState = {
  values: Record<string, string>;
  errors: Record<string, string>;
};
```

`tests/fixtures/eval_repos/ts_minimal/src/types/shared.ts`:

```typescript
export type ID = string;
```

- [ ] **Step 6: Verify directory contents**

```bash
find tests/fixtures/eval_repos/ts_minimal -type f | sort
```

Expected output (16 files):

```
tests/fixtures/eval_repos/ts_minimal/package.json
tests/fixtures/eval_repos/ts_minimal/src/components/Alert.tsx
tests/fixtures/eval_repos/ts_minimal/src/components/Button.tsx
tests/fixtures/eval_repos/ts_minimal/src/components/Card.tsx
tests/fixtures/eval_repos/ts_minimal/src/components/Input.tsx
tests/fixtures/eval_repos/ts_minimal/src/components/Modal.tsx
tests/fixtures/eval_repos/ts_minimal/src/types/api.ts
tests/fixtures/eval_repos/ts_minimal/src/types/events.ts
tests/fixtures/eval_repos/ts_minimal/src/types/forms.ts
tests/fixtures/eval_repos/ts_minimal/src/types/shared.ts
tests/fixtures/eval_repos/ts_minimal/src/types/user.ts
tests/fixtures/eval_repos/ts_minimal/src/utils/format_date.ts
tests/fixtures/eval_repos/ts_minimal/src/utils/format_html.ts
tests/fixtures/eval_repos/ts_minimal/src/utils/format_json.ts
tests/fixtures/eval_repos/ts_minimal/src/utils/format_number.ts
tests/fixtures/eval_repos/ts_minimal/src/utils/format_string.ts
```

- [ ] **Step 7: Commit source files (without .chameleon yet)**

```bash
git add tests/fixtures/eval_repos/ts_minimal/
git commit -m "Add ts_minimal eval-fixture source files

15 source files plus package.json, structured to form 3 dense
archetypes when bootstrapped (5 utility, 5 component, 5 type-only)."
```

---

## Task 3: Bootstrap `ts_minimal` with pinned `now` and commit `.chameleon/`

**Files:**

- Create: `tests/fixtures/eval_repos/ts_minimal/.chameleon/` (entire dir generated by bootstrap)

- [ ] **Step 1: Bootstrap the fixture with pinned `now`**

The pinned `now` is the moment-in-time the fixture commits to. We pick `1700000000.0` (2023-11-14 22:13:20 UTC) — a stable past date so every checked-in mtime falls outside the recency window, eliminating mtime-tie ambiguity.

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python -c '
from chameleon_mcp.tools import bootstrap_repo
import json, sys
result = bootstrap_repo(
    "/Users/crisn/Documents/Projects/chameleon/tests/fixtures/eval_repos/ts_minimal",
    now=1700000000.0,
)
print(json.dumps(result["data"], indent=2))
sys.exit(0 if result["data"].get("status") == "success" else 1)
'
```

Expected: prints `"status": "success"` plus an `archetypes_detected` count of 3.

- [ ] **Step 2: Inspect `archetypes.json` and `canonicals.json`**

```bash
cat tests/fixtures/eval_repos/ts_minimal/.chameleon/archetypes.json | python -m json.tool | head -40
cat tests/fixtures/eval_repos/ts_minimal/.chameleon/canonicals.json | python -m json.tool | head -60
```

Note down the 3 archetype names (chameleon-generated cluster keys, format like `named_arrow_no_jsx_at_src_utils_<hash>`). You will reference these names in Task 8 scenarios.

Write the names into a temporary log so they are visible later:

```bash
python -c '
import json
data = json.load(open("tests/fixtures/eval_repos/ts_minimal/.chameleon/archetypes.json"))
for name in sorted(data.get("archetypes", {}).keys()):
    print(name)
' > /tmp/ts_minimal_archetype_names.txt
cat /tmp/ts_minimal_archetype_names.txt
```

Expected: exactly 3 lines, one per archetype.

- [ ] **Step 3: Verify the `COMMITTED` sentinel exists**

```bash
ls tests/fixtures/eval_repos/ts_minimal/.chameleon/
```

Expected files: `COMMITTED`, `archetypes.json`, `canonicals.json`, `idioms.md`, `profile.json`, `rules.json` (and possibly more).

- [ ] **Step 4: Commit `.chameleon/`**

```bash
git add tests/fixtures/eval_repos/ts_minimal/.chameleon
git commit -m "Bootstrap ts_minimal fixture profile (pinned now=1700000000)

3 archetypes detected from the 15-file fixture. Profile is checked
in so scenarios exercise the advisory layer, not bootstrap."
```

---

## Task 4: Create `ruby_minimal` fixture source files

**Files:**

- Create: `tests/fixtures/eval_repos/ruby_minimal/Gemfile`
- Create: `tests/fixtures/eval_repos/ruby_minimal/app/models/*.rb` (5 files)
- Create: `tests/fixtures/eval_repos/ruby_minimal/app/controllers/*.rb` (5 files)
- Create: `tests/fixtures/eval_repos/ruby_minimal/spec/models/*.rb` (5 files)

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p tests/fixtures/eval_repos/ruby_minimal/app/models
mkdir -p tests/fixtures/eval_repos/ruby_minimal/app/controllers
mkdir -p tests/fixtures/eval_repos/ruby_minimal/spec/models
```

- [ ] **Step 2: Create `Gemfile`**

`tests/fixtures/eval_repos/ruby_minimal/Gemfile`:

```ruby
source "https://rubygems.org"
gem "rails", "~> 7.0"
```

- [ ] **Step 3: Create 5 model files**

Each model is an `ApplicationRecord` subclass with one association — uniform AST shape.

`tests/fixtures/eval_repos/ruby_minimal/app/models/user.rb`:

```ruby
class User < ApplicationRecord
  has_many :posts
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/models/post.rb`:

```ruby
class Post < ApplicationRecord
  belongs_to :user
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/models/comment.rb`:

```ruby
class Comment < ApplicationRecord
  belongs_to :post
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/models/tag.rb`:

```ruby
class Tag < ApplicationRecord
  has_many :taggings
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/models/tagging.rb`:

```ruby
class Tagging < ApplicationRecord
  belongs_to :tag
end
```

- [ ] **Step 4: Create 5 controller files**

Each controller subclasses `ApplicationController` with a single `index` action.

`tests/fixtures/eval_repos/ruby_minimal/app/controllers/users_controller.rb`:

```ruby
class UsersController < ApplicationController
  def index
    @users = User.all
  end
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/controllers/posts_controller.rb`:

```ruby
class PostsController < ApplicationController
  def index
    @posts = Post.all
  end
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/controllers/comments_controller.rb`:

```ruby
class CommentsController < ApplicationController
  def index
    @comments = Comment.all
  end
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/controllers/tags_controller.rb`:

```ruby
class TagsController < ApplicationController
  def index
    @tags = Tag.all
  end
end
```

`tests/fixtures/eval_repos/ruby_minimal/app/controllers/taggings_controller.rb`:

```ruby
class TaggingsController < ApplicationController
  def index
    @taggings = Tagging.all
  end
end
```

- [ ] **Step 5: Create 5 spec files**

Each spec is a top-level `RSpec.describe` block.

`tests/fixtures/eval_repos/ruby_minimal/spec/models/user_spec.rb`:

```ruby
require "rails_helper"

RSpec.describe User, type: :model do
  it { is_expected.to have_many(:posts) }
end
```

`tests/fixtures/eval_repos/ruby_minimal/spec/models/post_spec.rb`:

```ruby
require "rails_helper"

RSpec.describe Post, type: :model do
  it { is_expected.to belong_to(:user) }
end
```

`tests/fixtures/eval_repos/ruby_minimal/spec/models/comment_spec.rb`:

```ruby
require "rails_helper"

RSpec.describe Comment, type: :model do
  it { is_expected.to belong_to(:post) }
end
```

`tests/fixtures/eval_repos/ruby_minimal/spec/models/tag_spec.rb`:

```ruby
require "rails_helper"

RSpec.describe Tag, type: :model do
  it { is_expected.to have_many(:taggings) }
end
```

`tests/fixtures/eval_repos/ruby_minimal/spec/models/tagging_spec.rb`:

```ruby
require "rails_helper"

RSpec.describe Tagging, type: :model do
  it { is_expected.to belong_to(:tag) }
end
```

- [ ] **Step 6: Verify directory contents**

```bash
find tests/fixtures/eval_repos/ruby_minimal -type f | sort
```

Expected output (16 files): `Gemfile` plus the 15 source files in their respective dirs.

- [ ] **Step 7: Commit source files**

```bash
git add tests/fixtures/eval_repos/ruby_minimal/
git commit -m "Add ruby_minimal eval-fixture source files

15 Rails-shaped source files plus Gemfile, structured to form 3
dense archetypes (5 model, 5 controller, 5 spec)."
```

---

## Task 5: Bootstrap `ruby_minimal` and commit `.chameleon/`

**Files:**

- Create: `tests/fixtures/eval_repos/ruby_minimal/.chameleon/` (entire dir generated)

- [ ] **Step 1: Bootstrap the fixture**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python -c '
from chameleon_mcp.tools import bootstrap_repo
import json, sys
result = bootstrap_repo(
    "/Users/crisn/Documents/Projects/chameleon/tests/fixtures/eval_repos/ruby_minimal",
    now=1700000000.0,
)
print(json.dumps(result["data"], indent=2))
sys.exit(0 if result["data"].get("status") == "success" else 1)
'
```

Expected: `"status": "success"`, 3 archetypes detected.

- [ ] **Step 2: Log archetype names**

```bash
python -c '
import json
data = json.load(open("tests/fixtures/eval_repos/ruby_minimal/.chameleon/archetypes.json"))
for name in sorted(data.get("archetypes", {}).keys()):
    print(name)
' > /tmp/ruby_minimal_archetype_names.txt
cat /tmp/ruby_minimal_archetype_names.txt
```

Expected: exactly 3 lines.

- [ ] **Step 3: Verify the `COMMITTED` sentinel exists**

```bash
ls tests/fixtures/eval_repos/ruby_minimal/.chameleon/
```

Expected: `COMMITTED`, `archetypes.json`, `canonicals.json`, `idioms.md`, `profile.json`, `rules.json`.

- [ ] **Step 4: Commit `.chameleon/`**

```bash
git add tests/fixtures/eval_repos/ruby_minimal/.chameleon
git commit -m "Bootstrap ruby_minimal fixture profile (pinned now=1700000000)

3 archetypes detected from the 15-file Rails fixture."
```

---

## Task 6: Write runner skeleton (TDD)

**Files:**

- Create: `tests/hook_evals/runner.py`
- Create: `tests/hook_evals/runner_test.py`
- Create: `tests/hook_evals/__init__.py` (empty file)
- Create: `tests/hook_evals/scenarios/.gitkeep` (empty file so dir exists)

- [ ] **Step 1: Create directories and write failing assertion test**

```bash
mkdir -p tests/hook_evals/scenarios/ts
mkdir -p tests/hook_evals/scenarios/ruby
mkdir -p tests/hook_evals/scenarios/cross
touch tests/hook_evals/__init__.py
touch tests/hook_evals/scenarios/.gitkeep
```

Create `tests/hook_evals/runner_test.py`:

```python
"""Unit tests for tests/hook_evals/runner.py.

The runner itself runs scenario JSON against get_pattern_context. These
tests verify the runner's own logic without depending on real fixtures.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import assert_scenario, ScenarioResult


class AssertScenarioTest(unittest.TestCase):
    def _response(self, **overrides):
        base = {
            "data": {
                "repo": {
                    "id": "fake",
                    "profile_status": "profile_present",
                    "trust_state": "trusted",
                },
                "archetype": {"archetype": "utility_cluster_abc"},
                "canonical_excerpt": {"text": "export const x = 1;"},
                "rules": [["no-default-export", "Avoid default exports"]],
                "idioms": "Use named exports.",
            }
        }
        base["data"].update(overrides)
        return base

    def test_archetype_match_passes(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {"archetype_name": "utility_cluster_abc"},
        }
        result = assert_scenario(scenario, self._response())
        self.assertEqual(result.status, "PASS")

    def test_archetype_mismatch_fails(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {"archetype_name": "definitely_not_real_archetype"},
        }
        result = assert_scenario(scenario, self._response())
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("archetype" in m for m in result.mismatches))

    def test_canonical_substring_match(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {
                "archetype_name": "utility_cluster_abc",
                "canonical_excerpt_includes": ["export const"],
            },
        }
        result = assert_scenario(scenario, self._response())
        self.assertEqual(result.status, "PASS")

    def test_schema_rot_detection(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {"archetype_name": "utility_cluster_abc"},
        }
        result = assert_scenario(
            scenario,
            self._response(repo={"id": "x", "profile_status": "profile_corrupted", "trust_state": "trusted"}),
        )
        self.assertEqual(result.status, "SCHEMA_ROT")
        self.assertIn("refresh_eval_fixtures", " ".join(result.mismatches))


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))
    print(f"\nSummary: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errored")
    sys.exit(0 if result.wasSuccessful() else 1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner_test.py
```

Expected: FAIL with `ImportError: cannot import name 'assert_scenario' from 'runner'`.

- [ ] **Step 3: Implement minimal `runner.py`**

Create `tests/hook_evals/runner.py`:

```python
"""Hook eval scenario runner.

See docs/superpowers/specs/2026-05-12-hook-evals-design.md.

Default mode: calls chameleon_mcp.tools.get_pattern_context in-process.
--full mode: pipes a synthetic PreToolUse event through hooks/preflight-and-advise.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "eval_repos"
HOOK_SCRIPT = REPO_ROOT / "hooks" / "preflight-and-advise"
SCENARIOS_DIR = REPO_ROOT / "tests" / "hook_evals" / "scenarios"
HOOK_ERROR_LOG = Path.home() / ".local" / "share" / "chameleon" / ".hook_errors.log"


@dataclass
class ScenarioResult:
    name: str
    status: str  # PASS | FAIL | SCHEMA_ROT | HOOK_FAILED | ERROR
    mismatches: list[str] = field(default_factory=list)


def assert_scenario(scenario: dict, response: dict) -> ScenarioResult:
    """Assert a get_pattern_context response matches a scenario's `expected`.

    Returns a ScenarioResult. Does not raise.
    """
    name = scenario["name"]
    data = response.get("data", {}) or {}
    repo = data.get("repo", {}) or {}
    expected = scenario.get("expected", {}) or {}
    mismatches: list[str] = []

    profile_status = repo.get("profile_status")
    if profile_status == "profile_corrupted":
        return ScenarioResult(
            name=name,
            status="SCHEMA_ROT",
            mismatches=[
                "Fixture profile is unloadable. Run scripts/refresh_eval_fixtures.sh to regenerate."
            ],
        )

    archetype_node = data.get("archetype") or {}
    actual_archetype = archetype_node.get("archetype") if isinstance(archetype_node, dict) else None

    expected_archetype = expected.get("archetype_name", "<unset>")
    if expected_archetype != "<unset>":
        if expected_archetype != actual_archetype:
            mismatches.append(
                f"archetype: expected {expected_archetype!r}, got {actual_archetype!r}"
            )

    expected_status = expected.get("profile_status")
    if expected_status is not None and expected_status != profile_status:
        mismatches.append(
            f"profile_status: expected {expected_status!r}, got {profile_status!r}"
        )

    expected_trust = expected.get("trust_state")
    if expected_trust is not None and expected_trust != repo.get("trust_state"):
        mismatches.append(
            f"trust_state: expected {expected_trust!r}, got {repo.get('trust_state')!r}"
        )

    canonical = data.get("canonical_excerpt") or {}
    canonical_text = canonical.get("text", "") if isinstance(canonical, dict) else ""
    for needle in expected.get("canonical_excerpt_includes", []) or []:
        if needle not in canonical_text:
            mismatches.append(
                f"canonical_excerpt missing substring {needle!r}"
            )

    rules_pairs = data.get("rules") or []
    rules_text = "\n".join(f"{k}: {v}" for k, v in rules_pairs)
    for needle in expected.get("rules_must_include_substring", []) or []:
        if needle not in rules_text:
            mismatches.append(f"rules missing substring {needle!r}")
    for forbidden in expected.get("rules_must_not_include_substring", []) or []:
        if forbidden in rules_text:
            mismatches.append(f"rules unexpectedly contains substring {forbidden!r}")

    idioms_text = data.get("idioms", "") or ""
    for needle in expected.get("idioms_must_include_substring", []) or []:
        if needle not in idioms_text:
            mismatches.append(f"idioms missing substring {needle!r}")

    return ScenarioResult(
        name=name,
        status="PASS" if not mismatches else "FAIL",
        mismatches=mismatches,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="run scenarios through hooks/preflight-and-advise")
    args = parser.parse_args(argv)

    # Placeholder: scenario discovery + execution lands in Task 7.
    print(json.dumps({"status": "not_implemented", "full": args.full}, indent=2))
    print("Summary: 0 run, 0 passed, 0 failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner_test.py
```

Expected: PASS for all 4 tests, exit 0, prints `Summary: 4 run, 0 failed, 0 errored`.

- [ ] **Step 5: Commit**

```bash
git add tests/hook_evals/__init__.py tests/hook_evals/runner.py tests/hook_evals/runner_test.py tests/hook_evals/scenarios/.gitkeep
git commit -m "Add hook-evals runner skeleton with assert_scenario + unit tests

Implements the per-scenario assertion logic for archetype name,
profile_status, trust_state, canonical/rules/idioms substrings, and
the SCHEMA_ROT branch. Discovery and main loop land in Task 7."
```

---

## Task 7: Implement scenario discovery and MCP-mode main loop

**Files:**

- Modify: `tests/hook_evals/runner.py`
- Modify: `tests/hook_evals/runner_test.py`

- [ ] **Step 1: Add failing test for `discover_scenarios`**

Append to `tests/hook_evals/runner_test.py` (above the `if __name__` block):

```python
class DiscoverScenariosTest(unittest.TestCase):
    def test_discover_returns_sorted_scenarios(self):
        from runner import discover_scenarios
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ts").mkdir()
            (root / "ruby").mkdir()
            (root / "ts" / "02-b.json").write_text('{"name": "b"}')
            (root / "ts" / "01-a.json").write_text('{"name": "a"}')
            (root / "ruby" / "01-c.json").write_text('{"name": "c"}')

            found = discover_scenarios(root)
            names = [s["name"] for s in found]
            self.assertEqual(names, ["c", "a", "b"])
```

Add `import tempfile` and `from pathlib import Path` at the top of the test file if not already present.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner_test.py
```

Expected: FAIL with `ImportError: cannot import name 'discover_scenarios' from 'runner'`.

- [ ] **Step 3: Add `discover_scenarios` and the MCP execution loop to `runner.py`**

Add these functions to `tests/hook_evals/runner.py` (above `main`):

```python
def discover_scenarios(root: Path) -> list[dict]:
    """Glob scenarios/**/*.json, sorted lexicographically."""
    paths = sorted(glob.glob(str(root / "**" / "*.json"), recursive=True))
    scenarios = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        obj["_source_path"] = p
        scenarios.append(obj)
    return scenarios


def _synthesize_no_profile_marker(repo_tmp: Path, file_path: str) -> None:
    """For fixture_repo: null, drop a language marker so find_repo_root resolves."""
    ext = Path(file_path).suffix.lower()
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        (repo_tmp / "package.json").write_text("{}")
    elif ext == ".rb":
        (repo_tmp / "Gemfile").write_text("source 'https://rubygems.org'\n")


def _apply_trust_state(repo_tmp: Path, trust_state: str) -> None:
    """Per-scenario trust setup. Assumes CHAMELEON_PLUGIN_DATA is already set."""
    if trust_state in ("untrusted", "n/a"):
        return
    from chameleon_mcp.tools import trust_profile
    trust_profile(str(repo_tmp), repo_tmp.name)
    if trust_state == "stale":
        # Append a byte to profile.json to flip is_material_change on next load.
        profile_path = repo_tmp / ".chameleon" / "profile.json"
        with open(profile_path, "ab") as f:
            f.write(b" ")


def run_scenario_mcp(scenario: dict) -> ScenarioResult:
    """Run one scenario through get_pattern_context (MCP layer)."""
    from chameleon_mcp.tools import get_pattern_context

    fixture_repo = scenario.get("fixture_repo")
    file_path = scenario["file_path"]
    file_content = scenario.get("file_content", "")
    trust_state = scenario.get("trust_state", "trusted")
    name = scenario["name"]

    with tempfile.TemporaryDirectory() as repo_tmp_str, tempfile.TemporaryDirectory() as data_tmp_str:
        repo_tmp = Path(repo_tmp_str)
        os.environ["CHAMELEON_PLUGIN_DATA"] = data_tmp_str
        try:
            if fixture_repo is not None:
                src = FIXTURES_DIR / fixture_repo
                if not src.is_dir():
                    return ScenarioResult(
                        name=name,
                        status="ERROR",
                        mismatches=[f"fixture not found: {src}"],
                    )
                shutil.copytree(src, repo_tmp, dirs_exist_ok=True)
            else:
                _synthesize_no_profile_marker(repo_tmp, file_path)

            _apply_trust_state(repo_tmp, trust_state)

            target = repo_tmp / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")

            response = get_pattern_context(str(target))
            return assert_scenario(scenario, response)
        except Exception as exc:
            return ScenarioResult(name=name, status="ERROR", mismatches=[repr(exc)])
        finally:
            os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
```

Then replace the `main` function body with:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="run scenarios through hooks/preflight-and-advise")
    args = parser.parse_args(argv)

    scenarios = discover_scenarios(SCENARIOS_DIR)
    results: list[ScenarioResult] = []

    for scenario in scenarios:
        if args.full:
            result = run_scenario_full(scenario)
        else:
            result = run_scenario_mcp(scenario)
        results.append(result)

        line_color_ok = "PASS"
        if result.status != "PASS":
            line_color_ok = result.status
        sys.stderr.write(f"[{line_color_ok}] {scenario['name']}\n")
        for m in result.mismatches:
            sys.stderr.write(f"    {m}\n")

    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status != "PASS")
    summary = {
        "mode": "full" if args.full else "mcp",
        "scenarios_run": len(results),
        "passed": passed,
        "failed": failed,
        "results": [
            {"name": r.name, "status": r.status, "mismatches": r.mismatches}
            for r in results
        ],
    }
    print(json.dumps(summary, indent=2))
    print(f"Summary: {len(results)} run, {passed} passed, {failed} failed", file=sys.stderr)
    return 0 if failed == 0 else 1


def run_scenario_full(scenario: dict) -> ScenarioResult:
    """Stub: --full mode lands in Task 11."""
    return ScenarioResult(
        name=scenario["name"],
        status="ERROR",
        mismatches=["--full mode not implemented yet"],
    )
```

- [ ] **Step 4: Run unit tests to verify they pass**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner_test.py
```

Expected: PASS for all 5 tests now (4 prior + 1 new), exit 0.

- [ ] **Step 5: Smoke-run the runner (zero scenarios)**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Expected: stdout shows JSON with `"scenarios_run": 0`, stderr shows `Summary: 0 run, 0 passed, 0 failed`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add tests/hook_evals/runner.py tests/hook_evals/runner_test.py
git commit -m "Implement scenario discovery + MCP-mode runner

Threads scenarios through assert_scenario with per-scenario tmpdir
isolation via CHAMELEON_PLUGIN_DATA. --full mode stub returns ERROR;
implementation lands in Task 11."
```

---

## Task 8: Write TS seed scenarios

**Files:**

- Create: `tests/hook_evals/scenarios/ts/01-utility-export.json`
- Create: `tests/hook_evals/scenarios/ts/02-component.json`
- Create: `tests/hook_evals/scenarios/ts/03-type-only.json`
- Create: `tests/hook_evals/scenarios/ts/04-utility-rules-substring.json`
- Create: `tests/hook_evals/scenarios/ts/05-negative-no-match.json`

- [ ] **Step 1: Identify the 3 TS archetype names**

```bash
cat /tmp/ts_minimal_archetype_names.txt
```

Note them down. Three archetypes are expected. By naming convention they will hash-suffix differently but include path-pattern hints. Identify which one corresponds to:

- The `src/utils/` bucket (call it `<TS_UTIL>`)
- The `src/components/` bucket (call it `<TS_COMPONENT>`)
- The `src/types/` bucket (call it `<TS_TYPE>`)

Verify by reading `tests/fixtures/eval_repos/ts_minimal/.chameleon/canonicals.json` and matching each archetype's `paths_pattern` to a directory.

- [ ] **Step 2: Write scenario 01 (utility happy path)**

`tests/hook_evals/scenarios/ts/01-utility-export.json`:

```json
{
  "name": "ts: new utility file resolves to utility archetype",
  "description": "A file matching the src/utils/ pattern should match the utility cluster archetype.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/utils/format_currency.ts",
  "file_content": "export const formatCurrency = (n: number): string => `$${n.toFixed(2)}`;\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<TS_UTIL>",
    "canonical_excerpt_includes": ["export const"]
  }
}
```

Replace `<TS_UTIL>` with the actual archetype name from Step 1.

- [ ] **Step 3: Write scenario 02 (component)**

`tests/hook_evals/scenarios/ts/02-component.json`:

```json
{
  "name": "ts: new component resolves to component archetype",
  "description": "A PascalCase JSX-returning file under src/components/ should match the component archetype.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/components/Tooltip.tsx",
  "file_content": "export const Tooltip = (props: { text: string }) => <span>{props.text}</span>;\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<TS_COMPONENT>",
    "canonical_excerpt_includes": ["<"]
  }
}
```

Replace `<TS_COMPONENT>` with the actual name.

- [ ] **Step 4: Write scenario 03 (type-only)**

`tests/hook_evals/scenarios/ts/03-type-only.json`:

```json
{
  "name": "ts: new type-only file resolves to type archetype",
  "description": "A file containing only `export type ...` under src/types/ should match the type-only archetype.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/types/notification.ts",
  "file_content": "export type Notification = { id: string; read: boolean };\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<TS_TYPE>",
    "canonical_excerpt_includes": ["export type"]
  }
}
```

Replace `<TS_TYPE>` with the actual name.

- [ ] **Step 5: Write scenario 04 (rules substring on a utility file)**

`tests/hook_evals/scenarios/ts/04-utility-rules-substring.json`:

```json
{
  "name": "ts: utility file rules emit known key",
  "description": "Confirms the rules list is exposed and matchable. Uses an empty substring so it always matches if rules is a list at all.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/utils/format_email.ts",
  "file_content": "export const formatEmail = (e: string): string => e.trim().toLowerCase();\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<TS_UTIL>",
    "rules_must_include_substring": [":"]
  }
}
```

Replace `<TS_UTIL>`. The `":"` substring matches any `f"{key}: {value}"` formatting; this scenario guards against the rules pipeline becoming empty.

- [ ] **Step 6: Write scenario 05 (negative)**

`tests/hook_evals/scenarios/ts/05-negative-no-match.json`:

```json
{
  "name": "ts: file in an unknown directory does not match any archetype",
  "description": "A path that does not fall under any seeded archetype bucket should resolve to null.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/unknown/_one_off.ts",
  "file_content": "export const oneOff = 42;\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": null
  }
}
```

- [ ] **Step 7: Run the runner and verify scenarios pass**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Expected: all 5 TS scenarios PASS in stderr, summary shows `5 run, 5 passed, 0 failed`, exit 0.

If a scenario fails:

- `SCHEMA_ROT` means the fixture profile is unloadable — run `scripts/refresh_eval_fixtures.sh --apply` if it exists, otherwise re-run Task 3.
- Mismatch on `archetype` means the archetype name in the scenario does not match what chameleon produced. Re-inspect `archetypes.json` and update the scenario.
- Mismatch on `canonical_excerpt_includes` means the substring is sanitized away or never appeared. Inspect `canonicals.json` and pick a more universal substring.

- [ ] **Step 8: Commit**

```bash
git add tests/hook_evals/scenarios/ts/
git commit -m "Add 5 TS hook-eval scenarios (utility, component, type, rules, negative)"
```

---

## Task 9: Write Ruby seed scenarios

**Files:**

- Create: `tests/hook_evals/scenarios/ruby/01-model.json`
- Create: `tests/hook_evals/scenarios/ruby/02-controller.json`
- Create: `tests/hook_evals/scenarios/ruby/03-spec.json`
- Create: `tests/hook_evals/scenarios/ruby/04-model-rules-substring.json`
- Create: `tests/hook_evals/scenarios/ruby/05-negative-no-match.json`

- [ ] **Step 1: Identify the 3 Ruby archetype names**

```bash
cat /tmp/ruby_minimal_archetype_names.txt
```

Map each to:

- `app/models/` bucket → `<RB_MODEL>`
- `app/controllers/` bucket → `<RB_CONTROLLER>`
- `spec/models/` bucket → `<RB_SPEC>`

Verify against `tests/fixtures/eval_repos/ruby_minimal/.chameleon/canonicals.json`.

- [ ] **Step 2: Write scenario 01 (model happy path)**

`tests/hook_evals/scenarios/ruby/01-model.json`:

```json
{
  "name": "ruby: new model resolves to model archetype",
  "description": "An ApplicationRecord subclass under app/models/ should match the model archetype.",
  "fixture_repo": "ruby_minimal",
  "file_path": "app/models/article.rb",
  "file_content": "class Article < ApplicationRecord\n  belongs_to :user\nend\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<RB_MODEL>",
    "canonical_excerpt_includes": ["ApplicationRecord"]
  }
}
```

- [ ] **Step 3: Write scenario 02 (controller)**

`tests/hook_evals/scenarios/ruby/02-controller.json`:

```json
{
  "name": "ruby: new controller resolves to controller archetype",
  "description": "An ApplicationController subclass under app/controllers/ should match the controller archetype.",
  "fixture_repo": "ruby_minimal",
  "file_path": "app/controllers/articles_controller.rb",
  "file_content": "class ArticlesController < ApplicationController\n  def index\n    @articles = Article.all\n  end\nend\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<RB_CONTROLLER>",
    "canonical_excerpt_includes": ["ApplicationController"]
  }
}
```

- [ ] **Step 4: Write scenario 03 (spec)**

`tests/hook_evals/scenarios/ruby/03-spec.json`:

```json
{
  "name": "ruby: new spec resolves to spec archetype",
  "description": "An RSpec.describe block under spec/models/ should match the spec archetype.",
  "fixture_repo": "ruby_minimal",
  "file_path": "spec/models/article_spec.rb",
  "file_content": "require \"rails_helper\"\n\nRSpec.describe Article, type: :model do\n  it { is_expected.to belong_to(:user) }\nend\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<RB_SPEC>",
    "canonical_excerpt_includes": ["RSpec.describe"]
  }
}
```

- [ ] **Step 5: Write scenario 04 (rules substring)**

`tests/hook_evals/scenarios/ruby/04-model-rules-substring.json`:

```json
{
  "name": "ruby: model rules emit known key",
  "description": "Guards against the rules pipeline regressing to empty for the model archetype.",
  "fixture_repo": "ruby_minimal",
  "file_path": "app/models/photo.rb",
  "file_content": "class Photo < ApplicationRecord\n  belongs_to :user\nend\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": "<RB_MODEL>",
    "rules_must_include_substring": [":"]
  }
}
```

- [ ] **Step 6: Write scenario 05 (negative)**

`tests/hook_evals/scenarios/ruby/05-negative-no-match.json`:

```json
{
  "name": "ruby: file in an unknown directory does not match any archetype",
  "description": "A path that does not fall under any seeded archetype bucket should resolve to null.",
  "fixture_repo": "ruby_minimal",
  "file_path": "lib/unknown/_one_off.rb",
  "file_content": "module Unknown\nend\n",
  "trust_state": "trusted",
  "expected": {
    "archetype_name": null
  }
}
```

- [ ] **Step 7: Run the runner and verify**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Expected: 10 scenarios PASS (5 TS + 5 Ruby), 0 fail.

- [ ] **Step 8: Commit**

```bash
git add tests/hook_evals/scenarios/ruby/
git commit -m "Add 5 Ruby hook-eval scenarios (model, controller, spec, rules, negative)"
```

---

## Task 10: Write cross-cutting scenarios

**Files:**

- Create: `tests/hook_evals/scenarios/cross/01-untrusted-profile.json`
- Create: `tests/hook_evals/scenarios/cross/02-stale-profile.json`
- Create: `tests/hook_evals/scenarios/cross/03-no-profile.json`

- [ ] **Step 1: Untrusted scenario**

`tests/hook_evals/scenarios/cross/01-untrusted-profile.json`:

```json
{
  "name": "cross: untrusted profile surfaces trust_state in response",
  "description": "When the profile is present but not trusted, get_pattern_context returns the archetype but trust_state == 'untrusted'.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/utils/format_phone.ts",
  "file_content": "export const formatPhone = (s: string): string => s;\n",
  "trust_state": "untrusted",
  "expected": {
    "trust_state": "untrusted"
  }
}
```

- [ ] **Step 2: Stale scenario**

`tests/hook_evals/scenarios/cross/02-stale-profile.json`:

```json
{
  "name": "cross: stale profile surfaces trust_state == 'stale'",
  "description": "After trust_profile + a byte appended to profile.json, the next get_pattern_context call should detect material change and surface trust_state 'stale'.",
  "fixture_repo": "ts_minimal",
  "file_path": "src/utils/format_address.ts",
  "file_content": "export const formatAddress = (s: string): string => s;\n",
  "trust_state": "stale",
  "expected": {
    "trust_state": "stale"
  }
}
```

- [ ] **Step 3: No-profile scenario (TS marker only, no .chameleon/)**

`tests/hook_evals/scenarios/cross/03-no-profile.json`:

```json
{
  "name": "cross: repo with no .chameleon directory surfaces no_profile",
  "description": "Runner synthesizes a tmpdir with a package.json marker so find_repo_root resolves but no profile is found.",
  "fixture_repo": null,
  "file_path": "src/utils/whatever.ts",
  "file_content": "export const whatever = 1;\n",
  "trust_state": "untrusted",
  "expected": {
    "profile_status": "no_profile",
    "archetype_name": null
  }
}
```

- [ ] **Step 4: Run the runner and verify**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Expected: 13 scenarios PASS (5 TS + 5 Ruby + 3 cross), exit 0.

If the no-profile scenario reports `no_repo` instead of `no_profile`, verify the runner's `_synthesize_no_profile_marker` correctly writes `package.json`. The `.ts` extension should hit the TS branch.

If the stale scenario fails, ensure `_apply_trust_state` is appending to the correct file relative to `repo_tmp`.

- [ ] **Step 5: Commit**

```bash
git add tests/hook_evals/scenarios/cross/
git commit -m "Add 3 cross-cutting hook-eval scenarios (untrusted, stale, no-profile)"
```

---

## Task 11: Implement `--full` mode

**Files:**

- Modify: `tests/hook_evals/runner.py`
- Modify: `tests/hook_evals/runner_test.py`

- [ ] **Step 1: Add failing unit test for the capability check**

Append to `tests/hook_evals/runner_test.py`:

```python
class FullModeCapabilityTest(unittest.TestCase):
    def test_capability_check_passes_in_this_repo(self):
        from runner import full_mode_capability_check
        ok, reason = full_mode_capability_check()
        self.assertTrue(ok, f"capability check failed: {reason}")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner_test.py
```

Expected: FAIL with `ImportError: cannot import name 'full_mode_capability_check' from 'runner'`.

- [ ] **Step 3: Replace the `run_scenario_full` stub in `tests/hook_evals/runner.py`**

Replace the stub with this implementation:

```python
def full_mode_capability_check() -> tuple[bool, str]:
    """Return (ok, reason). All three must be present for --full mode."""
    if shutil.which("bash") is None:
        return False, "bash not on PATH"
    if not HOOK_SCRIPT.is_file() or not os.access(HOOK_SCRIPT, os.X_OK):
        return False, f"hook script missing or not executable: {HOOK_SCRIPT}"
    venv_python = REPO_ROOT / "mcp" / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return False, f"mcp venv python missing: {venv_python}"
    return True, "ok"


def _read_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def run_scenario_full(scenario: dict) -> ScenarioResult:
    """Pipe a synthetic PreToolUse event through hooks/preflight-and-advise."""
    fixture_repo = scenario.get("fixture_repo")
    file_path = scenario["file_path"]
    file_content = scenario.get("file_content", "")
    trust_state = scenario.get("trust_state", "trusted")
    name = scenario["name"]

    with tempfile.TemporaryDirectory() as repo_tmp_str, tempfile.TemporaryDirectory() as data_tmp_str:
        repo_tmp = Path(repo_tmp_str)
        os.environ["CHAMELEON_PLUGIN_DATA"] = data_tmp_str
        try:
            if fixture_repo is not None:
                src = FIXTURES_DIR / fixture_repo
                shutil.copytree(src, repo_tmp, dirs_exist_ok=True)
            else:
                _synthesize_no_profile_marker(repo_tmp, file_path)

            _apply_trust_state(repo_tmp, trust_state)

            target = repo_tmp / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")

            event = {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(target)},
                "session_id": "hook_evals",
            }

            log_mtime_before = _read_mtime(HOOK_ERROR_LOG)

            proc = subprocess.run(
                ["bash", str(HOOK_SCRIPT)],
                input=json.dumps(event).encode("utf-8"),
                capture_output=True,
                env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
                timeout=10,
            )

            log_mtime_after = _read_mtime(HOOK_ERROR_LOG)
            if log_mtime_before != log_mtime_after:
                return ScenarioResult(
                    name=name,
                    status="HOOK_FAILED",
                    mismatches=[
                        f"hook fail-opened; .hook_errors.log grew (stderr tail: {proc.stderr.decode('utf-8', 'replace')[-400:]})"
                    ],
                )

            try:
                hook_out = json.loads(proc.stdout.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return ScenarioResult(
                    name=name,
                    status="ERROR",
                    mismatches=[f"hook stdout was not JSON: {exc!r}"],
                )

            advisory_text = ""
            hook_specific = hook_out.get("hookSpecificOutput")
            if isinstance(hook_specific, dict):
                advisory_text = hook_specific.get("additionalContext", "")
            if not advisory_text:
                advisory_text = hook_out.get("additionalContext", "")

            # Hook output is a single string blob. Convert to a synthetic
            # response shape so assert_scenario can run substring checks
            # against canonical/idioms. Archetype assertions in --full mode
            # use a contains-check against the blob.
            synthetic = {
                "data": {
                    "repo": {"id": "full", "profile_status": "profile_present", "trust_state": "trusted"},
                    "archetype": {"archetype": "__full_mode_blob__"},
                    "canonical_excerpt": {"text": advisory_text},
                    "rules": [],
                    "idioms": advisory_text,
                }
            }
            # Treat expected.archetype_name as a substring check in --full mode.
            expected = scenario.get("expected", {})
            mismatches = []
            expected_arch = expected.get("archetype_name")
            if expected_arch is not None:
                if expected_arch not in advisory_text:
                    mismatches.append(
                        f"advisory blob missing archetype hint {expected_arch!r}"
                    )
            elif expected_arch is None and "archetype_name" in expected and advisory_text:
                # Negative scenario expects no advisory at all.
                mismatches.append("expected no advisory but blob is non-empty")
            for needle in expected.get("canonical_excerpt_includes", []) or []:
                if needle not in advisory_text:
                    mismatches.append(f"advisory blob missing substring {needle!r}")

            if mismatches:
                return ScenarioResult(name=name, status="FAIL", mismatches=mismatches)
            return ScenarioResult(name=name, status="PASS")
        except subprocess.TimeoutExpired:
            return ScenarioResult(name=name, status="HOOK_FAILED", mismatches=["hook timed out"])
        except Exception as exc:
            return ScenarioResult(name=name, status="ERROR", mismatches=[repr(exc)])
        finally:
            os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
```

Also update the `main` function to short-circuit on missing capabilities. Replace the `for scenario in scenarios:` block start with:

```python
    if args.full:
        ok, reason = full_mode_capability_check()
        if not ok:
            print(json.dumps({"status": "skipped", "reason": reason}, indent=2))
            print(f"Summary: 0 run, 0 passed, 0 failed (skipped: {reason})", file=sys.stderr)
            return 0

    for scenario in scenarios:
```

- [ ] **Step 4: Run unit tests**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner_test.py
```

Expected: all 6 tests PASS, exit 0.

- [ ] **Step 5: Run runner in `--full` mode**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py --full
```

Expected: 13 scenarios run. Some may FAIL on archetype-name substring match because the hook emits the cluster name in a longer prose block. Each FAIL should print a clear `advisory blob missing archetype hint <name>` line. The exit code is 1 if any fail.

This is acceptable: `--full` mode is opt-in and validates plumbing, not exact archetype names (the MCP-mode runner already does that). For now, accept FAILs that are clearly substring misses and move on.

- [ ] **Step 6: Run MCP mode again to confirm no regression**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Expected: all 13 scenarios PASS, exit 0.

- [ ] **Step 7: Commit**

```bash
git add tests/hook_evals/runner.py tests/hook_evals/runner_test.py
git commit -m "Implement --full mode: hook subprocess + capability check + fail-open guard

Pipes synthetic PreToolUse events through hooks/preflight-and-advise.
Detects fail-open by watching .hook_errors.log mtime (real path
~/.local/share/chameleon, hardcoded in the bash hooks; CHAMELEON_PLUGIN_DATA
does not redirect that file). Capability-based skip when bash, the hook,
or the venv python is missing."
```

---

## Task 12: Write `scripts/refresh_eval_fixtures.sh`

**Files:**

- Create: `scripts/refresh_eval_fixtures.sh`

- [ ] **Step 1: Write the script**

`scripts/refresh_eval_fixtures.sh`:

```bash
#!/usr/bin/env bash
# Re-bootstrap eval-fixture profiles with a pinned `now` for deterministic
# witness selection. Default mode is --check (dry run); --apply writes.
#
# Usage:
#   scripts/refresh_eval_fixtures.sh            # check, exit non-zero if diff
#   scripts/refresh_eval_fixtures.sh --check    # same
#   scripts/refresh_eval_fixtures.sh --apply    # write the regenerated .chameleon/
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINNED_NOW=1700000000.0
MODE="check"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check) MODE="check" ;;
        --apply) MODE="apply" ;;
        -h|--help)
            grep -E "^# " "$0" | sed 's/^# //'
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
    shift
done

FIXTURES=(
    "tests/fixtures/eval_repos/ts_minimal"
    "tests/fixtures/eval_repos/ruby_minimal"
)

cd "${REPO_ROOT}/mcp"

DIRTY=0

for fixture in "${FIXTURES[@]}"; do
    abs="${REPO_ROOT}/${fixture}"
    echo "==> ${fixture}"
    if [ ! -d "${abs}" ]; then
        echo "    missing fixture directory: ${abs}" >&2
        exit 1
    fi

    if [ "${MODE}" = "check" ]; then
        # Bootstrap into a tmpdir, diff against the checked-in profile.
        scratch="$(mktemp -d)"
        trap 'rm -rf "${scratch}"' EXIT
        cp -R "${abs}/." "${scratch}/"
        rm -rf "${scratch}/.chameleon"
        PYTHONPATH=.:../tests .venv/bin/python -c "
from chameleon_mcp.tools import bootstrap_repo
import sys, json
r = bootstrap_repo('${scratch}', now=${PINNED_NOW}, force=True)
if r['data'].get('status') != 'success':
    print(json.dumps(r['data'], indent=2))
    sys.exit(1)
"
        if ! diff -ruq "${abs}/.chameleon" "${scratch}/.chameleon" > /tmp/refresh_diff_$$.txt; then
            echo "    DIFF detected:" >&2
            cat /tmp/refresh_diff_$$.txt >&2
            DIRTY=1
        else
            echo "    clean"
        fi
        rm -f /tmp/refresh_diff_$$.txt
        rm -rf "${scratch}"
        trap - EXIT
    else
        # Apply mode: nuke .chameleon and re-bootstrap in place.
        rm -rf "${abs}/.chameleon"
        PYTHONPATH=.:../tests .venv/bin/python -c "
from chameleon_mcp.tools import bootstrap_repo
import sys, json
r = bootstrap_repo('${abs}', now=${PINNED_NOW}, force=True)
if r['data'].get('status') != 'success':
    print(json.dumps(r['data'], indent=2))
    sys.exit(1)
"
        echo "    regenerated"
    fi
done

if [ "${MODE}" = "check" ] && [ "${DIRTY}" -eq 1 ]; then
    echo "Refresh would change checked-in files. Run with --apply to commit." >&2
    exit 1
fi

echo "Summary: refresh ${MODE} complete"
exit 0
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/refresh_eval_fixtures.sh
```

- [ ] **Step 3: Run `--check` and verify clean state**

```bash
scripts/refresh_eval_fixtures.sh --check
```

Expected: prints `clean` for each fixture, ends with `Summary: refresh check complete`, exit 0. If it reports DIFF, the fixture profile in main is non-canonical — investigate (likely a profile-schema change since Task 3/5 ran).

- [ ] **Step 4: Smoke-test `--apply` does not change anything**

```bash
scripts/refresh_eval_fixtures.sh --apply
git status --short tests/fixtures/eval_repos/
```

Expected: `git status` shows no changes (the apply was a no-op because nothing drifted).

- [ ] **Step 5: Commit**

```bash
git add scripts/refresh_eval_fixtures.sh
git commit -m "Add scripts/refresh_eval_fixtures.sh (--check / --apply)

Regenerates eval-fixture .chameleon/ profiles with pinned now=1700000000
for deterministic witness selection. Default mode is --check (dry run);
exits non-zero if regeneration would alter the checked-in profile."
```

---

## Task 13: Integrate runner into `tests/run_all_orders.py`

**Files:**

- Modify: `tests/run_all_orders.py:17-23`

- [ ] **Step 1: Read the current TESTS list**

```bash
sed -n '17,30p' tests/run_all_orders.py
```

Confirm there are 5 entries today.

- [ ] **Step 2: Add the runner as the 6th entry**

In `tests/run_all_orders.py`, change the `TESTS = [...]` list from 5 entries to 6 by appending:

```python
    TESTS_DIR / "hook_evals" / "runner.py",
```

The full list becomes:

```python
TESTS = [
    TESTS_DIR / "smoke_test.py",
    TESTS_DIR / "comprehensive_test.py",
    TESTS_DIR / "bootstrap_mechanism_test.py",
    TESTS_DIR / "mcp_protocol_test.py",
    TESTS_DIR / "stubs_implemented_test.py",
    TESTS_DIR / "hook_evals" / "runner.py",
]
```

- [ ] **Step 3: Run the full ordered suite**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py
```

Expected: all 6 entries pass across all 3 randomized orders.

- [ ] **Step 4: Commit**

```bash
git add tests/run_all_orders.py
git commit -m "Run hook_evals/runner.py as 6th entry in run_all_orders

Ensures the MCP-mode eval scenarios run on every full test pass and
contributes to order-independence verification."
```

---

## Task 14: Write `tests/hook_evals/README.md`

**Files:**

- Create: `tests/hook_evals/README.md`

- [ ] **Step 1: Write the README**

`tests/hook_evals/README.md`:

```markdown
# tests/hook_evals

Deterministic synthetic-file scenario suite for chameleon's pattern advisory.

See `docs/superpowers/specs/2026-05-12-hook-evals-design.md` for the full design.

## Run

Default (MCP layer, fast, deterministic):

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Full hook plumbing (opt-in, exercises `hooks/preflight-and-advise`):

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py --full
```

`--full` mode silently skips when bash, the hook script, or the venv python is missing.

## Adding a scenario

1. Pick a fixture (`tests/fixtures/eval_repos/ts_minimal` or `ruby_minimal`).
2. Create `scenarios/<lang>/<NN>-<name>.json` with at minimum:
   ```json
   {
     "name": "short human label",
     "fixture_repo": "ts_minimal",
     "file_path": "src/utils/new.ts",
     "file_content": "...",
     "trust_state": "trusted",
     "expected": {"archetype_name": "<archetype-from-canonicals.json>"}
   }
   ```
3. Run the runner. The scenario passes when assertions match.

Keep new scenarios archetype-shaped (one per archetype), not bug-driven.

## Updating fixture profiles

After a chameleon profile-schema change:

```bash
scripts/refresh_eval_fixtures.sh --check    # dry run
scripts/refresh_eval_fixtures.sh --apply    # write
```

`--apply` regenerates `.chameleon/` with `now=1700000000` for deterministic witness selection. Commit the resulting diff.

## Internals

- `runner.py` discovers scenarios via `glob('scenarios/**/*.json')`, sorted.
- Each scenario gets its own tmpdir for repo and plugin-data, isolated via `CHAMELEON_PLUGIN_DATA`.
- `--full` mode pipes a synthetic PreToolUse event through `hooks/preflight-and-advise` and parses the advisory from `hookSpecificOutput.additionalContext` or top-level `additionalContext`.
- Fail-open detection watches `~/.local/share/chameleon/.hook_errors.log` (hardcoded path in the bash hooks; not redirectable via env var). `--full` mode appends one line to that log on hook failure, an accepted cost.
```

- [ ] **Step 2: Commit**

```bash
git add tests/hook_evals/README.md
git commit -m "Add tests/hook_evals/README.md"
```

---

## Task 15: Final verification pass

- [ ] **Step 1: Run full ordered suite**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/run_all_orders.py
```

Expected: all 6 suites pass across 3 random orders.

- [ ] **Step 2: Run the runner standalone (MCP mode)**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py
```

Expected: 13 scenarios PASS, exit 0, `Summary: 13 run, 13 passed, 0 failed` on stderr.

- [ ] **Step 3: Run `--full` mode**

```bash
cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/hook_evals/runner.py --full
```

Expected: capability check passes, scenarios run. Some FAILs on archetype-name substring are acceptable (see Task 11 Step 5). No HOOK_FAILED or ERROR statuses.

- [ ] **Step 4: Run `scripts/refresh_eval_fixtures.sh --check`**

```bash
scripts/refresh_eval_fixtures.sh --check
```

Expected: `clean` for both fixtures, exit 0.

- [ ] **Step 5: Confirm git state**

```bash
git log --oneline -15
```

Expected: ~14 new commits since the last spec commit (`0695fb1`), one per task plus any TDD intermediate commits.

```bash
git status --short
```

Expected: clean working tree.

- [ ] **Step 6: Self-check the runner is finding the fixtures correctly**

```bash
ls tests/fixtures/eval_repos/ts_minimal/.chameleon/
ls tests/fixtures/eval_repos/ruby_minimal/.chameleon/
```

Expected: both list `COMMITTED`, `archetypes.json`, `canonicals.json`, `idioms.md`, `profile.json`, `rules.json`.

---

## Out of scope (deferred per spec)

- Auto-generating scenarios from real-repo activity logs.
- Mutation testing for archetype renaming.
- A second-tier CI gate that runs `--full` mode on main but not every PR.
- Plumbing `now=` through `tools.refresh_repo` (Task 1 only threads `bootstrap_repo`; refresh script uses bootstrap directly with `force=True`).

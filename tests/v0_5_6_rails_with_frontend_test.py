"""Regression test for BUG-016: Rails-with-frontend recognizes all three
JS sidecar conventions.

Pre-v0.5.3 only ``app/javascript/`` was a Rails-with-frontend signal.
v0.5.3 (Bug E) added ``app/assets/javascripts/`` (gitlabhq's legacy
Rails 5 layout) and ``app/frontend/`` (Rails 7 / Vite-rails default).

The dogfood that surfaced this bug was running v0.5.2; the fix had
already shipped at HEAD when we wrote the bug report. This test
locks it in so a future signature refactor doesn't drop the gitlabhq
case again.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_rails_with_frontend_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_rwf_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


from chameleon_mcp.bootstrap.orchestrator import (  # noqa: E402
    _is_rails_with_frontend,
    _select_extractor,
)


def _mk_rails_root(td: str, js_layout: str) -> Path:
    root = Path(td)
    (root / "Gemfile").write_text('source "https://rubygems.org"\ngem "rails"\n')
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5"}}'
    )
    (root / "config").mkdir()
    (root / "config" / "application.rb").write_text(
        "require_relative 'boot'\nmodule App\n  class Application < Rails::Application\n  end\nend\n"
    )
    js_dir = root / js_layout
    js_dir.mkdir(parents=True)
    (js_dir / "main.js").write_text("console.log('hi');\n")
    # Ruby src so the rails-side bootstrap finds something
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "models").mkdir(exist_ok=True)
    for i in range(4):
        (root / "app" / "models" / f"user{i}.rb").write_text(
            f"class User{i} < ApplicationRecord\nend\n"
        )
    return root


def main() -> int:
    print("=== BUG-016: Rails-with-frontend covers app/javascript, app/assets/javascripts, app/frontend ===")

    for layout in (
        "app/javascript",
        "app/assets/javascripts",  # gitlabhq
        "app/frontend",
    ):
        with tempfile.TemporaryDirectory(prefix=f"bug016_{layout.replace('/', '_')}_") as td:
            root = _mk_rails_root(td, layout)
            t(
                f"_is_rails_with_frontend fires for {layout}",
                _is_rails_with_frontend(root),
                f"signal missed for {layout!r}",
            )
            ext = _select_extractor(root)
            t(
                f"_select_extractor returns Ruby for {layout}",
                ext is not None and ext.language == "ruby",
                f"got {ext.language if ext else None!r}",
            )

    # Negative: TS-only repo doesn't trip the signal
    with tempfile.TemporaryDirectory(prefix="bug016_neg_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        (root / "a.ts").write_text("export const x = 1;\n")
        t(
            "Pure TS repo is NOT Rails-with-frontend",
            not _is_rails_with_frontend(root),
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

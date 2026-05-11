"""Regression test for BUG-017: language_hint surfaces on misdetection.

Pre-v0.5.6 the language_hint envelope only populated when the Rails-
with-frontend detection FIRED (i.e., Ruby picked, app/javascript/ et al
detected). When that detection MISSED a Rails-with-frontend repo —
because the JS lived somewhere chameleon didn't yet recognize — TS won
silently and the user lost the safety net.

Now: when TS wins at the root AND a Gemfile is present AND >= 50 .rb
files exist, emit a reciprocal hint pointing the user at the Ruby
half.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_language_hint_misdetect_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_lang_hint_data_")
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


from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def _mk_rails_repo(td: Path, n_ruby: int = 60) -> None:
    """A Rails-ish repo where chameleon's frontend detection misses but
    TS deps live at the root."""
    (td / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5"}}'
    )
    (td / "tsconfig.json").write_text("{}")
    (td / "Gemfile").write_text(
        'source "https://rubygems.org"\ngem "rails"\n'
    )
    # NO app/javascript or app/assets/javascripts or app/frontend — so the
    # rails-with-frontend detector misses. Put Ruby files in a non-standard
    # layout so chameleon doesn't recognize this is Rails-with-frontend.
    src = td / "ruby_side"
    src.mkdir()
    for i in range(n_ruby):
        (src / f"file{i}.rb").write_text(
            f"# frozen_string_literal: true\nclass Foo{i}\n  def bar\n    {i}\n  end\nend\n"
        )
    ts_src = td / "src"
    ts_src.mkdir()
    for i in range(5):
        (ts_src / f"app{i}.ts").write_text(
            f"export const v{i} = {i};\n"
        )


def main() -> int:
    print("=== BUG-017: language_hint surfaces on TS-win + Ruby sidecar ===")

    with tempfile.TemporaryDirectory(prefix="bug017_") as td:
        root = Path(td)
        _mk_rails_repo(root, n_ruby=60)
        resp = bootstrap_repo(str(root))
        data = resp["data"]
        hint = data.get("language_hint")
        t(
            "TS bootstrap succeeded",
            data.get("status") == "success",
            f"status={data.get('status')} err={data.get('error')}",
        )
        t(
            "language_hint populated",
            isinstance(hint, dict),
            f"got {hint!r}",
        )
        if isinstance(hint, dict):
            t(
                "language_hint.primary is typescript",
                hint.get("primary") == "typescript",
                f"got {hint!r}",
            )
            t(
                "language_hint.secondary_detected is ruby",
                hint.get("secondary_detected") == "ruby",
                f"got {hint!r}",
            )
            t(
                "language_hint.secondary_file_count >= 50",
                int(hint.get("secondary_file_count") or 0) >= 50,
                f"got {hint!r}",
            )

    # Negative: TS-only repo without Gemfile gets no hint
    with tempfile.TemporaryDirectory(prefix="bug017_neg_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        (root / "a.ts").write_text("export const x = 1;\n")
        resp = bootstrap_repo(str(root))
        t(
            "TS-only repo has language_hint=None",
            resp["data"].get("language_hint") is None,
            f"got {resp['data'].get('language_hint')!r}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

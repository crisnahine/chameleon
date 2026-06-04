"""Archetype-independent content scan on files that resolve to no archetype.

A deterministic secret (or a dynamic eval) is a content fact, true no matter
which archetype a file matched or whether it matched one at all. The convention
and AST lints are correctly skipped without an archetype, but the credential scan
must still run so a leaked token in an unarchetyped file is not invisible. These
tests cover the shared scan helper, the posttool no-archetype advisory + state
recording, and the Stop backstop's no-archetype re-lint.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from chameleon_mcp import hook_helper
from chameleon_mcp.violation_class import is_hard_class

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


class TestScanArchetypeIndependent:
    def test_deterministic_secret_detected_and_hard(self, tmp_path):
        out = hook_helper._scan_archetype_independent(
            f'const k = "{AWS_KEY}";\n', str(tmp_path / "src/x.ts")
        )
        secrets = [v for v in out if v.get("rule") == "secret-detected-in-content"]
        assert secrets
        assert any(v.get("secret_hard") for v in secrets)
        assert any(is_hard_class(v) for v in secrets)

    def test_eval_call_detected(self, tmp_path):
        out = hook_helper._scan_archetype_independent(
            "const r = eval(userInput);\n", str(tmp_path / "src/x.ts")
        )
        assert any(v.get("rule") == "eval-call" for v in out)

    def test_clean_file_no_violations(self, tmp_path):
        out = hook_helper._scan_archetype_independent(
            "export const greeting = 'hi';\n", str(tmp_path / "src/x.ts")
        )
        assert out == []

    def test_no_secret_kind_does_not_hard_block(self, tmp_path):
        # A high-entropy / broad-fallback hit (no deterministic kind) stays
        # advisory: it must never be hard-class even though it is a secret rule.
        out = hook_helper._scan_archetype_independent(
            "export const x = 1;\n", str(tmp_path / "src/x.ts")
        )
        assert [v for v in out if v.get("rule") == "secret-detected-in-content"] == []

    def test_scan_failure_is_contained(self, tmp_path, monkeypatch):
        import chameleon_mcp.lint_engine as le

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(le, "scan_secrets", boom)
        # A raising secret scan must not abort the whole helper: the sink scan
        # still contributes (and a clean file yields an empty list, not a crash).
        out = hook_helper._scan_archetype_independent(
            "const r = eval(x);\n", str(tmp_path / "src/x.ts")
        )
        assert isinstance(out, list)
        assert any(v.get("rule") == "eval-call" for v in out)


class TestPosttoolNoArchetypeAdvisory:
    def test_hard_secret_recorded_into_state(self, tmp_path):
        repo_data = tmp_path / "data"
        file_path = str(tmp_path / "repo" / "config.ts")
        violations = hook_helper._scan_archetype_independent(f'const k = "{AWS_KEY}";\n', file_path)
        with patch.object(hook_helper, "_plugin_data_dir", return_value=repo_data):
            hook_helper._posttool_no_archetype_advisory(
                repo_root=tmp_path / "repo",
                repo_id="repo-secret",
                file_path=file_path,
                violations=violations,
                session_id="sess-1",
                now=1000.0,
            )
        from chameleon_mcp.enforcement import load_state

        state = load_state(repo_data / "repo-secret", "sess-1")
        fs = state.files.get(file_path)
        assert fs is not None
        # The synthetic no-archetype label is recorded so the Stop backstop
        # re-lints and the credential blocks the turn.
        assert hook_helper._NO_ARCHETYPE_LABEL in state.archetypes_with_violations

    def test_inline_ignore_drops_hard_record(self, tmp_path):
        repo_data = tmp_path / "data"
        repo = tmp_path / "repo"
        repo.mkdir()
        file_path = str(repo / "config.ts")
        # Write the file so the ignore-directive read finds the directive.
        (repo / "config.ts").write_text(
            f'const k = "{AWS_KEY}"; // chameleon-ignore secret-detected-in-content\n',
            encoding="utf-8",
        )
        violations = hook_helper._scan_archetype_independent(
            (repo / "config.ts").read_text(encoding="utf-8"), file_path
        )
        with patch.object(hook_helper, "_plugin_data_dir", return_value=repo_data):
            hook_helper._posttool_no_archetype_advisory(
                repo_root=repo,
                repo_id="repo-ign",
                file_path=file_path,
                violations=violations,
                session_id="sess-1",
                now=1000.0,
            )
        from chameleon_mcp.enforcement import load_state

        state = load_state(repo_data / "repo-ign", "sess-1")
        # The ignore directive drops the hard record, so no state file was created.
        assert state.files.get(file_path) is None

    def test_advisory_only_for_non_hard(self, tmp_path):
        repo_data = tmp_path / "data"
        repo = tmp_path / "repo"
        repo.mkdir()
        file_path = str(repo / "x.ts")
        (repo / "x.ts").write_text("const r = eval(userInput);\n", encoding="utf-8")
        violations = hook_helper._scan_archetype_independent(
            "const r = eval(userInput);\n", file_path
        )
        with patch.object(hook_helper, "_plugin_data_dir", return_value=repo_data):
            hook_helper._posttool_no_archetype_advisory(
                repo_root=repo,
                repo_id="repo-eval",
                file_path=file_path,
                violations=violations,
                session_id="sess-1",
                now=1000.0,
            )
        from chameleon_mcp.enforcement import load_state

        state = load_state(repo_data / "repo-eval", "sess-1")
        # eval-call is not archetype-independent, so without an archetype it never
        # reaches hard-class here; the advisory is emitted but no state is armed.
        assert state.files.get(file_path) is None


class TestStopBackstopNoArchetype:
    def _loaded(self):
        return SimpleNamespace(
            canonicals={"canonicals": {}},
            conventions={"conventions": {}},
            rules={},
        )

    def test_unarchetyped_secret_still_blocks(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "config.ts"
        f.write_text(f'const k = "{AWS_KEY}";\n', encoding="utf-8")

        # get_archetype resolves to nothing -> the no-archetype branch runs the
        # archetype-independent re-lint and the deterministic secret still stands.
        with patch("chameleon_mcp.tools.get_archetype", return_value={"data": {}}):
            out_rules: list[str] = []
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active={"secret-detected-in-content"},
                daemon_state={"available": False},
                out_rules=out_rules,
            )
        assert blocked is True
        assert "secret-detected-in-content" in out_rules

    def test_unarchetyped_secret_not_in_active_set_does_not_block(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "config.ts"
        f.write_text(f'const k = "{AWS_KEY}";\n', encoding="utf-8")

        with patch("chameleon_mcp.tools.get_archetype", return_value={"data": {}}):
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active=set(),
                daemon_state={"available": False},
            )
        assert blocked is False

    def test_unarchetyped_clean_file_does_not_block(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "x.ts"
        f.write_text("export const greeting = 'hi';\n", encoding="utf-8")

        with patch("chameleon_mcp.tools.get_archetype", return_value={"data": {}}):
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active={"secret-detected-in-content"},
                daemon_state={"available": False},
            )
        assert blocked is False

    def test_unarchetyped_secret_inline_ignore_clears(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "config.ts"
        f.write_text(
            f'const k = "{AWS_KEY}"; // chameleon-ignore secret-detected-in-content\n',
            encoding="utf-8",
        )

        with patch("chameleon_mcp.tools.get_archetype", return_value={"data": {}}):
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active={"secret-detected-in-content"},
                daemon_state={"available": False},
            )
        assert blocked is False

    def test_unarchetyped_secret_blocks_at_l0(self, tmp_path):
        # The deterministic-secret branch is archetype-independent and must refuse
        # the turn at the first edit (L0), not only after escalation to L2: a
        # leaked credential is never made safe by the escalation ladder.
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "config.ts"
        f.write_text(f'const k = "{AWS_KEY}";\n', encoding="utf-8")

        with patch("chameleon_mcp.tools.get_archetype", return_value={"data": {}}):
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active={"secret-detected-in-content"},
                daemon_state={"available": False},
                level=0,
            )
        assert blocked is True


class TestStopBackstopArchetypeDependentLevelGate:
    """Archetype-dependent rules honor the L2 ladder in the re-lint itself.

    An archetype-dependent hard violation on a confident AST match blocks at the
    Stop backstop only once the file has escalated to L2, so a single
    wrong-archetype guess cannot trap the turn. The archetype-independent branch
    is covered above; this exercises the level gate on the archetyped path.
    """

    def _loaded(self):
        return SimpleNamespace(
            canonicals={"canonicals": {}},
            conventions={"conventions": {}},
            rules={},
        )

    def _patches(self, dep_rule="naming-convention-violation"):
        # Resolve a confident AST archetype and surface one archetype-dependent
        # hard violation from the in-process re-lint, so only the level gate
        # decides the outcome.
        arch = {
            "data": {
                "archetype": "component",
                "match_quality": "ast",
                "confidence_band": "high",
            }
        }
        vio = [
            {
                "rule": dep_rule,
                "severity": "warning",
                "message": "m",
                "expected": "",
                "actual": "",
            }
        ]
        return (
            patch("chameleon_mcp.tools.get_archetype", return_value=arch),
            patch(
                "chameleon_mcp.hook_helper._lint_file_in_process",
                return_value=vio,
            ),
        )

    def test_archetype_dependent_blocks_at_l2(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "Widget.ts"
        f.write_text("export const C = 1\n", encoding="utf-8")
        p_arch, p_lint = self._patches()
        with p_arch, p_lint:
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active={"naming-convention-violation"},
                daemon_state={"available": False},
                level=2,
            )
        assert blocked is True

    def test_archetype_dependent_does_not_block_below_l2(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "Widget.ts"
        f.write_text("export const C = 1\n", encoding="utf-8")
        p_arch, p_lint = self._patches()
        with p_arch, p_lint:
            blocked = hook_helper._stop_file_still_blockable(
                repo,
                str(f),
                loaded=self._loaded(),
                active={"naming-convention-violation"},
                daemon_state={"available": False},
                level=0,
            )
        assert blocked is False

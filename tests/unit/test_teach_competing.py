"""teach_competing_import wires the wrapper-preference (competing) convention.

The competing convention + its principle were dead (competing_pairs always
None at bootstrap). This tool lets /chameleon-teach write
conventions.imports.<arch>.competing so the "use X, not Y" import rule and the
"use the project's wrapper" principle actually fire.
"""

from __future__ import annotations

import json


def _setup_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    from chameleon_mcp.conventions import empty_conventions

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "conventions.json").write_text(
        json.dumps(empty_conventions(generation=1)), encoding="utf-8"
    )
    # A TypeScript profile: the competing-import tests exercise npm-package
    # preferences, and the "not in package.json" warning is gated to TS/JS
    # profiles (a Ruby/Python repo's taught wrapper is not an npm package).
    (repo / ".chameleon" / "profile.json").write_text(
        json.dumps({"generation": 1, "language": "typescript"}), encoding="utf-8"
    )
    return repo


def _data(res):
    return res.get("data", res) if isinstance(res, dict) else res


def test_teach_competing_import_writes_and_is_idempotent(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)

    res = tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    # Mutation tools report success with status "success" (matches teach_profile,
    # teach_profile_structured, apply_archetype_renames).
    assert _data(res)["status"] == "success"

    conv = json.loads((repo / ".chameleon" / "conventions.json").read_text())
    competing = conv["conventions"]["imports"]["httpclient"]["competing"]
    assert {"preferred": "@/lib/http", "over": "axios"} in competing

    # The format helper reads this entry and emits the live import rule.
    from chameleon_mcp.conventions import format_conventions_for_session

    block = format_conventions_for_session(conv)
    assert "Use @/lib/http, not axios" in block

    # Idempotent: re-teaching the same pair doesn't duplicate it.
    res2 = tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    conv2 = json.loads((repo / ".chameleon" / "conventions.json").read_text())
    assert len(conv2["conventions"]["imports"]["httpclient"]["competing"]) == 1
    # The no-op must NOT claim the profile hash changed / suggest re-trust, since
    # nothing was written (mirrors unteach_competing_import's no-op note).
    d2 = _data(res2)
    assert d2["already_present"] is True
    assert "nothing changed" in d2["note"]
    assert "chameleon-trust" not in d2["note"]


def test_teach_competing_import_builds_counterexample(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    # A real file uses the discouraged import; teaching against it should capture
    # that line as the archetype's counterexample.
    src = repo / "src" / "httpClient.ts"
    src.parent.mkdir(parents=True)
    src.write_text("import axios from 'axios';\nexport const c = axios;\n", encoding="utf-8")

    res = tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    assert _data(res)["status"] == "success"

    ce = json.loads((repo / ".chameleon" / "counterexamples.json").read_text())
    rows = ce["archetypes"]["httpclient"]
    assert isinstance(rows, list) and len(rows) == 1
    entry = rows[0]
    assert entry["over"] == "axios"
    assert entry["snippet"] == "import axios from 'axios';"
    assert entry["preferred"] == "@/lib/http"


def test_teach_competing_import_no_counterexample_when_unused(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    # Nobody uses the discouraged import, so there is no real off-pattern to show.
    src = repo / "src" / "httpClient.ts"
    src.parent.mkdir(parents=True)
    src.write_text("import { http } from '@/lib/http';\n", encoding="utf-8")

    res = tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    assert _data(res)["status"] == "success"
    ce_path = repo / ".chameleon" / "counterexamples.json"
    if ce_path.is_file():
        ce = json.loads(ce_path.read_text())
        assert "httpclient" not in ce.get("archetypes", {})


def test_unteach_competing_import_removes_counterexample(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    src = repo / "src" / "httpClient.ts"
    src.parent.mkdir(parents=True)
    src.write_text("import axios from 'axios';\n", encoding="utf-8")
    tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    ce_path = repo / ".chameleon" / "counterexamples.json"
    assert "httpclient" in json.loads(ce_path.read_text())["archetypes"]

    # Un-teaching the only pair must drop the stale counterexample.
    res = tools.unteach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    assert _data(res)["removed"] is True
    assert "httpclient" not in json.loads(ce_path.read_text())["archetypes"]


def test_two_teaches_one_archetype_keep_both_counterexamples(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("import axios from 'axios';\n", encoding="utf-8")
    (repo / "src" / "b.ts").write_text("import moment from 'moment';\n", encoding="utf-8")
    tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="dayjs", over="moment"
    )
    ce_path = repo / ".chameleon" / "counterexamples.json"
    # Both taught off-patterns are kept; the second teach does not clobber the first.
    rows = json.loads(ce_path.read_text())["archetypes"]["httpclient"]
    assert {r["over"] for r in rows} == {"axios", "moment"}

    # Removing moment recomputes from the remaining axios pair (still used in a.ts).
    tools.unteach_competing_import(
        str(repo), archetype="httpclient", preferred="dayjs", over="moment"
    )
    rows2 = json.loads(ce_path.read_text())["archetypes"]["httpclient"]
    assert [r["over"] for r in rows2] == ["axios"]


def test_teach_caps_counterexample_rows_per_archetype(tmp_path, monkeypatch):
    from chameleon_mcp import counterexamples as ce
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(parents=True)
    over = ce._MAX_ROWS_PER_ARCHETYPE + 3
    for n in range(over):
        (repo / "src" / f"off{n}.ts").write_text(f"import x from 'badmod{n}';\n", encoding="utf-8")
        tools.teach_competing_import(
            str(repo),
            archetype="httpclient",
            preferred=f"@/lib/good{n}",
            over=f"badmod{n}",
        )
    rows = json.loads((repo / ".chameleon" / "counterexamples.json").read_text())["archetypes"][
        "httpclient"
    ]
    # The append must not let the artifact grow past the cap (cap, not cap+1).
    assert len(rows) == ce._MAX_ROWS_PER_ARCHETYPE


def test_reteaching_same_over_does_not_duplicate_counterexample(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("import axios from 'axios';\n", encoding="utf-8")
    tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    # Re-teaching the SAME over (different preferred) replaces the row, not appends.
    tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/fetch", over="axios"
    )
    rows = json.loads((repo / ".chameleon" / "counterexamples.json").read_text())["archetypes"][
        "httpclient"
    ]
    assert len(rows) == 1 and rows[0]["over"] == "axios"


def test_teach_competing_import_rejects_bad_input(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)

    # empty 'over'
    assert (
        _data(
            tools.teach_competing_import(str(repo), archetype="httpclient", preferred="x", over="")
        )["status"]
        == "failed"
    )
    # preferred == over
    assert (
        _data(
            tools.teach_competing_import(str(repo), archetype="httpclient", preferred="x", over="x")
        )["status"]
        == "failed"
    )
    # invalid archetype name
    assert (
        _data(
            tools.teach_competing_import(str(repo), archetype="Bad Name!", preferred="x", over="y")
        )["status"]
        == "failed"
    )


def _setup_repo_with_archetypes(tmp_path, monkeypatch, names):
    repo = _setup_repo(tmp_path, monkeypatch)
    (repo / ".chameleon" / "archetypes.json").write_text(
        json.dumps({"archetypes": {n: {} for n in names}}), encoding="utf-8"
    )
    return repo


def test_teach_competing_known_archetype_no_warning(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo_with_archetypes(tmp_path, monkeypatch, ["httpclient"])
    res = _data(
        tools.teach_competing_import(
            str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
        )
    )
    assert res["status"] == "success"
    assert "warning" not in res


def test_teach_competing_unknown_archetype_warns_but_succeeds(tmp_path, monkeypatch):
    # The rule drives a lint; a typo'd archetype no file matches is a silent dead
    # rule. It is still recorded (forward-compat for renamed archetypes) but flagged.
    from chameleon_mcp import tools

    repo = _setup_repo_with_archetypes(tmp_path, monkeypatch, ["httpclient"])
    res = _data(
        tools.teach_competing_import(
            str(repo), archetype="typoclient", preferred="@/lib/http", over="axios"
        )
    )
    assert res["status"] == "success"
    assert "warning" in res
    assert "typoclient" in res["warning"]


def test_teach_competing_no_catalog_no_warning(tmp_path, monkeypatch):
    # Fail-open: with no archetypes.json the known set is undeterminable, so no warning.
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        tools.teach_competing_import(
            str(repo), archetype="anything", preferred="@/lib/http", over="axios"
        )
    )
    assert res["status"] == "success"
    assert "warning" not in res


def _setup_repo_with_package_json(tmp_path, monkeypatch, deps):
    repo = _setup_repo_with_archetypes(tmp_path, monkeypatch, ["httpclient"])
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {d: "1.0.0" for d in deps}}), encoding="utf-8"
    )
    return repo


def test_teach_competing_warns_when_preferred_package_absent(tmp_path, monkeypatch):
    # A bare npm package `preferred` not in package.json is a likely typo that would
    # silently steer the model at a nonexistent module; flag it (non-fatal).
    from chameleon_mcp import tools

    repo = _setup_repo_with_package_json(tmp_path, monkeypatch, ["styled-components"])
    res = _data(
        tools.teach_competing_import(
            str(repo),
            archetype="httpclient",
            preferred="styled-componentz",
            over="emotion",
        )
    )
    assert res["status"] == "success"
    assert "warning" in res
    assert "styled-componentz" in res["warning"]


def test_teach_competing_no_warning_when_preferred_package_present(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo_with_package_json(tmp_path, monkeypatch, ["styled-components"])
    res = _data(
        tools.teach_competing_import(
            str(repo),
            archetype="httpclient",
            preferred="styled-components",
            over="emotion",
        )
    )
    assert res["status"] == "success"
    assert "warning" not in res


def test_teach_competing_alias_preferred_never_warns_as_package(tmp_path, monkeypatch):
    # A path-alias / relative preferred is resolved by tsconfig, not package.json,
    # and may be created later — never flag it as a missing package (avoid punishing
    # a valid forward-looking teaching).
    from chameleon_mcp import tools

    repo = _setup_repo_with_package_json(tmp_path, monkeypatch, ["react"])
    for pref in ("@/lib/cn", "./utils/cn", "@/utils/http"):
        res = _data(
            tools.teach_competing_import(
                str(repo), archetype="httpclient", preferred=pref, over=f"x-{pref}"
            )
        )
        assert res["status"] == "success"
        assert "package.json" not in (res.get("warning") or "")


def test_teach_competing_baseurl_bare_import_not_flagged_as_package(tmp_path, monkeypatch):
    # A bare specifier resolved via tsconfig `baseUrl` (`lib/api-client` ->
    # `<repo>/lib/api-client.ts`) is a first-party module, not a missing npm
    # package. It must not be flagged, while a genuinely absent bare package still is.
    from chameleon_mcp import tools

    repo = _setup_repo_with_package_json(tmp_path, monkeypatch, ["axios"])
    (repo / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": "."}}), encoding="utf-8"
    )
    (repo / "lib").mkdir(exist_ok=True)
    (repo / "lib" / "api-client.ts").write_text("export const api = {};\n", encoding="utf-8")

    ok = _data(
        tools.teach_competing_import(
            str(repo), archetype="httpclient", preferred="lib/api-client", over="fetch"
        )
    )
    assert "package.json" not in (ok.get("warning") or "")

    # detection intact: a real missing bare package still warns
    bad = _data(
        tools.teach_competing_import(
            str(repo),
            archetype="httpclient",
            preferred="totally-absent-pkg",
            over="fetch",
        )
    )
    assert "package.json" in (bad.get("warning") or "")

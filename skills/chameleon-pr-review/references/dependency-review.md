# pr-review reference — Step 2.5: Dependency-change review

### Step 2.5: Dependency-change review (always, for manifest/lockfile diffs)

Run this whenever the diff touches a dependency manifest or lockfile of ANY ecosystem — the npm/Bundler set the tool parses (`package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile`, `Gemfile.lock`) AND the ones it does not (Python `requirements*.txt` / `pyproject.toml` / `Pipfile` / `setup.py`, Go, Rust, PHP). Always call `scan_dependency_changes` (below) — it parses the npm/Bundler files and reports the rest in `uncovered_manifests` for the hand-review disclosure. These are the supply-chain entry points a human reviewer reads line by line and the convention review above does not cover. This pass is a pure parse of the diff text and the manifest/lockfile JSON or YAML. It makes NO network calls and does not install or run anything: only the added (`+`) lines matter, and the existing repo content gives the "previously present" baseline.

**Tool-backed (deterministic):** Call the `scan_dependency_changes` action once for the whole diff:

```
chameleon_review(action="scan_dependency_changes", params={"repo": <repo_id>, "base_ref": <the PR base branch, or the branch's merge base; use the locked production_ref from .chameleon/config.json when no PR base is known; default "main">})
```

It parses the manifest/lockfile diff (no network) and returns structured `findings`. Each finding is `{check, severity, path, evidence, message, detail}` — the check TYPE is `finding.check` (NOT `rule`; that is `lint_file`'s field), the cited added line is `finding.evidence` (NOT `line`), the manifest/lockfile it sits in is `finding.path`, and `finding.severity` is the literal `"FIX"` or `"NIT"` (not `error`/`warning`). Route by `finding.check`: `install-script`, `non-registry-host`, `non-registry-source`, and `minified-manifest` come back at severity `"FIX"`; `new-dependency` comes back at `"NIT"` and is the 2.5a listing you carry into the human-judgment gate below. (The top-level envelope also carries `manifests_changed` and `summary` on a successful scan, and `uncovered_manifests` ONLY when non-empty; a degraded scan — `status` `"degraded"`/`"failed"` — omits all three, so guard for their absence rather than reading them unconditionally.) Use these findings as the deterministic source for 2.5b/2.5c/2.5d/2.5e and the 2.5a listing instead of hand-parsing the JSON/YAML — each is groundable by the round-3 refuter against the tool result (this is the Step 2.5 exception to the chameleon-data rule). The tool does NOT score typosquats; that judgment stays yours under 2.5a. If `scan_dependency_changes` is unavailable in this session, fall back to the manual parse described below and note it in one line. It is no-network and never replaces the opt-in `dep_audit` CVE scan (`chameleon_review(action="dep_audit", ...)`).

**Uncovered ecosystems (Python and others) — disclose AND hand-review, split by severity exactly as the npm path does.** The scanner parses only npm and Bundler. A changed dependency manifest of an ecosystem it does not parse — Python (`requirements*.txt`, `pyproject.toml`, `Pipfile`, `setup.py`), Go, Rust, PHP — comes back in the envelope's `uncovered_manifests` list (with `findings` empty and `manifests_changed` empty, which for those files means "not parsed", NOT "reviewed clean"). When `uncovered_manifests` is non-empty, the tool could not scan it, but YOU can still read the added (`+`) lines — so hand-review them and route each signal to the SAME severity the npm/Bundler checks give the identical content. Do not lump everything into an ACK: a blatant supply-chain red flag that is plainly visible in the diff is a witnessed finding, and suppressing it because the parser was silent inverts the very asymmetry the ACK rule exists to prevent.

- **ACK (does NOT drive the verdict):** the "not covered by the automated scan" disclosure for each file (its own ACK line), AND a new direct dependency whose only signal is its NAME — a routine add, treated exactly like the npm 2.5a new-dependency ACK (confirm it is not a typosquat, e.g. `left-pad-py`). A routine Python dep add therefore stays APPROVE + ACK, symmetric with the identical npm add.
- **FIX (drives NEEDS CHANGES, like npm 2.5b/2.5d):** an added line carrying a red flag you can read directly — a non-registry or git/URL/path source (`pkg @ git+https://…`, a `-e <url>`, a `file:`/`path:` dep, a `[[tool.poetry.source]]` pointing off-PyPI), a registry redirection (a `--index-url`/`--extra-index-url` to a non-PyPI host in requirements), or an install hook (a `setup.py` that runs code, a Pipfile script). Cite the exact added line — this is the Step 2.5 diff-parse exception to the chameleon-data rule (the manifest diff is the backing fact), the same as the npm findings, and it is refuter-groundable against that line.

So a clean Python dependency add is APPROVE + ACK (no pollution), and a Python manifest carrying an index redirection or a git source is NEEDS CHANGES with a FIX — identical to how the same content reads in a `package.json`. The one thing never acceptable is a Python PR that added a non-registry source rendering a silent clean APPROVE.

Each finding cites the exact lockfile line or manifest key. The five checks are independent; run every one that applies even if an earlier check fired.

#### 2.5e. Minified manifest (FIX)

`scan_dependency_changes` returns a `minified-manifest` FIX finding when a `package.json` diff collapses a manifest object onto ONE physical added line — either the whole manifest as a single JSON line (`detail.reason` `single-line-manifest`) OR a dependency/pin/scripts container opened inline with its pairs packed onto one line (`detail.reason` `packed-container-line`, e.g. `"dependencies": { "evil": "git+ssh://…", "left-pad": "^1.0.0" }`, or a packed `overrides`/`resolutions` object that pins a transitive dependency to a git source). Both are supply-chain evasions: the per-key scanners (install-script, non-registry-host/-source, new-dependency) are line-oriented and cannot decompose a packed line, so every other 2.5 check was silently defeated for it. Surface it as a **FIX** citing the finding, and re-review the manifest by hand (expand it) before trusting any of the 2.5a-2.5d results for that file. A source file (not a manifest) is never subject to this check.

#### 2.5a. New direct dependency → acknowledge provenance (ACK, does NOT drive the verdict)

Parse the manifest diff (`package.json` `dependencies`/`devDependencies`/`optionalDependencies`/`peerDependencies`; `Gemfile` `gem` lines) for a dependency name that was NOT present before this change. A bump of an already-present dependency is NOT this finding (it may be a different finding under 2.5b/2.5d); only a name that did not exist in the manifest before counts as new. `scan_dependency_changes` reports each as a `new-dependency` finding at severity `NIT`.

For each new direct dependency, emit an **ACK** line in the dedicated "Acknowledge before merge" section. An ACK is NOT a BLOCK and does NOT drive the verdict. This is a deliberate human gate, not a defect claim: the reviewer must confirm the package is the intended one (not a typosquat of a popular name, e.g. `lodahs` for `lodash`, `cross-env.js` for `cross-env`) and that adding it is wanted. State the dependency name, the version range added, and the manifest file. A routine PR that only adds a legitimate dependency stays at its findings verdict (APPROVE if nothing else fired) with an outstanding ACK the human clears out-of-band.

(Earlier versions raised a BLOCK here. That conflated "must fix before merge" with "please confirm this is intended" and recorded every routine dependency add as a BLOCK verdict in the durable ledger, corrupting the per-`complexity_tier` review-clean metric. The provenance gate stays — it just lives in its own non-verdict ACK channel now, matching the engine's own `NIT`/advisory classification of `new-dependency`.)

When several new dependencies land in one change, list each as its own ACK line so each gets its own acknowledgement.

#### 2.5b. Lockfile resolved host is not the expected registry (FIX)

In the lockfile diff, every added entry that resolves a package records the URL it was fetched from. Flag any added entry whose resolved host is NOT the package manager's public registry:
- npm (`package-lock.json` `resolved`, `npm-shrinkwrap.json` `resolved`, `yarn.lock` `resolved`, `pnpm-lock.yaml` `resolution.tarball`/`resolved`): expected host is `registry.npmjs.org`.
- Bundler (`Gemfile.lock` `remote:` under a `GEM` section): expected host is `rubygems.org`.

A resolved URL pointing at any other host (a private mirror the repo does not already use, a raw GitHub tarball, an arbitrary domain) is a **FIX**: the dependency is being pulled from somewhere other than the registry, which is how a tampered or planted package enters. Cite the exact lockfile line and the host. If the repo's other lockfile entries consistently use a private registry (the diff shows the SAME non-`registry.npmjs.org` host on pre-existing entries), that host is this repo's normal registry; treat it as expected and do not flag added entries that use it. Flag only hosts that differ from what the rest of the lockfile already uses.

#### 2.5c. New install lifecycle script (FIX)

In the `package.json` diff, flag a newly added `scripts.preinstall`, `scripts.install`, or `scripts.postinstall` as a **FIX**. An install-lifecycle script runs automatically on `npm install` with no further prompt, which is the classic vector for code that executes the moment a dependency tree is materialized. Cite the script key and its command. (A script that already existed and is merely edited is still worth a look, but the FIX-worthy signal is a NEW install hook on a diff that also adds or bumps dependencies.)

#### 2.5d. Non-registry dependency source (FIX)

In the manifest diff, flag any added or changed dependency whose version specifier is a source other than a registry version range. These pull code straight from a remote without going through the registry's publish path:
- `git+ssh:`, `git+https:`, `git:`, `github:`, or a bare `user/repo#ref` git shorthand.
- `file:` (a local path dependency) and `link:`.
- `http:` or `https:` pointing at a tarball.

Cite the dependency name and the source string. For a `Gemfile`, the equivalent is a `gem ... git:`/`github:`/`path:` option on a `gem` line. A `git+ssh:` or `file:` source is a **FIX** because the resolved code is not the registry artifact and is not covered by the registry's integrity guarantees.

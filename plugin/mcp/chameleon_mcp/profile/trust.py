"""Trust state management for committed profiles.

Per docs/architecture.md "Profile schema" → `.trust` file format + Round 4
trust model with cooldown.

Trust is per-user, per-repo. Stored at `${PLUGIN_DATA}/<repo_id>/.trust`.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp._thresholds import threshold_float
from chameleon_mcp.locks import LockHeldError, portable_flock_deadline, portable_funlock
from chameleon_mcp.safe_open import (
    UnsafeFileError,
    safe_read_profile_artifact_bytes,
)


def _resolve_main_key(repo_root: Path | str, self_key: str) -> str | None:
    """Resolved main-worktree path key for a linked worktree, else ``None``.

    A grant on a main worktree covers its linked worktrees: they are the same
    repo and read the same reviewed profile. This resolves a linked worktree
    (identified by its ``.git`` FILE pointer) to its MAIN worktree root so a grant
    recorded for main's path also vouches for the worktree -- INDEPENDENT of
    whether the worktree checked out its own committed ``.chameleon``.

    That independence is the fix for the common, documented workflow:
    ``/chameleon-init`` tells teams to COMMIT ``.chameleon`` so it is shared, and
    ``git worktree add`` then checks it into every new worktree. The old
    resolution keyed on ``.chameleon`` being ABSENT from the worktree, so a
    committed profile defeated the bridge and a freshly created worktree (e.g.
    for ``/chameleon-deep-work``) came up untrusted with its guardrails silently
    off. Keying on the ``.git`` pointer instead makes trust inherit either way.

    Content is still guarded downstream: the resolved key drives the granted
    profile-hash lookup, so a worktree whose ``.chameleon`` diverges from the
    granted profile does not silently pass as the reviewed one. For every
    non-worktree root (``.git`` is a directory or absent) this returns ``None``
    and trust behavior is byte-identical. Returns ``None`` when the resolved key
    equals ``self_key`` (nothing to inherit).
    """
    from chameleon_mcp.worktree import main_worktree_root

    try:
        wt_root = Path(repo_root)
        git_marker = wt_root / ".git"
        # A linked worktree has a ``.git`` FILE (a ``gitdir:`` pointer); a
        # standalone repo has a ``.git`` DIRECTORY. Only the file case bridges.
        if not git_marker.is_file():
            return None
        # verify_backref: the trust bridge fails closed against a forged .git
        # pointer (a planted dir claiming to be a worktree of a victim's trusted
        # main). The profile-content path (resolve_profile_root) does not need it.
        main_root = main_worktree_root(git_marker, verify_backref=True)
        if main_root is None:
            return None
        main_key = str(main_root.resolve())
        if main_key == self_key:
            return None
        # Fail-closed content gate. Inherit main's grant ONLY when the worktree
        # either has no profile of its own (it reads main's) OR carries a profile
        # trust-hash-identical to main's -- i.e. it is the same reviewed content.
        # A worktree checked out on a branch whose ``.chameleon`` was modified (a
        # poisoned feature branch) diverges here and does NOT inherit trust; it
        # reads untrusted, regardless of the one-time-trust default that would
        # otherwise skip the staleness re-check. Off the per-edit hot path for
        # every non-worktree repo (the ``.git``-is-a-file gate returns first).
        wt_profile = wt_root / ".chameleon"
        if wt_profile.exists():
            main_profile = main_root / ".chameleon"
            if (not main_profile.exists()) or hash_profile(wt_profile) != hash_profile(
                main_profile
            ):
                return None
    except OSError:
        return None
    return main_key


class ProfileInjectionError(Exception):
    """A profile_dir failed the canonical-artifacts injection/secret scan.

    Raised by :func:`grant_trust` when a committed profile's prose artifacts
    (conventions.json, idioms.md, principles.md, canonicals.json) carry a
    prompt-injection signal, a hardcoded secret, or a dangerous code pattern.
    Callers treat it as "refuse to trust this profile" rather than a crash.
    """


def plugin_data_dir() -> Path:
    """Resolve where chameleon stores per-user state (trust DB, drift.db).

    Delegates to plugin_paths.plugin_data_dir(). Trust state is per-user,
    not per-plugin-instance. CHAMELEON_PLUGIN_DATA is the only supported
    override; Claude Code's CLAUDE_PLUGIN_DATA is deliberately NOT honored
    (would partition trust records across launchers).
    """
    from chameleon_mcp.plugin_paths import plugin_data_dir as _pd

    return _pd()


def repo_data_dir(repo_id: str) -> Path:
    """`${PLUGIN_DATA}/<repo_id>/` directory, created if missing (0700)."""
    from chameleon_mcp.plugin_paths import ensure_plugin_data_dir

    base = ensure_plugin_data_dir()
    d = base / repo_id
    d.mkdir(exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


@dataclass
class TrustRecord:
    """Contents of `.trust` file.

    Schema:
        granted_at: ISO-8601 timestamp of the (first) grant.
        granted_by_user: best-effort username for the audit trail.
        profile_sha256: hash of the "root" profile_dir at grant time. This
            is what older trust records carried alone; newer records keep it as
            the fallback hash for any repo_root not present in the new
            ``repo_root_specific_hashes`` map (backward compat).
        repo_root: filesystem path of the profile_dir's parent recorded
            on the FIRST grant. For non-monorepo repos this is the only
            trusted root. For monorepos with multiple workspace-internal
            trust grants the value reflects whichever root was trusted
            first; subsequent workspace grants land in the per-root map.
        repo_root_specific_hashes: optional map of resolved-repo_root path
            → profile_sha256, populated when a workspace-internal profile
            is trusted alongside (or instead of) the root. Lookups for a
            given repo_root prefer this map; absence falls back to
            ``profile_sha256``. Legacy records that lack this field still
            load (defaults to ``{}``).
        repo_root_specific_granted_at: optional map of resolved-repo_root path
            → ISO-8601 grant time for THAT root, so a subsequent grant of a
            workspace under a monorepo-shared repo_id reports its own grant
            time rather than the first root's. The top-level ``granted_at``
            stays the first grant. Legacy records lack this field (defaults
            to ``{}``); lookups fall back to ``granted_at``.
    """

    granted_at: str
    granted_by_user: str
    profile_sha256: str
    repo_root: str = ""
    repo_root_specific_hashes: dict[str, str] = field(default_factory=dict)
    repo_root_specific_granted_at: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> TrustRecord:
        raw_map = data.get("repo_root_specific_hashes") or {}
        specific: dict[str, str] = {}
        if isinstance(raw_map, dict):
            for k, v in raw_map.items():
                if isinstance(k, str) and isinstance(v, str):
                    specific[k] = v
        raw_times = data.get("repo_root_specific_granted_at") or {}
        specific_times: dict[str, str] = {}
        if isinstance(raw_times, dict):
            for k, v in raw_times.items():
                if isinstance(k, str) and isinstance(v, str):
                    specific_times[k] = v
        return cls(
            granted_at=str(data.get("granted_at", "")),
            granted_by_user=str(data.get("granted_by_user", "")),
            profile_sha256=str(data.get("profile_sha256", "")),
            repo_root=str(data.get("repo_root", "")),
            repo_root_specific_hashes=specific,
            repo_root_specific_granted_at=specific_times,
        )

    def to_dict(self) -> dict:
        out: dict = {
            "granted_at": self.granted_at,
            "granted_by_user": self.granted_by_user,
            "profile_sha256": self.profile_sha256,
            "repo_root": self.repo_root,
        }
        if self.repo_root_specific_hashes:
            out["repo_root_specific_hashes"] = dict(self.repo_root_specific_hashes)
        if self.repo_root_specific_granted_at:
            out["repo_root_specific_granted_at"] = dict(self.repo_root_specific_granted_at)
        return out

    def granted_at_for_root(self, repo_root: Path | str) -> str:
        """Grant time recorded for ``repo_root`` specifically.

        Prefers the per-root map (the honest "when did I trust THIS root"),
        falling back to the top-level first-grant ``granted_at`` for legacy
        records and for the root that carries the top-level fields.
        """
        try:
            key = str(Path(repo_root).resolve())
        except OSError:
            key = str(repo_root)
        return self.repo_root_specific_granted_at.get(key) or self.granted_at

    def hash_for_root(self, repo_root: Path | str) -> str:
        """Return the most-specific trusted hash for ``repo_root``.

        Lookup order:
            1. ``repo_root_specific_hashes[str(repo_root.resolve())]`` when
               a workspace-internal grant has been recorded.
            2. ``profile_sha256`` — the "root" hash recorded on the first
               grant. This is the backward-compat path for legacy records
               and for repos where only the top-level was trusted.

        Always returns a string (possibly empty when the record is malformed).
        """
        try:
            key = str(Path(repo_root).resolve())
        except OSError:
            key = str(repo_root)
        specific = self.repo_root_specific_hashes.get(key)
        if specific:
            return specific
        main_key = _resolve_main_key(repo_root, key)
        if main_key is not None:
            inherited = self.repo_root_specific_hashes.get(main_key)
            if inherited:
                return inherited
        return self.profile_sha256

    def grants_root(self, repo_root: Path | str) -> bool:
        """True iff this record was granted for ``repo_root`` specifically.

        A record is keyed by repo_id, which a monorepo shares across its root
        and every workspace-internal ``.chameleon`` profile. A grant on the
        root does NOT vouch for a different workspace's profile (different
        code, different conventions, never reviewed), so an ungranted
        workspace must read as *untrusted* -- not *stale*, which both leaks an
        unreviewed canonical and implies a refresh that never happened.

        Newer records seed ``repo_root_specific_hashes`` with every granted
        root, so membership there is authoritative. Legacy records have
        no map; fall back to the single top-level ``repo_root``.
        """
        try:
            key = str(Path(repo_root).resolve())
        except OSError:
            key = str(repo_root)
        if self.repo_root_specific_hashes:
            if key in self.repo_root_specific_hashes:
                return True
            main_key = _resolve_main_key(repo_root, key)
            return main_key is not None and main_key in self.repo_root_specific_hashes
        if key == self.repo_root:
            return True
        main_key = _resolve_main_key(repo_root, key)
        return main_key is not None and main_key == self.repo_root


_HASHED_ARTIFACTS: tuple[str, ...] = (
    ".archetype_renames.json",
    "archetypes.json",
    "calls_index.json",
    "canonicals.json",
    "config.json",
    "constant_index.json",
    "conventions.json",
    "counterexamples.json",
    "enforcement.json",
    "exports_index.json",
    "function_catalog.json",
    "principles.md",
    "idioms.md",
    "profile.json",
    "reverse_index.json",
    "rules.json",
    "symbol_signatures.json",
)


def hash_profile(profile_dir: Path) -> str:
    """SHA-256 over the user-visible profile surface for material-change detection.

    Hashes every artifact in :data:`_HASHED_ARTIFACTS` that exists on disk,
    in the fixed declaration order of that tuple, with each entry framed by
    ``b"\\x00<filename>\\x00"`` so two artifacts can never collide via
    boundary ambiguity. The fixed ordering plus per-file framing means the
    hash is reproducible byte-for-byte from the profile_dir alone — useful
    for audit reproducibility.

    Included artifacts:

    - ``archetypes.json`` — archetype definitions. ``/chameleon-rename``
      mutates these; older records did NOT include this file, so renames slipped
      past trust unchanged (Bug H1).
    - ``canonicals.json`` — canonical witness mappings. Also rewritten by
      ``/chameleon-rename``.
    - ``config.json`` — committed repo config (enforcement mode, canonical_ref,
      production_ref). Hashed so flipping enforcement or re-pointing derivation
      de-trusts the profile instead of slipping past unchanged.
    - ``enforcement.json`` — the block-rule calibration verdict. Hashed so a
      tampered or planted calibration (e.g. flipping a known-false-positive rule
      to "active") de-trusts the profile instead of slipping past unchanged.
    - ``exports_index.json`` — per-file exported-symbol sets backing the
      phantom-symbol check. Hashed so a planted index (e.g. one claiming a file
      exports a name it does not, to mask a hallucinated import) de-trusts the
      profile rather than silently steering the check.
    - ``function_catalog.json`` — per-function name/signature-shape catalog
      backing the cross-file duplication prefilter. Hashed so a planted catalog
      (e.g. one inventing a function+path to steer the duplication candidates
      reaching the model) de-trusts the profile rather than slipping past
      unchanged.
    - ``reverse_index.json`` — exported-name -> importer reverse index backing
      the cross-file edit-time advisory and the existence-break query. Hashed so
      a planted index (e.g. one fabricating importer call sites to manufacture a
      false break) de-trusts the profile rather than silently steering the check.
    - ``idioms.md`` — captured team idioms. ``/chameleon-teach`` mutates
      this; included so the user re-reviews new natural-language idioms
      before they reach model context. Once ``.chameleon/idioms/`` exists
      (the per-file idiom store), this file is a generated view of that
      store and drops OUT of the hashed surface — see below.
    - ``profile.json`` — top-level profile + summary. The original
      hash input.
    - ``rules.json`` — lint rules; ``/chameleon-rename`` may rewrite
      archetype-keyed entries here.
    - ``symbol_signatures.json`` — per-callable signature + body span backing
      the forward definition-hydration the correctness judge reads. Hashed so a
      planted index (e.g. one inventing a definition to steer the judge) de-trusts
      the profile rather than silently feeding the reviewer fabricated context.

    Once ``.chameleon/idioms/`` exists, ``idioms.md`` is a rendered view of
    that store, so hashing it would flip trust on every cosmetic re-render
    or hand edit of the view without a real content change (and would miss
    a store-only edit that never touches the view). Instead each
    ``idioms/*.json`` record is hashed in its place, sorted by filename so
    the digest stays reproducible from profile_dir alone; non-record files
    in that directory (``.view_digest``, ``.quarantine.md``) are metadata,
    not idiom truth, and are excluded. Unmigrated profiles (no ``idioms/``
    dir) hash byte-identically to before this store existed.

    Returns an empty string if ``profile.json`` is missing — callers treat
    that as "no trustable profile yet" rather than a real hash. Missing
    optional artifacts (e.g., ``idioms.md`` on a repo that hasn't run
    /chameleon-teach) are simply skipped: their framing bytes never get
    written, so adding the file later produces a distinct hash.
    """
    profile_json = profile_dir / "profile.json"
    if not profile_json.is_file():
        return ""
    h = hashlib.sha256()
    names: list[str] = list(_HASHED_ARTIFACTS)
    idioms_dir = profile_dir / "idioms"
    try:
        if idioms_dir.is_dir():
            # The store is truth and idioms.md is a generated view: hash the
            # truth, not the view (a byte-identical re-render or a viewer's
            # hand edit must never flip trust). Sorted filenames keep the
            # digest reproducible from profile_dir alone.
            names = [n for n in names if n != "idioms.md"]
            names.extend(sorted(f"idioms/{p.name}" for p in idioms_dir.glob("*.json")))
    except OSError:
        pass
    for filename in names:
        artifact = profile_dir / filename
        try:
            body = safe_read_profile_artifact_bytes(artifact)
        except FileNotFoundError:
            continue
        except (OSError, UnsafeFileError) as exc:
            h.update(b"\x01" + filename.encode("utf-8") + b"\x01")
            h.update(b"UNSAFE:" + type(exc).__name__.encode("ascii"))
            continue
        h.update(b"\x00" + filename.encode("utf-8") + b"\x00")
        h.update(body)
    return h.hexdigest()


def trust_state_for(repo_id: str) -> TrustRecord | None:
    """Read the .trust file for a repo. Returns None if not trusted."""
    trust_path = repo_data_dir(repo_id) / ".trust"
    return _read_trust_record(trust_path)


def trust_state_probe(repo_id: str) -> TrustRecord | None:
    """``trust_state_for`` without repo_data_dir's create-if-missing side
    effect. For pure existence probes (detect_repo's legacy-id re-trust hint)
    that speculate about identities nothing else uses — the probe must never
    mint a permanently-orphaned empty data directory per probed id.
    """
    from chameleon_mcp.plugin_paths import ensure_plugin_data_dir

    return _read_trust_record(ensure_plugin_data_dir() / repo_id / ".trust")


def _read_trust_record(trust_path: Path) -> TrustRecord | None:
    if not trust_path.is_file():
        return None
    try:
        return TrustRecord.from_dict(json.loads(trust_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError, OSError):
        # OSError covers the TOCTOU window: is_file() saw the file but a
        # concurrent rotation/removal makes read_text() raise. Fail open to
        # "untrusted" (None) rather than letting the gate raise -- a trust read
        # must never crash the caller.
        return None


def injected_prose_artifact(profile_dir: Path) -> str | None:
    """Name of the first prose artifact (``idioms.md`` or ``principles.md``)
    that trips the injection scan, or None if both are clean.

    The same narrow check the /chameleon-teach gate uses (ignore-previous,
    you-are-now-X, reveal-prompt, eval/exec/rm-rf). Callers that mutate
    idioms.md before granting trust (e.g. the idiom-store migration) must run
    this BEFORE that mutation, not after: a migration regenerates idioms.md
    from parsed records, and free-form prose with no recognized "### " block
    (which is exactly the shape a raw injection payload has) is dropped from
    the regenerated view rather than quarantined -- scanning the post-
    migration file would silently clear a poisoned repo instead of refusing
    it. Fails open (returns None) on a scanner error: trusting your own repo
    must not wedge on an unrelated bug.
    """
    try:
        from chameleon_mcp.tools import _looks_suspicious

        for prose in ("idioms.md", "principles.md"):
            prose_path = profile_dir / prose
            if not prose_path.is_file():
                continue
            try:
                prose_text = prose_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _looks_suspicious(prose_text)[0]:
                return prose
    except Exception:  # noqa: BLE001
        pass
    return None


def grant_trust(repo_id: str, profile_dir: Path) -> TrustRecord:
    """Write or update a .trust record for repo_id with current profile hash.

    Uses an atomic write pattern (write tmp then rename) to avoid partial
    files. ``profile_dir.parent`` (the "repo_root" being trusted) is
    recorded so future tool calls can resolve repo_id → repo_root without
    scanning every known repo.

    Per-root storage (Bug H6):

    Repositories with multiple `repo_root`-equivalent layouts under the
    same git remote (monorepos with per-workspace .chameleon/, hybrid
    Rails+JS, etc.) all share a single repo_id. To keep workspace-internal
    trust grants from being clobbered by a root grant (and vice versa)
    the record now carries an additive map of resolved
    ``repo_root → profile_sha256``.

    - First grant (no existing record): writes the top-level
      ``profile_sha256`` AND seeds ``repo_root_specific_hashes`` with the
      same hash, keyed by the resolved repo_root. The top-level fields
      mirror the original semantics for any caller still reading the legacy
      shape.
    - Subsequent grant for the SAME repo_root as the existing record:
      refreshes both the top-level hash and the matching map entry.
    - Subsequent grant for a DIFFERENT repo_root (workspace-internal
      trust under the same repo_id): leaves the top-level
      ``profile_sha256`` + ``repo_root`` alone and only writes/updates
      the per-root map entry. This preserves the original "root" trust
      while extending coverage to the workspace.
    """
    repo_root = profile_dir.parent
    try:
        repo_root_str = str(repo_root.resolve())
    except OSError:
        repo_root_str = str(repo_root)

    # Defense-in-depth at grant time for the two PROSE artifacts a committed profile
    # can poison: idioms.md (user-taught) and principles.md (derived). canonicals.json
    # / conventions.json are NOT scanned here: they carry real witness code where such
    # tokens (eval(), secret-looking literals, "you must" comments) are legitimate, so
    # a scan there false-positives and refuses trust on healthy repos. All profile
    # content is additionally sanitized at every <chameleon-context> render site.
    poisoned = injected_prose_artifact(profile_dir)
    if poisoned is not None:
        raise ProfileInjectionError(
            f"profile at {profile_dir}: {poisoned} contains an injection pattern; refusing trust"
        )

    new_hash = hash_profile(profile_dir)

    trust_path = repo_data_dir(repo_id) / ".trust"
    lock_path = trust_path.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Bounded acquisition: an unbounded wait here can wedge a session
        # behind whichever process holds the trust lock. Raising is correct
        # for both callers: the trust_profile tool surfaces an error envelope
        # and refresh-time trust preservation swallows it and skips.
        if not portable_flock_deadline(lock_fd, threshold_float("TRUST_LOCK_TIMEOUT_SECONDS")):
            raise LockHeldError(lock_path, None, None)

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        existing = trust_state_for(repo_id)
        if existing is None:
            record = TrustRecord(
                granted_at=now_iso,
                granted_by_user=_current_user(),
                profile_sha256=new_hash,
                repo_root=repo_root_str,
                repo_root_specific_hashes={repo_root_str: new_hash},
                repo_root_specific_granted_at={repo_root_str: now_iso},
            )
        else:
            specific = dict(existing.repo_root_specific_hashes)
            specific[repo_root_str] = new_hash
            # This root's own grant time, so a later query for THIS root reports
            # when it was actually trusted, not the first root's grant time.
            specific_times = dict(existing.repo_root_specific_granted_at)
            specific_times[repo_root_str] = now_iso
            if existing.repo_root == repo_root_str or not existing.repo_root:
                record = TrustRecord(
                    granted_at=now_iso,
                    granted_by_user=_current_user(),
                    profile_sha256=new_hash,
                    repo_root=repo_root_str,
                    repo_root_specific_hashes=specific,
                    repo_root_specific_granted_at=specific_times,
                )
            else:
                # A DIFFERENT root under the same (monorepo-shared) repo_id: keep
                # the top-level first-grant fields, only extend the per-root maps.
                record = TrustRecord(
                    granted_at=existing.granted_at,
                    granted_by_user=existing.granted_by_user,
                    profile_sha256=existing.profile_sha256,
                    repo_root=existing.repo_root,
                    repo_root_specific_hashes=specific,
                    repo_root_specific_granted_at=specific_times,
                )

        tmp_path = trust_path.with_suffix(".trust.tmp")
        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, trust_path)
    finally:
        portable_funlock(lock_fd)
        os.close(lock_fd)
    return record


def _trust_revalidation_enabled() -> bool:
    """Whether trust re-validates when the profile changes after a grant.

    Default OFF: trust is ONE-TIME. Once a repo is trusted it stays trusted across
    every later profile change (refresh, re-bootstrap, teach) and never goes
    "stale", so the user never re-grants. Set ``CHAMELEON_TRUST_REVALIDATE=1`` to
    restore the old behavior, where any change to the trust-hashed profile surface
    re-prompts for a fresh grant. Read at call time so it can be toggled per
    process / test.
    """
    return os.environ.get("CHAMELEON_TRUST_REVALIDATE") == "1"


def profile_diverged_from_grant(
    record: TrustRecord, repo_root: Path | str, profile_dir: Path
) -> bool:
    """True iff the profile changed since trust was granted AND re-validation is
    enabled. With trust persistence ON (the default), this is always False --
    trust is one-time and survives profile changes. The single funnel every
    staleness decision routes through, so the persistence policy is enforced in
    one place. An empty / unreadable current profile reads as not-diverged (no
    trustable surface to compare), matching the legacy hook guard.
    """
    if not _trust_revalidation_enabled():
        return False
    current = hash_profile(profile_dir)
    if not current:
        return False
    return record.hash_for_root(repo_root) != current


def is_material_change(repo_id: str, current_profile_dir: Path) -> bool:
    """Return True iff the trusted profile changed since grant AND re-validation
    is enabled (``CHAMELEON_TRUST_REVALIDATE=1``).

    With trust persistence ON (default), this is always False: trust is one-time
    and never goes stale. Under the kill switch it consults the per-root hash map
    via ``record.hash_for_root(current_profile_dir.parent)`` (Bug H6: a workspace
    with its own grant uses the workspace hash; otherwise it falls back to the
    root hash; legacy records keep single-hash-per-repo_id semantics).
    """
    record = trust_state_for(repo_id)
    if record is None:
        return False
    from chameleon_mcp.worktree import resolve_profile_root

    # A linked worktree's own .chameleon is absent; hash the main worktree's
    # profile it actually reads. resolve_profile_root is the identity for every
    # non-worktree dir, so this is unchanged off the worktree path.
    effective_dir = resolve_profile_root(current_profile_dir.parent) / ".chameleon"
    return profile_diverged_from_grant(record, current_profile_dir.parent, effective_dir)


def _current_user() -> str:
    """Best-effort current-user identification (for trust audit trail)."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")

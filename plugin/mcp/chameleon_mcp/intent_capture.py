"""Per-session intent capture: assertions and digests, never raw prose.

UserPromptSubmit hands the human-typed prompt remainder to ``capture_intent``,
which extracts checkable assertion tokens -- multi-digit numerals, compound
identifiers, quoted strings -- and appends them with a prompt digest to a
per-session NDJSON file under the repo's plugin-data dir. The Stop-path judge
routing reads these back: fresh checkable tokens force a reviewer spawn and
ride into its prompt so the review can cross-check the change against what was
actually asked.

Privacy posture, stated plainly: only extracted tokens, a 16-hex-char prompt
digest, and a bounded set of verbatim SCOPE-CONSTRAINT CLAUSES persist,
locally, in a 0700 directory with 0600 files, size- and retention-capped. The
deterministic hard-secret scanner runs over the whole prompt and over each
token before anything is written; a prompt that trips it persists only
``secret_suppressed: true`` with zero tokens and zero scope lines. That
scanner covers the known hard kinds only -- a credential matching no pattern
can still persist inside a quoted token or a scope line, which is why the
capture is bounded, swept after ``INTENT_RETENTION_DAYS``, and killable with
``CHAMELEON_INTENT_CAPTURE=0``.

The scope-line contract (``extract_scope_lines``, ``capture_intent``'s
``scope_lines`` field) is a second, narrower channel than the tokens above:
it never stores the prompt itself, only a bounded CLAUSE around each match of
an explicit scoping phrase ("don't touch X", "only change Y", "leave Z
alone", ...) -- narrowed off the matching sentence, not the whole (possibly
run-on) sentence, so an unrelated preceding clause and a trailing reason
clause are dropped while the scope object itself is never split -- each
re-scanned by the same hard-secret and credential-shape gates as every
token, capped in count and per-line length. It has its own kill switch,
``CHAMELEON_INTENT_CONTRACT=0``, independent of ``CHAMELEON_INTENT_CAPTURE``
(which disables capture -- tokens and scope lines both -- entirely).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.optouts import _safe_session_marker

# Quoted strings across double/single/backtick quotes: 2..80 chars, no newline,
# so a stray apostrophe cannot swallow a paragraph.
_QUOTED_RE = re.compile(r"\"([^\"\n]{2,80})\"|'([^'\n]{2,80})'|`([^`\n]{2,80})`")

# Two-plus-digit integers and decimals. Bare single digits ("fix the 2 bugs")
# are conversational noise that would force a judge spawn nearly every turn;
# the spec-constant class this targets (ports, limits, versions, amounts) is
# almost always multi-digit or decimal. A single digit is still captured when
# it sits in an explicit assignment-shaped position -- right after "to "/"="/
# ":" the way "set the retry limit to 7" or "retries=3" reads -- since that
# context is exactly the checkable-constant case a bare count like "2 bugs"
# never has. Quoted single digits still capture via the quoted-string pattern.
# Each assignment-shaped single-digit alternative also excludes a digit
# immediately followed by "/<digit>" -- a fraction or ratio numerator
# ("retries=1/3", "set to 1/2") is not a standalone checkable constant.
_NUMERAL_RE = re.compile(
    r"(?<![\w.])\d{2,}(?:\.\d+)?(?![\w.])"
    r"|(?<![\w.])\d\.\d+(?![\w.])"
    r"|(?<=\bto )\d(?![\w.])(?!/\d)"
    r"|(?<=[=:]\s)\d(?![\w.])(?!/\d)"
    r"|(?<=[=:])\d(?![\w.])(?!/\d)"
)

# Code-shaped identifiers only: dotted/underscored compounds, camelCase,
# CONSTANT_CASE, and slash-delimited path segments -- both the absolute form
# (e.g. "/api/v2/sync") and the far more common relative repo-path form a
# user actually types (e.g. "src/config/settings.ts"). The absolute form
# requires a leading slash and 2+ segments; the relative form requires 1+
# directory segments AND a dotted extension on the final segment, so it never
# fires on ordinary prose slashes ("and/or", "24/7", "n/a"), none of which end
# in a file extension. Plain English words never match.
_IDENTIFIER_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+\b"
    r"|\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b"
    r"|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
    r"|/[\w\-]+(?:/[\w\-]+)+"
    r"|\b(?:[\w\-]+/)+[\w\-]+\.[A-Za-z0-9]+\b"
)


# Explicit scoping phrases (case-insensitive): "don't/do not
# touch/modify/edit/change", "only change/modify/edit", "leave X alone",
# "keep X (as is)", "without changing/touching", "must not". A sentence
# matching one of these names an explicit boundary the request drew around
# the change -- the only class of prompt text the intent contract persists
# verbatim (never a paraphrase or a summary of "what the user wants").
_SCOPE_PHRASE_RE = re.compile(
    r"\b(?:don't|do not)\s+(?:touch|modify|edit|change)\b"
    r"|\bonly\s+(?:change|modify|edit)\b"
    r"|\bleave\b.{0,60}?\balone\b"
    r"|\bkeep\b.{0,60}?\bas[- ]is\b"
    r"|\bwithout\s+(?:changing|touching)\b"
    r"|\bmust\s+not\b",
    re.IGNORECASE,
)

# Coordinating/transitional conjunctions that mark the LEFT edge of a scope
# clause: text before the nearest one (relative to a scope-phrase match) is
# an unrelated preceding clause a punctuation-free run-on glued on, not part
# of what the phrase scopes. Commas/semicolons/colons are deliberately absent
# -- an object introduced via a comma ("the auth module, leave it alone")
# must survive, so only whole-word conjunctions cut.
_SCOPE_LEFT_BOUNDARY_RE = re.compile(
    r"\b(?:and|but|or|nor|so|yet|then|also|plus|because|additionally)\b",
    re.IGNORECASE,
)

# Reason markers that bound the RIGHT edge of a scope clause: text from here
# on is the run-on's justification, not the scope itself. Deliberately
# narrower than the left set -- "and"/"or"/"but" never cut on the right,
# since a scope object can be compound ("don't touch A and B") and cutting
# there would drop part of the object.
_SCOPE_RIGHT_BOUNDARY_RE = re.compile(r"\b(?:because|since)\b", re.IGNORECASE)

# Sentence terminators for the scope split. `!`, `?`, and newline always
# terminate; a `.` terminates ONLY when it is not an identifier-internal dot
# -- a period flanked by word characters on both sides (`config.json`,
# `v1.2.3`, `foo.bar.baz`) is part of a filename/module/version a scope
# directive names ("don't touch config.json"), not a sentence boundary, so
# splitting there would drop the object's extension. The two `.`-alternatives
# fire only when the period lacks a word char on one side; a flat alternation,
# linear in input length (no catastrophic backtracking).
#
# Accepted residual: a sentence period with NO trailing space before the next
# word ("config.json.Also do X") is, by local context alone, indistinguishable
# from a dotted identifier ("app.UserModel", "Module.Handler"), so it is kept
# whole rather than split. That deliberately favors never dropping a real scope
# OBJECT over splitting a rare no-space run-on; the merged run-on is still
# bounded by the per-clause char cap and narrowed by `_scope_clause_bounds`.
_SENTENCE_SPLIT_RE = re.compile(r"[!?\n]|(?<!\w)\.|\.(?!\w)")


def _scope_clause_bounds(sentence: str, start: int, end: int) -> tuple[int, int]:
    """Return the ``[left, right)`` bounds of the scope clause around a
    ``_SCOPE_PHRASE_RE`` match spanning ``[start, end)`` in ``sentence``.

    Left bound: the end of the nearest ``_SCOPE_LEFT_BOUNDARY_RE`` word
    found strictly before ``start`` (cuts AFTER the conjunction, dropping
    the unrelated clause before it), else ``0``. Right bound: the start of
    the nearest ``_SCOPE_RIGHT_BOUNDARY_RE`` word found at or after ``end``
    (drops a trailing reason clause), else ``len(sentence)``. Both scans are
    bounded to the region outside ``[start, end)``, so a conjunction inside
    the phrase's own match span is never treated as a boundary. Both regexes
    are flat word alternations -- linear in sentence length, no nested
    quantifiers, so this is safe on adversarial input.
    """
    left = 0
    for m in _SCOPE_LEFT_BOUNDARY_RE.finditer(sentence, 0, start):
        left = m.end()
    right_match = _SCOPE_RIGHT_BOUNDARY_RE.search(sentence, end)
    right = right_match.start() if right_match else len(sentence)
    return left, right


def extract_scope_lines(text: str) -> list[str]:
    """Extract verbatim scope-constraint CLAUSES from prompt text.

    Splits ``text`` into sentences on ``_SENTENCE_SPLIT_RE`` (``!``/``?``/
    newline, and ``.`` except when it is an identifier-internal dot such as
    ``config.json`` or ``v1.2.3`` -- so a filename/module a directive names
    is not split off its object) and matches ``_SCOPE_PHRASE_RE`` against
    each FULL sentence (this is essential: it
    protects multi-keyword phrases like "leave X alone" / "keep X as is"
    whose regex span crosses commas). For each match, ``_scope_clause_bounds``
    narrows the persisted text to a bounded clause around the phrase --
    dropping an unrelated preceding clause glued on by a coordinating
    conjunction ("...refactor the payment flow AND while you are at it
    don't touch X") and a trailing reason clause ("...don't touch X BECAUSE
    it is fragile") -- instead of the whole (possibly run-on) sentence.
    Every value inside the phrase's own match span is always kept intact
    (a boundary word inside the span is never treated as a cut point), and
    the right-hand scan never cuts on "and"/"or"/"but" so a compound scope
    object ("don't touch A and B") is never split in half. UNALTERED beyond
    the boundary trim and a ``str.strip()`` -- never paraphrased, reworded,
    or summarized. A sentence with multiple matches yields one clause per
    match, and identical clauses are deduped (order-preserving) before the
    count cap below.

    Clauses are capped at ``INTENT_SCOPE_LINES_MAX`` (earliest-first,
    applied BEFORE the per-line char cap and the secret scan below, so the
    cap governs how many candidate clauses are considered at all, not how
    many survive scanning). Each surviving candidate is then truncated to
    ``INTENT_SCOPE_LINE_MAX_CHARS`` -- truncation only ever shortens a
    verbatim prefix, it never rewrites -- and the truncated (i.e. the exact
    text that would persist) is re-scanned with ``_has_hard_secret`` and
    ``_looks_credential_shaped``; a line tripping either is dropped, the
    same persistence gate ``capture_intent`` runs over every extracted
    token. A prompt naming no scoping phrase returns ``[]``.

    Gated on ``CHAMELEON_INTENT_CONTRACT``: returns ``[]`` unconditionally
    when set to ``"0"``, independent of ``CHAMELEON_INTENT_CAPTURE`` (which
    gates capture entirely). Fails open to ``[]`` on any error -- this must
    never raise into ``capture_intent``'s hot path.
    """
    if os.environ.get("CHAMELEON_INTENT_CONTRACT") == "0":
        return []
    try:
        if not isinstance(text, str) or not text:
            return []
        candidates: list[str] = []
        seen: set[str] = set()
        for segment in _SENTENCE_SPLIT_RE.split(text):
            sentence = segment.strip()
            if not sentence:
                continue
            for m in _SCOPE_PHRASE_RE.finditer(sentence):
                left, right = _scope_clause_bounds(sentence, m.start(), m.end())
                clause = sentence[left:right].strip()
                if clause and clause not in seen:
                    seen.add(clause)
                    candidates.append(clause)
        cap_count = threshold_int("INTENT_SCOPE_LINES_MAX")
        cap_chars = threshold_int("INTENT_SCOPE_LINE_MAX_CHARS")
        out: list[str] = []
        for line in candidates[:cap_count]:
            if len(line) > cap_chars:
                line = line[:cap_chars]
            if _has_hard_secret(line) or _looks_credential_shaped(line):
                continue
            out.append(line)
        return out
    except Exception:
        return []


def _intent_path(repo_data: Path, session_id: str | None) -> Path:
    return Path(repo_data) / f".intent.{_safe_session_marker(session_id)}.ndjson"


def extract_assertions(text: str) -> dict[str, list[str]]:
    """Extract checkable tokens from prompt text, deduped and capped.

    Returns ``{"numerals": [...], "identifiers": [...], "quoted": [...]}`` with
    order-preserving dedupe across all three buckets and the total token count
    capped at ``INTENT_MAX_TOKENS_PER_PROMPT``.
    """
    out: dict[str, list[str]] = {"numerals": [], "identifiers": [], "quoted": []}
    if not isinstance(text, str) or not text:
        return out
    cap = threshold_int("INTENT_MAX_TOKENS_PER_PROMPT")
    seen: set[str] = set()
    total = 0

    def _add(bucket: str, value: str | None) -> None:
        nonlocal total
        if total >= cap or not value or value in seen:
            return
        seen.add(value)
        out[bucket].append(value)
        total += 1

    for m in _NUMERAL_RE.finditer(text):
        _add("numerals", m.group(0))
    for m in _IDENTIFIER_RE.finditer(text):
        _add("identifiers", m.group(0))
    for m in _QUOTED_RE.finditer(text):
        _add("quoted", m.group(1) or m.group(2) or m.group(3))
    return out


def _mentions_guard_construct(text: str) -> bool:
    """True when the prompt names a security-guard construct.

    Uses the shared removed-invariant lexicon (``autopass.GUARD_LEXICON``:
    before_action, verify_* callbacks, authorization, csrf, ...), defined once
    there so the diff-side and intent-side security lenses cannot drift. A
    request that talks about guards is security-relevant even when it carries
    no extractable token. Fails open to False.
    """
    try:
        from chameleon_mcp.autopass import GUARD_LEXICON

        return any(rx.search(text) for rx in GUARD_LEXICON)
    except Exception:
        return False


def _has_hard_secret(text: str) -> bool:
    """True when the deterministic hard-secret scanner fires on ``text``.

    Deliberately does NOT consult the inline-ignore index: a prompt-borne
    ``chameleon-ignore`` directive must not defeat redaction. This is the one
    intentional divergence from the file-content scan in the hook path, which
    honors rule-named directives because there the directive lives in reviewed
    source, not in an arbitrary prompt. Fails CLOSED: a scanner error reads as
    "secret present" so nothing persists on doubt.
    """
    try:
        from chameleon_mcp.lint_engine import scan_hard_secrets
        from chameleon_mcp.violation_class import is_hard_class, tag_secret_hardness

        violations = [v.to_dict() for v in scan_hard_secrets(text)]
        if not violations:
            return False
        tag_secret_hardness(violations)
        return any(is_hard_class(v) for v in violations)
    except Exception:
        return True


# Known credential prefixes, matched loosely on purpose: the deterministic
# scanner above requires exact token lengths (a real ghp_ PAT is 36 chars),
# so an over-long paste, a truncated copy, or a newer format the patterns do
# not know yet (GitHub fine-grained github_pat_ tokens) would persist
# verbatim. Persistence has the opposite risk asymmetry from lint: a false
# suppress costs one routing token, a false persist writes a credential to
# disk. This gate is therefore greedy and applies ONLY here, never to the
# calibrated lint path where its false positives would be unacceptable.
_CREDENTIAL_PREFIX_RE = re.compile(
    r"^(?:"
    r"gh[pousr]_"
    r"|github_pat_"
    r"|glpat-"
    r"|xox[a-z]-"
    r"|sk-[A-Za-z0-9]"
    r"|[spr]k_(?:live|test)_"
    r"|AKIA|ASIA"
    r"|AIza"
    r"|ya29\."
    r"|eyJ[A-Za-z0-9_\-]{10,}"
    r")"
)


def _looks_credential_shaped(token: str) -> bool:
    """Greedy persistence gate for tokens that resemble credentials.

    True for any token carrying a known credential prefix, and for long
    single-token strings mixing upper, lower, and digits (the shape of an
    opaque key, almost never of a hand-written identifier). Fails open to
    True on error: nothing persists on doubt.
    """
    try:
        if len(token) < 12:
            return False
        if _CREDENTIAL_PREFIX_RE.match(token):
            return True
        if len(token) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-]+", token):
            has_digit = any(c.isdigit() for c in token)
            has_upper = any(c.isupper() for c in token)
            has_lower = any(c.islower() for c in token)
            return has_digit and has_upper and has_lower
        return False
    except Exception:
        return True


def capture_intent(repo_data: Path, session_id: str | None, prompt_text: str) -> None:
    """Append one intent entry for a prompt. Never raises.

    A prompt carrying a hard secret persists ``secret_suppressed: true`` with
    zero tokens (the suppression itself is signal for the judge routing's
    honesty, and the digest still allows dedupe). Otherwise each extracted
    token is individually re-scanned and dropped if it alone trips the
    scanner. A prompt whose surviving token lists are all empty STILL appends
    an empty-token entry: it marks the turn's request as having named nothing
    checkable, which the scope-drift advisory relies on (a stale earlier
    prompt's identifiers must not govern a later bare "commit this" turn),
    and it is a no-op for every token reader. Only the digest, flags, and
    ``scope_lines`` persist -- never full prompt prose. ``scope_lines`` is
    itself bounded and secret-scanned by ``extract_scope_lines`` and is
    unconditionally empty on the suppressed branch.
    """
    try:
        if not isinstance(prompt_text, str) or not prompt_text:
            return
        digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]
        security = _mentions_guard_construct(prompt_text)
        if _has_hard_secret(prompt_text):
            entry = {
                "ts": time.time(),
                "prompt_digest": digest,
                "secret_suppressed": True,
                "tokens": {},
                "security": security,
                # Never extract scope lines from a secret-bearing prompt: a
                # matched scoping sentence could itself carry the secret the
                # suppression above just refused to persist.
                "scope_lines": [],
            }
        else:
            tokens = extract_assertions(prompt_text)
            tokens = {
                k: [t for t in v if not _has_hard_secret(t) and not _looks_credential_shaped(t)]
                for k, v in tokens.items()
            }
            entry = {
                "ts": time.time(),
                "prompt_digest": digest,
                "secret_suppressed": False,
                "tokens": tokens,
                "security": security,
                "scope_lines": extract_scope_lines(prompt_text),
            }

        path = _intent_path(repo_data, session_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _trim_intent_file(path, threshold_int("INTENT_FILE_MAX_BYTES"))
    except Exception:
        return


def _trim_intent_file(path: Path, max_bytes: int) -> None:
    """Drop oldest lines once the file exceeds ``max_bytes``. Best-effort.

    Newest intent is the routing-relevant intent, so trimming is oldest-first.
    Atomic tmp + rename so a reader never sees a partial file.
    """
    try:
        if max_bytes <= 0 or path.stat().st_size <= max_bytes:
            return
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            n = len(line.encode("utf-8"))
            if kept and used + n > max_bytes:
                break
            kept.append(line)
            used += n
        kept.reverse()
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()  # type: ignore[possibly-undefined]
        except (OSError, UnboundLocalError):
            pass


def read_intent(repo_data: Path, session_id: str | None) -> list[dict]:
    """Parse the session's intent NDJSON, skipping corrupt lines. Fail-open []."""
    try:
        path = _intent_path(repo_data, session_id)
        if not path.is_file():
            return []
        entries: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
        return entries
    except Exception:
        return []


def checkable_tokens(entries: list[dict], since_ts: float | None = None) -> list[str]:
    """Flattened token values from non-suppressed entries newer than ``since_ts``.

    ``since_ts=None`` means all entries. Order-preserving dedupe across entries
    and buckets, so the judge prompt sees each value once, oldest-first.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("secret_suppressed"):
            continue
        ts = entry.get("ts")
        if since_ts is not None and not (isinstance(ts, (int, float)) and ts > since_ts):
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            continue
        for bucket in ("numerals", "identifiers", "quoted"):
            for value in tokens.get(bucket) or []:
                if isinstance(value, str) and value not in seen:
                    seen.add(value)
                    out.append(value)
    return out


def scope_lines(entries: list[dict], since_ts: float | None = None) -> list[str]:
    """Flattened verbatim scope lines from non-suppressed entries newer than
    ``since_ts``.

    Mirrors ``checkable_tokens``'s shape exactly: ``since_ts=None`` means all
    entries, order-preserving dedupe across entries so the review job's
    intent contract sees each scope line once, oldest-first. An entry
    captured before this field existed (or one whose ``scope_lines`` is
    missing or malformed) contributes nothing, never an error.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("secret_suppressed"):
            continue
        ts = entry.get("ts")
        if since_ts is not None and not (isinstance(ts, (int, float)) and ts > since_ts):
            continue
        for value in entry.get("scope_lines") or []:
            if isinstance(value, str) and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def recent_excerpts(entries: list[dict], since_ts: float | None = None) -> list[str]:
    """Verbatim prompt excerpts for the review job's intent contract.

    v1 stores no separate full-prompt prose -- ``capture_intent`` persists
    only extracted tokens, digests, and the bounded, secret-scanned scope
    lines a prompt's scoping sentences produced (``extract_scope_lines``).
    The intent contract's "recent excerpts" are therefore exactly those
    retained scope lines; this is a thin alias over ``scope_lines`` so a
    later revision that captures a wider verbatim excerpt window can extend
    this function alone without touching any caller.
    """
    return scope_lines(entries, since_ts)


def identifier_tokens(entries: list[dict], since_ts: float | None = None) -> list[str]:
    """Identifier-bucket token values from non-suppressed entries newer than ``since_ts``.

    Order-preserving dedupe. Identifiers are the scope anchors (symbol / file /
    module names) the request named; ``scope_drift_files`` uses them to flag a
    changed file that shares no name with anything the request mentioned.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("secret_suppressed"):
            continue
        ts = entry.get("ts")
        if since_ts is not None and not (isinstance(ts, (int, float)) and ts > since_ts):
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            continue
        for value in tokens.get("identifiers") or []:
            if isinstance(value, str) and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def latest_request_identifiers(entries: list[dict]) -> list[str]:
    """Identifier tokens of the LATEST captured prompt -- the turn's governing request.

    The scope-drift advisory compares changed files against what "the request"
    named, and that request is the most recent prompt, not the whole session:
    stale identifiers from an earlier prompt must not govern a later turn (a
    bare "commit this" turn scored against the first prompt's file names flags
    everything else the session touched, repeatedly). The newest entry alone
    decides. Token-less (captured with empty buckets), secret-suppressed, or
    malformed newest entry -> [] -> the advisory stays silent; it never falls
    back to an older prompt's tokens.
    """
    for entry in reversed(entries or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("secret_suppressed"):
            return []
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            return []
        return [v for v in (tokens.get("identifiers") or []) if isinstance(v, str)]
    return []


# Path noise that must not count as scope overlap between a request identifier and
# a changed file path (extensions and ubiquitous directory names).
_GENERIC_PATH_TOKENS = frozenset(
    {
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "py",
        "rb",
        "src",
        "lib",
        "app",
        "index",
        "test",
        "tests",
        "spec",
        "specs",
        "mod",
        "init",
    }
)


def _scope_word_tokens(s: str) -> set[str]:
    """Lowercase word tokens of a path or identifier.

    Splits on non-alphanumerics and camelCase boundaries, drops generic path noise
    and tokens shorter than 3 chars, so ``AuthService`` and ``auth/service.ts``
    both yield ``{auth, service}``.
    """
    parts = re.split(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])", s or "")
    return {p.lower() for p in parts if len(p) >= 3 and p.lower() not in _GENERIC_PATH_TOKENS}


def scope_drift_files(
    intent_identifiers: list[str],
    changed_rel_paths: list[str],
    *,
    min_intent_tokens: int = 2,
    max_flagged: int = 5,
) -> list[str]:
    """Changed files that look unrequested relative to the captured intent.

    Returns the changed paths whose words share nothing with any identifier the
    request named. Stays empty unless the request named at least
    ``min_intent_tokens`` distinct words AND at least one changed file DID overlap
    -- without the overlap gate, a turn whose captured intent belonged to an
    earlier prompt would flag every file. Sorted, capped at ``max_flagged``.
    """
    intent_words: set[str] = set()
    for tok in intent_identifiers or []:
        if isinstance(tok, str):
            intent_words |= _scope_word_tokens(tok)
    if len(intent_words) < max(1, min_intent_tokens):
        return []
    per_path = {p: _scope_word_tokens(p) for p in (changed_rel_paths or []) if isinstance(p, str)}
    if not any(words & intent_words for words in per_path.values()):
        return []
    drifted = [p for p, words in per_path.items() if not (words & intent_words)]
    return sorted(drifted)[: max(0, max_flagged)]


def security_intent_seen(entries: list[dict], since_ts: float | None = None) -> bool:
    """True when any entry newer than ``since_ts`` carries the security flag.

    Suppressed entries count too: a secret-bearing prompt that talked about
    guards is exactly the request that warrants a forced review, and the flag
    is a derived boolean that exposes none of the redacted content.
    """
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        if since_ts is not None and not (isinstance(ts, (int, float)) and ts > since_ts):
            continue
        if entry.get("security"):
            return True
    return False


def reap_stale_prefixed(repo_data: Path, prefixes: tuple[str, ...], max_age_seconds: int) -> int:
    """Best-effort removal of old per-session working files by name prefix.

    The intent files, judge request/in-flight/pending markers, and judged-digest
    markers all share the session-marker lifecycle: no SessionEnd hook exists,
    so SessionStart sweeps anything older than the retention horizon. Skips
    ``.tmp`` names (a writer may be mid-rename) and non-files; never raises.
    Returns the count removed.
    """
    try:
        entries = list(Path(repo_data).iterdir())
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for p in entries:
        name = p.name
        if name.endswith(".tmp") or not any(name.startswith(pref) for pref in prefixes):
            continue
        try:
            if not p.is_file() or now - p.stat().st_mtime <= max_age_seconds:
                continue
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed

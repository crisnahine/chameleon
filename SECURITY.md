# Security Policy

## Supported versions

Security fixes land on the latest released minor. Update to the most recent
`chameleon` release before reporting, since the issue may already be fixed.

| Version | Supported |
| ------- | --------- |
| latest  | yes       |
| older   | no        |

## Reporting a vulnerability

Report privately through GitHub: open a
[security advisory](https://github.com/crisnahine/chameleon/security/advisories/new)
on this repository. Do not open a public issue for a security problem.

Include what the issue is, how to reproduce it, and the impact you see. You will
get an acknowledgement within a few days. Once a fix is ready it ships in a patch
release and the advisory is published.

## Scope

chameleon runs locally and treats the repositories it analyzes as untrusted
input. The most useful reports are a committed profile or repo that can steer
the context chameleon injects or slip past the trust gate, a hook that fails
unsafe, or any path that exfiltrates code or executes repository code without an
explicit opt-in (`CHAMELEON_ALLOW_DEP_AUDIT`, `CHAMELEON_ALLOW_TESTS`,
`CHAMELEON_ALLOW_TSC`, `CHAMELEON_ALLOW_ESLINT_EVAL`).

Two outbound network paths are on by default. Neither sends your code
anywhere; both only download inputs chameleon needs:

1. At refresh time, a repo with a locked `production_ref` (or one whose clean,
   origin-backed production branch is about to be locked by the one-time
   migration) runs a bounded, timeout-capped, non-interactive
   `git fetch origin <branch>` against the repo's own remote, so derivation
   sees the latest production tip. It downloads your own repo's commits from
   your own origin and uploads nothing. It self-suppresses under CI, never
   runs on a hook hot path, fails open to the last-fetched ref, and is killed
   with `CHAMELEON_FETCH_PRODUCTION_REF=0`.
2. The TypeScript parser provisions chameleon's own pinned `typescript`
   dependency from the npm registry (`npm ci`, with an `npm install`
   fallback) into `~/.local/share/chameleon/node-deps/<version>/`. This
   installs the plugin's lockfile-pinned parser, not anything from the
   analyzed repo. It runs once per plugin version, lazily on the first TS
   parse that needs it — usually the first bootstrap of a TypeScript repo,
   but after a plugin upgrade the first TS parse can be a later tool call or
   turn-end review.

Separately, the default-on turn-end reviewers (correctness judge, duplication
confirm, round-3 refuter) spawn `claude -p` subprocesses, which reach the
model API through your existing Claude Code authentication. That is the model
runtime's own network use, not a chameleon HTTP call, but it is outbound
traffic a hardened environment should account for; the spawns are killed by
their own flags (the `enforcement.*` config keys and `CHAMELEON_*` switches
documented in `docs/architecture.md`).

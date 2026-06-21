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

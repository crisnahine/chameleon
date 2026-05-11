# Codex CLI (OpenAI) marketplace submission checklist

Submit chameleon to OpenAI's Codex CLI plugin marketplace so users can
install via `/plugins` search instead of the manual local-clone path.

## Status

- [ ] Not yet submitted (as of v0.5.0, 2026-05-11)

## Where to submit

OpenAI's plugin marketplace repo: **https://github.com/openai/plugins**

OpenAI runs Codex CLI's plugin index as a GitHub repo. Submission is a
**pull request** against that repo adding an entry for chameleon.

## Submission flow

1. Fork `openai/plugins` to your GitHub account.
2. Add a new entry in their plugin index file (likely `plugins/chameleon.json`
   or `plugins.json` — check the repo's `CONTRIBUTING.md` for the exact path
   when filing).
3. The entry references:
   - `name`: `chameleon`
   - `displayName`: `Chameleon`
   - `description`: same short description used for Cursor
   - `repository`: `https://github.com/crisnahine/chameleon`
   - `manifest`: `.codex-plugin/plugin.json`
   - `license`: `MIT`
   - `category`: `coding` / `developer-tools`
   - `maintainer`: `crisjosephnahine@gmail.com`
4. Open a PR with the title `Add chameleon plugin` and a body matching the
   `openai/plugins` PR template.

## What OpenAI typically asks in PR review

- **Acceptance test transcript**: open a clean Codex CLI session inside a
  TypeScript repo, send the exact user message `Let's make a react todo list`
  (or any free-form coding ask), and prove that the using-chameleon skill
  triggers BEFORE Codex writes code. Paste the transcript in the PR body.
  This is the same acceptance bar superpowers uses (see
  `superpowers/CLAUDE.md`).
- **Security model**: confirm no external network calls; profile data stays
  in-repo; trust grants stay per-user under `~/.local/share/chameleon/`.
- **CHANGELOG link**: deep-link to the current release.

## Pre-submission checklist

- [ ] `.codex-plugin/plugin.json` validates against Codex's schema (Codex
      CLI ships a `codex plugin validate <path>` command; run it)
- [ ] Install chameleon locally into Codex CLI (see [Local verification](#local-verification))
- [ ] Capture the acceptance-test transcript described above
- [ ] Version in `.codex-plugin/plugin.json` matches latest tag (`0.5.0`)
- [ ] CHANGELOG has an unambiguous entry for the version being submitted

## Local verification

1. Install Codex CLI: `npm install -g @openai/codex-cli` (or whatever the
   current install command is per OpenAI's docs at the time of submission).
2. From the chameleon repo root, point Codex at the plugin:
   ```bash
   codex plugin install ./.codex-plugin/plugin.json
   ```
   (or whatever Codex's local-install command is — check `codex plugin --help`)
3. Verify the 7 user-invocable slash commands appear in Codex's command
   palette.
4. Run `/chameleon-init` on a TS or Rails repo.
5. Run `/chameleon-trust`.
6. Edit a file via Codex's edit flow. Verify `<chameleon-context>` is
   injected (Codex's stream-json output should show the additionalContext).
7. Capture the transcript as a `.txt` for the PR body.

If anything in steps 3–6 doesn't work, file a bug; do NOT submit until clean.

## After PR submission

- OpenAI's review is **human + automated**. First response often arrives
  within 1–2 weeks.
- Address review comments inline. Common feedback:
  - "The description claims X but the transcript shows Y" — tighten the
    description or capture a better transcript
  - "How does this interact with Codex's existing tool surface?" — explain
    the MCP-tool surface from `mcp/chameleon_mcp/tools.py`

## After merge

- [ ] Update README's Codex CLI section to drop the "pending marketplace
      listing" note
- [ ] Update CHANGELOG
- [ ] Verify `/plugins` search inside Codex CLI surfaces chameleon

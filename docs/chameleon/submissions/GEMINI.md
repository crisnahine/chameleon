# Gemini CLI (Google) extension marketplace submission checklist

Gemini CLI calls plugins "extensions." They install via
`gemini extensions install <URL>`. The URL can be ANY public GitHub repo
that ships a `gemini-extension.json` at its root — which chameleon already
does. **No formal submission is strictly required** for users to install,
but submission to Google's curated extension index increases discovery.

## Status

- [x] Direct-install URL works today: `gemini extensions install https://github.com/crisnahine/chameleon`
      — anyone with Gemini CLI can install chameleon without our doing anything.
- [ ] Not yet submitted to Google's curated extension catalog.

## Where to submit (curated catalog)

Google maintains an extension index. As of this writing the submission
endpoint is documented at:
**https://cloud.google.com/gemini/docs/cli/extensions/publish**

(URL may have moved; navigate from
https://cloud.google.com/gemini/docs/cli → Extensions → Publishing.)

## Submission flow

Google's process is typically:

1. **Verify the extension manifest** — `gemini extensions validate /path/to/chameleon` from a local clone. This must pass before submission.
2. **Sign in to the Cloud Console** with the same Google account that
   owns the GitHub repo.
3. **Fill out the publisher form** — description, repository URL,
   category, license, maintainer contact.
4. **Provide a verification token** — Google may ask to verify domain
   ownership of the github.com/crisnahine namespace via a special file
   in the repo. They'll instruct you on the file's name + contents.
5. **Wait for review** — typically 1–3 weeks.

## What's in chameleon's `gemini-extension.json` today

Already in repo root:

```json
{
  "name": "chameleon",
  "description": "Archetype-aware coding assistant for TypeScript and Ruby on Rails repos.",
  "version": "0.5.0",
  "contextFileName": "GEMINI.md"
}
```

`GEMINI.md` already exists at repo root and points the model at chameleon's
runtime behavior.

## Pre-submission checklist

- [ ] `gemini-extension.json` version matches the latest tag (currently 0.5.0)
- [ ] `GEMINI.md` exists and is current (it does)
- [ ] Local install works: `gemini extensions install <path>` from a clone
- [ ] Hook the local install up to a real TS or Rails repo and verify the
      using-chameleon skill triggers when the user starts an edit
- [ ] Capture a transcript for the publisher form

## Local verification

1. Install Gemini CLI: `npm install -g @google/gemini-cli` (or whatever
   the current install command is per Google's docs at the time of
   submission).
2. From a local chameleon clone, run:
   ```bash
   gemini extensions install file:///path/to/chameleon
   ```
   Or test the public URL path:
   ```bash
   gemini extensions install https://github.com/crisnahine/chameleon
   ```
3. Confirm `gemini extensions list` shows `chameleon`.
4. Open Gemini in a TS or Rails repo and run `/chameleon-init`.
5. Run `/chameleon-trust`.
6. Edit a file. Verify the model reads `GEMINI.md` (chameleon's
   `<chameleon-context>` should be visible in Gemini's verbose-mode output).
7. Capture the session transcript.

If anything fails, file a bug; do NOT submit until clean.

## After submission

- Google's review queue: **1–3 weeks** typical.
- Feedback patterns:
  - "Clarify what the plugin does without chameleon-specific vocabulary"
    → link to `docs/chameleon/VOCABULARY-AND-COMPETITIVE.md`
  - "Privacy statement" → point at this doc + the local-only
    architecture (no telemetry, no external calls)

## After approval

- [ ] Update README's Gemini CLI section
- [ ] Update CHANGELOG with the curated-catalog listing
- [ ] Confirm `gemini extensions search chameleon` surfaces the entry

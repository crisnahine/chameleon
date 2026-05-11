# Cursor marketplace submission checklist

Submit chameleon to Cursor's plugin marketplace so users can install via
`/add-plugin chameleon` instead of the manual local-clone path.

## Status

- [ ] Not yet submitted (as of v0.5.0, 2026-05-11)

## Where to submit

Cursor's plugin submission portal: **https://docs.cursor.com/plugins/publish**
(double-check the URL when filing; Cursor occasionally restructures docs).

Falls back to: https://www.cursor.com/ → Docs → Plugins → "Submit a plugin."

## What Cursor needs in the submission form

1. **Plugin name**: `chameleon`
2. **Display name**: `Chameleon`
3. **Description (short, < 200 chars)**:
   > Auto-derives codebase conventions and injects archetype-aware code
   > guidance per edit. TypeScript and Ruby on Rails.
4. **Manifest path in repo**: `.cursor-plugin/plugin.json`
5. **Repository URL**: `https://github.com/crisnahine/chameleon`
6. **License**: MIT
7. **Category**: Coding / Code quality / AI developer tools
8. **Brand color**: `#10B981` (the green already in `.cursor-plugin/plugin.json`)
9. **Long description**: Reuse the README's "Why" + "How it works" sections.
10. **Privacy / data handling note**: All processing is local. The plugin runs
    a Python MCP server inside the user's session; no telemetry; no data
    leaves the user's machine.

## What Cursor may require beyond the form

- **A demo screenshot or GIF** — record one of: `/chameleon-init` running on a
  TS repo, OR the `<chameleon-context>` injection visible in a Cursor edit.
- **An installation walkthrough** — point them at `INSTALL.md`.
- **A maintainer contact** — `crisjosephnahine@gmail.com`.

## Pre-submission checklist

- [ ] `.cursor-plugin/plugin.json` validates against Cursor's schema (open
      Cursor → Settings → Plugins → "Validate local plugin" feature, point
      at the repo, confirm no errors)
- [ ] README's "Cursor" section in `README.md#cursor` has the install
      command Cursor's marketplace expects (`/add-plugin chameleon`)
- [ ] The plugin loads cleanly in Cursor when added via the local-plugin
      path (verify before submitting; see [Local verification](#local-verification))
- [ ] License is set to MIT in `.cursor-plugin/plugin.json` (it is)
- [ ] Version field matches the latest tagged release (currently `0.5.0`)

## Local verification (do this BEFORE submitting)

1. Install Cursor desktop (https://www.cursor.com/downloads).
2. Open Cursor, then a TS or Rails repo.
3. Settings → Plugins → "Add local plugin" → point at the chameleon repo.
4. Confirm the 7 user-invocable slash commands appear in the Cursor Agent
   palette: `/chameleon-init`, `/chameleon-trust`, `/chameleon-status`,
   `/chameleon-teach`, `/chameleon-refresh`, `/chameleon-disable`,
   `/chameleon-pause-15m`.
5. Run `/chameleon-init` on the repo. Verify `.chameleon/profile.json` appears.
6. Run `/chameleon-trust`. Verify trust state flips.
7. Edit a file. Verify Cursor surfaces the `<chameleon-context>` block (or
   that the model references the canonical example).
8. Capture a screenshot of step 7 for the submission.

If anything in steps 4–7 doesn't work, file a bug; do NOT submit until clean.

## After submission

Cursor's review queue is human-driven; expect **1–4 weeks** for first
response. Common feedback patterns to anticipate:

- "Add more demo content / screenshots" — record more GIFs
- "Clarify the privacy policy" — point at this doc + the LICENSE
- "What does 'archetype' mean?" — link to
  `docs/chameleon/VOCABULARY-AND-COMPETITIVE.md`

## After approval

- [ ] Update README's Cursor section to drop the "pending marketplace
      listing" note
- [ ] Update CHANGELOG with an "Added — Cursor marketplace listing" line
      in the next patch release
- [ ] Tag a fresh release (`v0.5.x`) so the marketplace pulls a fresh
      version automatically

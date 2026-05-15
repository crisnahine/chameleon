# Chameleon

Chameleon is an archetype-aware coding assistant for TypeScript and Ruby on Rails repositories. When a chameleon profile is committed to `.chameleon/` in a repo, the model gets per-file context: which archetype the file belongs to, the canonical witness example, the team's rules from `.prettierrc`/`tsconfig.json`/`.rubocop.yml`, and any captured idioms.

Slash commands (Gemini extension):

- Use the documented chameleon slash commands when explicitly invoked by the user. Each one has a corresponding skill at `skills/<command-name>/SKILL.md` describing its flow.
- Before any code edit in a chameleon-enabled repo, look up the file's archetype + canonical via the `chameleon-mcp` server and shape your output to match.

For full design + invariants, see `docs/architecture.md`. For setup, see `docs/install.md`.

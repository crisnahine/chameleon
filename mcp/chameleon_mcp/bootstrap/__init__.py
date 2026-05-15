"""Bootstrap engine for chameleon.

Coordinates AST scan + clustering + interactive interview + atomic profile commit.
Invoked by `bootstrap_repo` and `refresh_repo` MCP tools.

See docs/architecture.md:
- "Bootstrap interview flow" — the ≤3-prompt user-facing flow
- "Atomicity & Crash Safety" — atomic transaction protocol
- "Cluster signature function" — clustering algorithm
"""

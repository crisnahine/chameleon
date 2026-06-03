# Installing chameleon

chameleon is a Claude Code plugin that learns your repo's conventions and feeds the model archetype-aware context on every edit. It supports TypeScript and Ruby on Rails repos.

Two ways to read this guide:

- **You already have `uv` and Node.js 20+.** Jump to [Quick path](#quick-path). Two commands and you are done.
- **Fresh machine, or you are not sure what you have.** Start at [Full setup](#full-setup). It walks every prerequisite with copy-paste commands and a check after each one.

Working on chameleon itself (not just using it)? See [CONTRIBUTING.md](../.github/CONTRIBUTING.md) instead. This guide is for users.

---

## Quick path

You have `uv` on your `PATH` and Node.js 20 or newer. Inside any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. Done. Verify it worked: [Verify the plugin loaded](#verify-the-plugin-loaded).

Editing Ruby on Rails repos too? You also need Ruby 3.0+ with the `prism` gem. The [Full setup](#full-setup) section has the per-OS commands.

---

## Full setup

### What you need

| Tool | What it does | Do you need it? |
|---|---|---|
| Claude Code | the harness chameleon plugs into | Always |
| `uv` | runs chameleon's Python server | Always |
| Node.js 20+ | reads TypeScript/JavaScript files | Always |
| Ruby 3.0+ with `prism` | reads Ruby files | Only if you edit Rails repos |

You never run `uv sync` or `npm install` by hand. chameleon builds its own Python environment and Node dependencies the first time it runs. You only install the three tools above; chameleon handles the rest. See [How dependencies resolve](#how-dependencies-resolve) if you want the detail.

The steps below are split by operating system. Do the one that matches your machine, then go to [Verify your prerequisites](#verify-your-prerequisites).

### macOS

These commands use [Homebrew](https://brew.sh). If you do not have it, install it first:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then install `uv` and Node.js:

```bash
brew install uv node
```

`brew install node` gives you the current Node, which is well past 20.

chameleon wraps each hook in `timeout` so a stuck process cannot stall Claude. macOS does not ship `timeout`. Install coreutils, which provides `gtimeout` (chameleon detects either):

```bash
brew install coreutils
```

Optional but recommended. Without it the hooks still run, they just lose the external wall-clock cap (chameleon's own internal timeouts still apply).

Ruby (only if you edit Rails repos):

```bash
brew install ruby
```

macOS ships an old system Ruby (2.6). The Homebrew one is 3.x and includes `prism`. After install, Homebrew prints a line to add it to your `PATH`; run that line, or open a new terminal.

### Linux (Debian / Ubuntu)

`uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

This installs to `~/.local/bin`. Open a new terminal afterward, or run `source $HOME/.local/bin/env`, so `uv` is on your `PATH`.

Node.js 20+ (the version in `apt` is usually too old, so use NodeSource):

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

Ruby (only if you edit Rails repos):

```bash
sudo apt-get install -y ruby-full
```

If `ruby --version` reports older than 3.3, also run `gem install prism` (Ruby 3.3+ already bundles it). On a different distro, use its package manager or [rbenv](https://github.com/rbenv/rbenv); any Ruby 3.0+ with the `prism` gem works.

### Windows

chameleon runs on native Windows through **Git for Windows**, which provides the `bash` its hooks use; the Python server locks and runs cross-platform. WSL2 also works and some people prefer it.

Install Git for Windows (provides Git Bash): https://git-scm.com/download/win . Run Claude Code from a shell where `bash` is on `PATH`.

`uv` (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Node.js 20+: download the LTS installer from https://nodejs.org (or `winget install OpenJS.NodeJS.LTS`).

Ruby (only if you edit Rails repos): use [RubyInstaller](https://rubyinstaller.org), pick 3.3+ so `prism` is bundled.

On Windows, chameleon serializes its profile writes with a small `.chameleon.winlock` file in the repo root (POSIX locks a directory handle instead and leaves no file). It is safe to ignore or add to `.gitignore`.

### Verify your prerequisites

Run each command. If the version prints, that tool is ready.

```bash
uv --version       # expect: uv 0.x.x
node --version     # expect: v20.x.x or higher
npm --version      # expect: 10.x.x or similar (ships with Node)
```

Only if you edit Rails repos:

```bash
ruby --version                                  # expect: ruby 3.0.0 or higher
ruby -e "require 'prism'; puts Prism::VERSION"  # expect: a version number, no error
```

If any command says "command not found", that tool is not on your `PATH`. The usual fix is to open a new terminal (installers update your `PATH` but existing shells do not pick it up). If it still fails, see [Troubleshooting](#troubleshooting).

Important: run these in the **same kind of shell you start Claude Code from**. A tool can be on your `PATH` in one terminal and missing in another.

---

## Install the plugin

Inside any Claude Code session:

```
/plugin marketplace add crisnahine/chameleon
/plugin install chameleon@chameleon
```

Restart Claude Code. That is the whole install. The plugin lands at `~/.claude/plugins/cache/chameleon/chameleon/<version>/`.

---

## Verify the plugin loaded

In a Claude Code session, ask:

> What chameleon tools do you have?

The model should list tools like `detect_repo`, `get_archetype`, `bootstrap_repo`. If it lists nothing, see [Slash commands or tools do not show up](#slash-commands-or-tools-do-not-show-up).

The very first time the server starts it builds a Python environment (about 5 to 10 seconds). That is one-time. Later starts are instant.

---

## Your first profile

Open a TypeScript or Ruby on Rails repo in Claude Code.

1. **Bootstrap a profile.** Skip this only if `.chameleon/` already exists and you trust who committed it.

   ```
   /chameleon-init
   ```

   This scans the repo, groups files into archetypes, and writes `.chameleon/`. It takes 3 to 10 seconds for repos under 5,000 files. Commit the result so your team shares it.

   If this is a TypeScript repo, the first run also installs the Node side once (about 10 seconds). You do not do anything; it just takes a moment.

2. **Trust the profile.**

   ```
   /chameleon-trust
   ```

   You type the repo's folder name to confirm. Trust is per-user and lives at `~/.local/share/chameleon/<repo_id>/.trust`. It is not committed, so every teammate trusts once on their own machine.

3. **Edit a file.** Before the edit lands, chameleon should mention which archetype the file matches and point at the canonical example. That is it working.

---

## Opt-out

Five layers, most permanent at the top:

```
.chameleon/.skip       per-repo, all users (committed to the repo)
CHAMELEON_DISABLE=1     per-user, every repo (set in your shell rc)
CHAMELEON_VERIFY=0      disable post-edit verification only
/chameleon-disable      this session only
/chameleon-pause-15m    next 15 minutes, then auto-resumes
```

Use `.chameleon/.skip` for repos chameleon should never touch (a docs-only repo, for example). Use the env var to turn it off for yourself everywhere. Use the slash commands for a quick, scoped pause.

---

## Updating

```
/plugin marketplace update chameleon
```

Restart Claude Code. The server is a long-lived process and does not pick up the new version until the session restarts. The Python and Node dependencies re-resolve on their own the next time chameleon runs.

Old versions stay in the plugin cache. To clear them after an update:

```bash
scripts/prune-plugin-cache.sh           # dry run, shows what would go
scripts/prune-plugin-cache.sh --apply   # delete every cached version except the current one
```

**Upgrading from v0.1.x (one-time):** the profile format changed and old profiles are refused. In each repo, run `/chameleon-refresh` to rebuild, then `/chameleon-trust` again (the rebuilt profile has a new signature). Full detail in [CHANGELOG.md](../CHANGELOG.md#020--2026-05-11).

---

## Uninstalling

```
/plugin uninstall chameleon
/plugin marketplace remove chameleon
```

chameleon runs a long-lived per-user daemon. Stop it (if running), then clear your local trust and drift cache:

```bash
# stop the background daemon if it's still alive (it also idles out after 10 min)
[ -f ~/.local/share/chameleon/.daemon.pid ] && kill "$(head -1 ~/.local/share/chameleon/.daemon.pid)" 2>/dev/null
rm -rf ~/.local/share/chameleon
```

`.chameleon/` folders committed in your repos are untouched. Delete them per repo if you want them gone.

---

## How dependencies resolve

You install three tools (`uv`, Node, optionally Ruby). chameleon builds everything else itself:

- **Python server.** The plugin's [`.mcp.json`](../.mcp.json) runs `uvx --refresh-package chameleon-mcp --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`. On first launch `uv` builds an isolated environment in its own cache (5 to 10 seconds). After that, instant.
- **TypeScript reader.** The first `/chameleon-init` on a TypeScript repo runs `npm install` once inside the plugin folder (about 10 seconds). If you only touch Ruby repos this never runs.

This is why the prerequisite list is short: the tools build the rest on demand.

---

## Troubleshooting

Each heading is the symptom you actually see.

### `uvx: command not found`, or "chameleon-mcp not found"

`uv` is not on your `PATH` in the shell Claude Code launched from.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal (so the `PATH` change takes effect), then restart Claude Code. On macOS, `brew install uv` is an alternative.

### "npm not found" during `/chameleon-init` on a TypeScript repo

Node is missing or not on your `PATH`. Install Node 20+ (see your OS section above), confirm `npm --version` prints in the same shell you start Claude Code from, then retry `/chameleon-init`.

### A tool installed fine but is still "command not found"

The installer updated your `PATH`, but shells that were already open do not see it. Open a new terminal, or fully quit and reopen Claude Code. On macOS, the Homebrew Ruby and a few others need a `PATH` line that `brew` prints at install time; run that line.

### `ruby` works but `require 'prism'` errors

Your Ruby is older than 3.3, which does not bundle `prism`. Install it:

```bash
gem install prism
```

If `gem install` needs sudo and you do not want system-wide gems, use a version manager like rbenv so Ruby and gems live in your home directory.

### Bootstrap fails with `failed_unsupported_language`

The repo has no TypeScript signal (`tsconfig.json` or `package.json`) and no Ruby signal (`Gemfile`). chameleon supports only those two stacks. There is nothing to fix; the repo is out of scope.

### First MCP start is slow

Expected, once. `uv` builds the Python environment (5 to 10 seconds) on first launch. The first `/chameleon-init` on a TypeScript repo also runs `npm install` once (about 10 seconds). Both are one-time per install.

### Slash commands or tools do not show up

Run `/plugin list` and confirm `chameleon` is installed and enabled.

- Missing: re-run the two install commands in [Install the plugin](#install-the-plugin).
- Listed but inactive: restart Claude Code.

### `detect_repo` still says `untrusted` after `/chameleon-trust`

Check that `~/.local/share/chameleon/<repo_id>/.trust` exists. If not, run `/chameleon-trust` again and type the repo's folder name exactly when asked.

If the state is `stale`, the committed profile changed after you trusted it. Run `/chameleon-trust` once more to re-approve the new version.

### Edits feel slow

Before each edit chameleon runs a short check (200 to 500 ms warm). If you are in a fast editing burst and do not need it, run `/chameleon-pause-15m` or `/chameleon-disable`.

### `/chameleon-doctor` warns "neither timeout(1) nor gtimeout on PATH"

chameleon wraps each hook in `timeout` to cap a stuck Python process. macOS does not ship it. Install coreutils, which provides `gtimeout`:

```bash
brew install coreutils
```

The plugin still works without it; the hooks just run without the external wall-clock cap and rely on chameleon's internal timeouts.

### Windows: hooks open in an editor, or `bash` is not recognized

chameleon's hooks need `bash`. Install Git for Windows (https://git-scm.com/download/win) and start Claude Code from a shell where `bash` runs. WSL2 also works.

---

## Related docs

- [README.md](../README.md) - what chameleon is and why
- [architecture.md](architecture.md) - how it works internally
- [CHANGELOG.md](../CHANGELOG.md) - release history
- [CONTRIBUTING.md](../.github/CONTRIBUTING.md) - working on chameleon itself

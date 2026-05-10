# Cross-Platform Polyglot Hooks for Chameleon

Chameleon's hooks need to work on Windows, macOS, and Linux. This document explains the polyglot wrapper technique that makes it possible. The pattern is adopted from `superpowers/docs/windows/polyglot-hooks.md` and uses the same `run-hook.cmd` dispatcher.

## The Problem

Claude Code runs hook commands through the system's default shell:
- **Windows**: CMD.exe
- **macOS/Linux**: bash or sh

This creates several challenges:

1. **Script execution**: Windows CMD can't execute `.sh` files directly — it tries to open them in a text editor.
2. **Path format**: Windows uses backslashes (`C:\path`), Unix uses forward slashes (`/path`).
3. **Environment variables**: `$VAR` syntax doesn't work in CMD.
4. **No `bash` in PATH**: Even with Git Bash installed, `bash` isn't in the PATH when CMD runs.

## The Solution: Polyglot `.cmd` Wrapper

A polyglot script is valid syntax in multiple languages simultaneously. Chameleon's `hooks/run-hook.cmd` is valid in both CMD and bash:

```cmd
: << 'CMDBLOCK'
@echo off
REM Windows: cmd.exe runs the batch portion, finds bash, calls the named script
if exist "C:\Program Files\Git\bin\bash.exe" (
    "C:\Program Files\Git\bin\bash.exe" "%~dp0%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %ERRORLEVEL%
)
where bash >nul 2>nul
if %ERRORLEVEL% equ 0 (
    bash "%~dp0%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %ERRORLEVEL%
)
exit /b 0
CMDBLOCK

# Unix: run the named script directly
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$1"
shift
exec bash "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
```

### How It Works

#### On Windows (CMD.exe)

1. `: << 'CMDBLOCK'` — CMD sees `:` as a label and ignores `<< 'CMDBLOCK'`.
2. `@echo off` suppresses command echoing.
3. The batch block searches for Git Bash at standard install locations, falls back to `where bash`, and silently exits if no bash is found (so the plugin still works, just without the hook).
4. `exit /b %ERRORLEVEL%` exits the batch script.
5. Everything after `CMDBLOCK` is never reached by CMD.

#### On Unix (bash/sh)

1. `: << 'CMDBLOCK'` — `:` is a no-op, `<< 'CMDBLOCK'` starts a heredoc.
2. Everything until `CMDBLOCK` is consumed by the heredoc (ignored).
3. `exec bash "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"` runs the named script.

## File Structure

```
hooks/
├── hooks.json              Points to run-hook.cmd
├── run-hook.cmd            Polyglot wrapper (cross-platform entry point)
├── session-start           Bash script: SessionStart hook
├── preflight-and-advise    Bash script: PreToolUse Edit/Write/NotebookEdit
├── posttool-recorder       Bash script: PostToolUse Bash (HMAC log)
└── callout-detector        Bash script: UserPromptSubmit (frustration hint)
```

Hook scripts use **extensionless filenames** so Claude Code's Windows auto-detection — which prepends `bash` to any command containing `.sh` — doesn't interfere.

### hooks.json

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" session-start"
          }
        ]
      }
    ]
  }
}
```

The path must be quoted because `${CLAUDE_PLUGIN_ROOT}` may contain spaces on Windows.

## Requirements

### Windows
- **Git for Windows** must be installed (provides `bash.exe`).
- Default install path: `C:\Program Files\Git\bin\bash.exe`.
- The dispatcher also checks `C:\Program Files (x86)\Git\bin\bash.exe` and `where bash`.

### Unix (macOS/Linux)
- Standard bash shell.
- `run-hook.cmd` and the hook scripts must have execute permission (`chmod +x`).

## Troubleshooting

### Hook silently doesn't run on Windows
Check that Git for Windows is installed at `C:\Program Files\Git`. The dispatcher exits 0 (silent success) when no bash is found, so the plugin continues without hook injection. This is intentional — chameleon's advisory hook is fail-open.

### Script opens in text editor instead of running
The hooks.json is pointing directly to a hook script. Always point to `run-hook.cmd <script-name>` instead.

### "bash is not recognized" inside Claude Code
The dispatcher's Git Bash detection fell through. Verify with:

```powershell
where bash
"C:\Program Files\Git\bin\bash.exe" --version
```

### Works in terminal but not as Claude Code hook
Test the hook in a simulated environment:

```powershell
$env:CLAUDE_PLUGIN_ROOT = "C:\path\to\chameleon"
cmd /c "C:\path\to\chameleon\hooks\run-hook.cmd session-start"
```

## Reference

The dispatcher pattern is adopted from [superpowers](https://github.com/obra/superpowers), which ships this same wrapper to Windows users via the official Anthropic plugin marketplace. See `superpowers/docs/windows/polyglot-hooks.md` for the upstream version.

Related Claude Code issues:
- [anthropics/claude-code#9758](https://github.com/anthropics/claude-code/issues/9758) — .sh scripts open in editor on Windows
- [anthropics/claude-code#3417](https://github.com/anthropics/claude-code/issues/3417) — Hooks don't work on Windows

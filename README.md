# cresume

## Description

`cresume` is a tiny cross-machine Codex session picker. Sync `~/.codex/sessions`
between computers and resume any user session directly without relying on
Codex's local session index.

```text
Git        → project/source files
Syncthing  → Codex session files
cresume    → session discovery and resume
```

Synchronized rollouts can exist locally before, or without, reliably appearing
in Codex's normal local picker/index. `cresume` is a practical workaround: it
reads the files directly and passes the selected UUID to `codex resume <UUID>`.
This is not a claim that every Codex version has the same picker behavior.

## Implementation details

Codex stores rollout JSONL session files under `~/.codex/sessions`.
`cresume` scans `rollout-*.jsonl` files recursively, reads session metadata and
the first user message, and offers a picker sorted by file modification time.
Set `CODEX_HOME` to use a different location; it scans `$CODEX_HOME/sessions`.
An unset or empty `CODEX_HOME` defaults to `~/.codex`.

The picker shows the last update (local time), project, branch, and a short
message preview. Child/guardian sessions and subagent sources are excluded.
Duplicate session UUIDs keep the most recently modified rollout. Missing
fields get sensible placeholders; malformed JSON lines and unreadable rollouts
are skipped.

This is one Python script using only the standard library. It works with any
mechanism that synchronizes session files; it does not integrate with Syncthing.

The parser supports `session_meta.payload` and `session_meta.payload.meta`,
including outer git/source/parent metadata. It prefers `session_id`, then `id`,
then a UUID in the filename. It recognizes `event_msg` / `user_message` and
user-role `response_item` messages with text content, and skips known injected
environment/instruction blocks. Metadata is expected near the start of the
rollout; reading stops once metadata and a real user preview are found.
The rollout format is an implementation detail, so future changes may require
small parser updates. Direct resume cannot recover a corrupt or incompatible
rollout. Archived sessions are outside v0.1's scope.

`cresume` operates entirely locally and reads session files without modifying
them. It does not transmit data, read `auth.json`, or access Codex's SQLite
databases. After selection, the normal Codex CLI takes over and may use the
network as usual.

## Setup

Requirements:

- Linux
- Python 3.8 or newer
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), available as `codex` in PATH
- Optional: `fzf` for a searchable picker
- Recommended for cross-machine use: Syncthing

Install from GitHub:

```bash
git clone https://github.com/mdel2424/cresume.git
cd cresume
./install.sh
```

The installer copies the script to `~/.local/bin/cresume` and makes it executable.
If that directory is missing from PATH, it prints this line for you to add to
your shell configuration (for example, `~/.bashrc`):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Manual installation, from the repository directory:

```bash
mkdir -p "$HOME/.local/bin"
cp cresume "$HOME/.local/bin/cresume"
chmod +x "$HOME/.local/bin/cresume"
```

No sudo, pip, or package installation is needed. The installer does not change
your shell configuration or configure Syncthing.

Configure a Syncthing folder on each computer for **only**:

```text
~/.codex/sessions
```

Use `$CODEX_HOME/sessions` instead if you have customized Codex's home directory.
Wait for synchronization to finish before resuming, and exit Codex on one
computer before continuing the same session on another.

Rollouts contain conversation content and can contain sensitive project
information. Sync them only between trusted devices.

**Do not synchronize the entire `~/.codex` directory.** Authentication and
machine-local state should remain local. Depending on your Codex version,
examples include:

```text
~/.codex/auth.json
~/.codex/state_*.sqlite
~/.codex/state_*.sqlite-wal
~/.codex/state_*.sqlite-shm
~/.codex/logs_*.sqlite
```

Not every version creates all these files. Keep authentication files local,
and do not concurrently synchronize live SQLite databases with Syncthing:
database files and their journal/sidecar files may be copied at different times,
leaving inconsistent state.

## Usage

```bash
cresume                    # all user sessions, newest first
cresume project-name       # start fzf with this search query
cresume sparse attention   # positional words form one query
cresume --cwd              # only the exact normalized current directory
cresume --help
```

With `fzf`, search the displayed fields and press Enter to select, or Esc to
cancel. Personal `FZF_DEFAULT_OPTS`/`FZF_DEFAULT_OPTS_FILE` settings are ignored
to keep selection output predictable.

Without `fzf`, the numbered menu shows the newest 40 sessions. Query words act
as case-insensitive substring filters before that limit. Enter a number to
select; an empty line, `q`, Ctrl-D, or Ctrl-C cancels. `--cwd` resolves `~`, `..`,
and symlinks; it does not match child directories or map paths between machines.

Selection replaces `cresume` with:

```bash
codex resume <SESSION_UUID>
```

Codex handles resuming and any working-directory prompt. Use matching project
paths on both machines where practical, or choose the appropriate local
directory when Codex asks.

When prompted, set Codex to switch automatically to the session's working
directory.

To continue a session on another computer, start on Computer A:

```bash
cd ~/code/project
codex
```

Exit when finished, then let Syncthing synchronize the rollout files.
Use Git separately to commit and transfer the actual codebase.

On Computer B, pull the project changes with Git, then:

```bash
cd ~/code/project
cresume
```

Select the session and continue working.

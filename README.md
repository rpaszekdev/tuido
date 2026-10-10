# TUiDO

```
 _____  _   _  _  ____   ___   
|_   _|| | | |(_)|  _ \ / _ \  
  | |  | | | || || | | || | | |
  | |  | |_| || || |_| || |_| |
  |_|   \___/ |_||____/ \___/  
```

**A terminal Pomodoro timer with an agent-shareable todo list.**

TUiDO is a single-file, dependency-free terminal app that pairs a focus timer
with a live todo list — your tasks on the left, a big ASCII tomato that fills
with `#` as the timer runs on the right. The same todo list is exposed through a
scriptable CLI, so **you or an AI agent can add and complete items while the
timer is running** — the TUI watches the file and reloads live.

```
 T U i D O                                                    running -
 TODOS  3 open · 1 done          │               WORK  ·  2 done
   1 [ ] ship the two-column UI  │                    \ | /
   2 [ ] tune the fill animation │                     \|/
   3 [x] write the AGENTS snippet│                      |
                                 │                ##~##~##~##
                                 │              ################
                                 │              ################
                                 │               ##############
                                 │                 ##########
                                 │                     ####
                                 │               14:30   41%
 space start/pause  s skip  r reset  a add  d done  x del  j/k move  c clear  q quit
 todos: ~/.local/share/tuido/todos.json
```

## Features

- **Focus timer** — 25 / 5 / 15 minutes by default, with a long break every *N*
  work sessions. Work → break → work cycles advance automatically, with a sound
  alert on each transition.
- **Todo list** — add, complete, edit and delete tasks from inside the TUI, or
  from the shell.
- **Agent-friendly** — the todo list and live timer state are plain JSON files.
  Any external process can run `tuido add "..."` and it appears in the running
  TUI instantly; `tuido status` lets an agent see what is in focus right now.
- **ASCII-art vibe** — a figlet-style wordmark, a leaf-crowned pomodoro that
  fills bottom-up with hashtags (with a shimmering fill surface), an ASCII
  bitmap clock, and a responsive two-column layout.
- **Zero dependencies** — pure Python standard library (`curses`), no pip.

## Requirements

- Python 3.8+ with the `curses` module (ships with CPython on macOS and Linux).
- macOS or Linux. On macOS the alert uses `afplay`; elsewhere it falls back to a
  terminal beep via `osascript`/no-op.

## Install

```sh
git clone https://github.com/<you>/tuido.git
cd tuido
./install.sh          # symlinks bin/tuido into ~/.local/bin
```

Make sure `~/.local/bin` is on your `PATH` (the installer tells you if it isn't).

Run it straight from the checkout without installing:

```sh
./bin/tuido --start
```

## Usage

### TUI

```sh
tuido                       # open the timer (starts immediately)
tuido --paused              # open paused
tuido -w 50 -b 10 -l 20 -c 3
tuido --sound Ping          # any name in /System/Library/Sounds
tuido --mute                # no sound at session end
```

Keys:

| key | action | key | action |
|-----|--------|-----|--------|
| `space` | start / pause | `a` | add task |
| `s` | skip phase | `d` | toggle done |
| `r` | reset phase | `x` | delete task |
| `j` / `k` | move selection | `c` | clear completed |
| `g` / `G` | jump to top / bottom | `v` | hide / show done |
| `q` / `esc` | quit | | |

### CLI

```sh
tuido add "write the report"        # add a todo (also reads stdin)
tuido add --top "urgent thing"
printf 'task a\ntask b\n' | tuido add
tuido ls [--open] [--json]
tuido done 3 4
tuido undone 3
tuido edit 3 "new text"
tuido rm 3
tuido clear                         # delete completed
tuido clear-all --yes               # wipe everything
tuido status [--json]               # live timer + counts
tuido path                          # print data file paths
tuido banner                        # print the wordmark
```

## Use with an AI agent

Point your agent at the CLI. It can add work as it goes and mark things done,
and it will show up in your running TUI immediately:

```sh
tuido add "refactor the parser"     # agent logs a task
tuido done 7                        # agent finishes it
tuido status                        # agent checks what's in focus
```

The TUI reloads `todos.json` whenever it changes, and a running TUI also writes
`state.json` (phase, remaining time, completed count) so `tuido status` reports
live state.

## Data

Everything lives in `~/.local/share/tuido/`:

| file | purpose |
|------|---------|
| `todos.json` | your todo list (source of truth) |
| `state.json` | live timer state, written while a TUI runs, removed on exit |
| `.lock` | advisory lock guarding concurrent writes |

Writes are atomic (temp file + `rename`) and guarded by `flock`, so you and an
agent can safely write at the same time.

## Architecture

- `tuido.py` — the whole application: curses UI, JSON todo store, timer state
  machine, and the CLI (one file, standard library only).
- `bin/tuido` — a launcher that finds a Python with `curses` and runs `tuido.py`.
- `install.sh` — symlinks the launcher onto your `PATH`.

## License

MIT — see [LICENSE](LICENSE).

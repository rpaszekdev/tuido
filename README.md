# TUiDO

```
 _____  _   _  _  ____   ___   
|_   _|| | | |(_)|  _ \ / _ \  
  | |  | | | || || | | || | | |
  | |  | |_| || || |_| || |_| |
  |_|   \___/ |_||____/ \___/  
```

**A terminal Pomodoro timer with an agent-shareable todo list.**

<img width="1680" height="1050" alt="image" src="https://github.com/user-attachments/assets/ac8f4d47-5f53-4a73-85c7-06ce7723b352" />


TUiDO is a single-file, dependency-free terminal app that pairs a focus timer
with a live todo list — your tasks on the left, and on the right a big ASCII
pomodoro that fills with `#` as the timer runs, with a large countdown below it.
The same todo list is exposed through a scriptable CLI, so **you or an AI agent
can add and complete items while the timer is running** — the TUI watches the
file and reloads live.

```
 T U i D O                                  running \   focus 1:30:45   2 done

 TODOS  1 open · 2 done     │                    WORK
   3 [ ] studying modules   │                      .
  [e] edit [x] del [d] done │             .....................
  [a] add                   │           ##~##~##~##~##~##~##~#
                            │    ######  ######          ######  ######
   (todo controls, left)    │    ######  ######          ######  ######
                            │             28%   0 pomodoros
                            │  [ -5m ] [ +5m ] [ pause ] [ reset ] [ timer ]
                                                                    (pomodoro, right)
                                                             ? help
```

Controls are split by side — todo actions live in the left column (with `[a] add`
in the empty space under the last task), pomodoro controls in the right. The
bottom is kept quiet; press `?` for the full key list (progressive disclosure).
On `d`, the check briefly replaces the tomato while the countdown stays put.

## Features

- **Focus timer** — 25 / 5 / 15 minutes by default, with a long break every *N*
  work sessions. Work → break → work cycles advance automatically, with a sound
  alert on each transition.
- **Todo list** — add, complete, edit and delete tasks from inside the TUI, or
  from the shell. Long text wraps; the hovered/selected task shows an inline
  `[e] edit  [x] delete  [d] done` menu.
- **Big countdown + filling pomodoro** — the right pane shows a leaf-crowned
  tomato that fills bottom-up with `#` (with a shimmering fill surface), and a
  large ASCII countdown below it that scales to your terminal.
- **Add time on the fly** — `+` / `-` (or the on-screen `[ -5m ]` / `[ +5m ]`
  buttons) extend or trim the current session by 5 minutes.
- **Focus stats** — the top-right corner shows today's focused time, all-time
  total and the number of tasks completed. Both are saved next to your tasks in
  `todos.json`, so they survive restarts (`tuido focus` / `tuido status`).
  Pressing `d` flashes a big ASCII checkmark across the visualizer for a second.
- **Daily log** — every finished work session is recorded by date (focus time,
  pomodoros, tasks done) in `sessions.json`; today's total is shown up top and
  `tuido sessions` prints the history.
- **Ideas** — press `i` anywhere in the TUI to open a scratchpad and capture a
  thought; it's appended to `ideas.md` (the overlay shows the exact path).
  `tuido idea "..."` and `tuido ideas` do the same from the shell.
- **Edit and remember your timer** — press `t` (or click `[ timer ]`) to set the
  work / short / long / cycles lengths; they are saved to `settings.json` and
  reused next launch. Also settable from the CLI: `tuido config work 50`.
- **Agent-friendly** — the todo list and live timer state are plain JSON files.
  Any external process can run `tuido add "..."` and it appears in the running
  TUI instantly; `tuido status` lets an agent see what is in focus right now.
- **Mouse + keyboard** — hover a task to focus it, click the inline menu or the
  footer buttons, or use keys for everything.
- **Zero dependencies** — pure Python standard library (`curses`), no pip.

## Requirements

- Python 3.8+ with the `curses` module (ships with CPython on macOS and Linux).
- macOS or Linux. On macOS the alert uses `afplay`; elsewhere it falls back to a
  terminal beep.

## Install

```sh
git clone https://github.com/rpaszekdev/tuido.git
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
tuido -w 50 -b 10 -l 20 -c 3   # override just for this run
tuido --sound Ping          # any name in /System/Library/Sounds
tuido --mute                # no sound at session end
```

Keys:

| key | action | key | action |
|-----|--------|-----|--------|
| `space` | start / pause | `a` | add task |
| `s` | skip phase | `d` | toggle done |
| `r` | reset phase | `e` | edit selected task |
| `+` / `-` | add / remove 5 min | `x` | delete task |
| `j` / `k` | move selection | `t` | edit timer lengths |
| `g` / `G` | jump to top / bottom | `c` | clear completed |
| `?` | help overlay | `v` | hide / show done |
| `i` | ideas scratchpad | `q` / `esc` | quit |

Long task text wraps to as many lines as it needs. Controls are grouped by side:
the selected task shows an `[e] edit  [x] delete  [d] done` menu that appears
while you navigate (`j`/`k` or arrow keys, or mouse hover) and fades away after
5 seconds. `[a] add` sits under the tasks with a blank line above it, and stays
pinned to the bottom of the column when the list is long. `[ -5m ] [ +5m ]
[ pause/start ] [ reset ] [ timer ]` sit under the pomodoro (right column), each
with its key shown beneath. All of them are clickable. Press `?` for the full
key list.

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
tuido status [--json]               # live timer + focus time + counts
tuido focus [--reset] [--json]      # cumulative focused time
tuido sessions [--json]             # daily focus log (focus time / pomodoros / tasks)
tuido idea "text"                   # save an idea (also reads stdin)
tuido ideas [--path]                # list ideas / print the ideas file path

tuido config                        # show remembered timer lengths
tuido config work 50                # set + remember (minutes)
tuido config --reset                # back to defaults

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
| `todos.json` | your todo list, plus cumulative `focus_seconds` and the done counter |
| `sessions.json` | per-day log: focus seconds, pomodoros, tasks done |
| `ideas.md` | free-form ideas/thoughts captured with `i` |
| `settings.json` | remembered timer lengths (work / short / long / cycles) |
| `state.json` | live timer state, written while a TUI runs, removed on exit |
| `.lock` | advisory lock guarding concurrent writes |

Writes are atomic (temp file + `rename`) and guarded by `flock`, so you and an
agent can safely write at the same time.

## Architecture

- `tuido.py` — the whole application: curses UI, JSON todo store, timer state
  machine, settings, and the CLI (one file, standard library only).
- `bin/tuido` — a launcher that finds a Python with `curses` and runs `tuido.py`.
- `install.sh` — symlinks the launcher onto your `PATH`.

## License

MIT — see [LICENSE](LICENSE).

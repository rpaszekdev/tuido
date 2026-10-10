# AGENTS.md

Notes for AI agents working in this repository.

## Logging work with TUiDO

This project ships a task tracker whose CLI is safe to call from an agent:
`bin/tuido` (or `tuido` once installed). Use it to make your progress visible
to the human in their running TUI, which live-reloads on every change.

```sh
./bin/tuido add "task description"   # up to ~40 chars, imperative voice
./bin/tuido ls --open                # list pending tasks
./bin/tuido done <id>                # mark finished
./bin/tuido status                   # current timer + counts
```

Guidelines:

- Add a task before starting a non-trivial unit of work; mark it `done` when it
  is finished and verified.
- Keep one item per coherent unit of work, not per file edit.
- Do not call `clear-all` without explicit user confirmation.
- The todo data lives in `~/.local/share/tuido/todos.json`; do not commit it.

## Project layout

- `tuido.py` — the entire application (curses UI + CLI + todo store).
- `bin/tuido` — launcher; resolves `../tuido.py`, picks a Python with `curses`.
- `install.sh` — symlinks the launcher into `~/.local/bin`.

## Conventions

- Standard library only — do not add third-party dependencies.
- Keep `tuido.py` runnable as a single file.
- Test changes with `python3 -m py_compile tuido.py` and by driving the TUI
  through a pty (the app requires a real terminal).

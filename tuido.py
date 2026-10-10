#!/usr/bin/env python3
"""TUiDO - a terminal Pomodoro timer with a shared, agent-friendly todo list.

  TUiDO  =  TUI + todo.  One tool, two faces over the same data:

  * a curses TUI (`tuido`) that puts your todo list on the left and, on the
    right, a big ASCII pomodoro that fills with '#' as the focus timer runs -
    with a large countdown below it
  * a scriptable CLI (`tuido add/ls/done/rm/config/...`) over the same data,
    so you - or an AI agent working in another pane - can add and complete
    items while the TUI is running. The TUI watches the file and reloads live.

Data lives as JSON under ~/.local/share/tuido/:
  todos.json     your todo list (source of truth). Also stores the cumulative
                 focused seconds and the done-tasks counter, shown top-right.
  state.json     the live timer state, written by a running TUI
  settings.json  remembered timer lengths (set in the TUI with `t`)

`tuido path` prints those paths. `tuido status` shows the timer state, focus
time and todo counts, so an agent can see what you are working on right now.

Keys in the TUI:
  space start/pause   s skip   r reset   a add   d done   x delete
  e edit the selected task   t edit timer lengths (remembered)
  + / - add or remove 5 minutes   j/k move   c clear done   v view   q/esc quit
  Long tasks wrap; hovering a task (or selecting it) shows an [e] edit /
  [x] delete / [d] done menu for it. The footer buttons are clickable too.
"""

import argparse
import contextlib
import curses
import fcntl
import json
import locale
import math
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

APP_NAME = "tuido"
VERSION = "3.2.0"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "tuido")
TODOS_FILE = os.path.join(DATA_DIR, "todos.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOCK_FILE = os.path.join(DATA_DIR, ".lock")
OLD_DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pomodoro")

SOUND_DIR = "/System/Library/Sounds"

DEFAULT_WORK = 25
DEFAULT_SHORT = 5
DEFAULT_LONG = 15
DEFAULT_CYCLES = 4
DEFAULT_SOUND = "Glass"

NUM_DEFAULTS = {"work": DEFAULT_WORK, "short": DEFAULT_SHORT,
                "long": DEFAULT_LONG, "cycles": DEFAULT_CYCLES}

PHASE_NAME = {"work": "WORK", "short": "SHORT BREAK", "long": "LONG BREAK"}
PHASE_PAIR = {"work": 1, "short": 2, "long": 3}  # curses color-pair numbers

# 3x5 bitmap font for the countdown (scaled up with scale_rows).
GLYPHS = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": ("  #", "  #", "  #", "  #", "  #"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    ":": ("   ", " # ", "   ", " # ", "   "),
    " ": ("   ", "   ", "   ", "   ", "   "),
}

# ASCII-art wordmark, six rows, uniform width (used by the CLI).
BANNER = [
    " _____  _   _  _  ____   ___   ",
    "|_   _|| | | |(_)|  _ \\ / _ \\  ",
    "  | |  | | | || || | | || | | |",
    "  | |  | |_| || || |_| || |_| |",
    "  |_|   \\___/ |_||____/ \\___/  ",
    "                               ",
]

# Pomodoro crown (leaves + stem). Body is generated to fit the panel.
TOMATO_CROWN = [" \\ | / ", "  \\|/  ", "   |   "]
TOMATO_MAX_HALF = 12
TOMATO_MAX_BODY = 20

SPINNER = "|/-\\"

TAGLINE = "a terminal pomodoro + todo"

USAGE = f"""\
{TAGLINE}

Usage:
  {APP_NAME} [timer options]            run the TUI (timer + todos)
  {APP_NAME} add "text" [--top]         add a todo (also reads stdin)
  {APP_NAME} ls [--open] [--json]       list todos
  {APP_NAME} done <id...>               mark done
  {APP_NAME} undone <id...>             mark not done
  {APP_NAME} edit <id> "text"           change a todo's text
  {APP_NAME} rm <id...>                 delete todos
  {APP_NAME} clear                      delete completed todos
  {APP_NAME} clear-all --yes            delete every todo
  {APP_NAME} status [--json]            show live timer + focus time + counts
  {APP_NAME} focus [--reset] [--json]   show or reset cumulative focused time
  {APP_NAME} config [key] [value]       show or set remembered timer lengths
  {APP_NAME} path                       print the data file paths
  {APP_NAME} banner                     print the banner

Timer options for the TUI (override remembered settings for one run):
  -w, --work MIN     work session length         (default {DEFAULT_WORK})
  -b, --break MIN    short break length          (default {DEFAULT_SHORT})
  -l, --long MIN     long break length           (default {DEFAULT_LONG})
  -c, --cycles N     work sessions per long break (default {DEFAULT_CYCLES})
  -s, --sound NAME   alert sound in {SOUND_DIR}
      --mute         no sound at session end
      --paused       open without starting the timer

Keys:
  space start/pause   s skip   r reset   a add   d done   x delete
  e edit task   t edit timer lengths (remembered)   +/- add or remove 5 minutes
  j/k move   g/G top/bottom   c clear done   v view   ? help   q/esc quit
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def to_seconds(minutes):
    """Convert minutes (int/float) to whole seconds."""
    return int(round(float(minutes) * 60))


def format_duration(seconds):
    """Human-readable focused time: H:MM:SS, or MM:SS under an hour."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def next_phase(finished_phase, completed_work, cycles):
    """Phase that follows ``finished_phase``.

    ``completed_work`` is the number of work sessions finished *including*
    this one. A long break is taken every ``cycles`` work sessions
    (``cycles <= 0`` disables long breaks).
    """
    if finished_phase == "work":
        if cycles > 0 and completed_work % cycles == 0:
            return "long"
        return "short"
    return "work"


def big_time(text):
    """Render ``text`` (e.g. '25:00') as a list of 5 strings."""
    return [" ".join(GLYPHS.get(ch, GLYPHS[" "])[line] for ch in text)
            for line in range(5)]


def scale_rows(rows, factor):
    """Expand each character and duplicate each row to grow the bitmap."""
    out = []
    for row in rows:
        wide = "".join(ch * factor for ch in row)
        out.extend([wide] * factor)
    return out


def build_tomato(body_h, max_half, crown=True):
    """Return tomato rows: an optional leaf crown plus a rounded body."""
    body_h = max(3, body_h)
    max_half = max(2, max_half)
    width = 2 * max_half + 1
    rows = [c.center(width) for c in TOMATO_CROWN] if crown else []
    centre = (body_h - 1) / 2 or 1
    for i in range(body_h):
        t = (i - centre) / centre
        half = max(0, min(max_half, round(max_half * (1 - t * t) ** 0.5)))
        rows.append(("#" * (2 * half + 1)).center(width))
    return rows


def render_tomato(rows, crown_n, frac, tick):
    """Fill the tomato body bottom-up with '#'. Returns list of strings."""
    body_n = len(rows) - crown_n
    filled = int(round(frac * body_n))
    surface = len(rows) - filled
    out = []
    for i, row in enumerate(rows):
        line = []
        for col, ch in enumerate(row):
            if ch != "#":
                line.append(ch)
            elif i >= surface:
                line.append("~" if i == surface and (col + tick) % 3 == 0 else "#")
            else:
                line.append(".")
        out.append("".join(line))
    return out


def big_check(width, height, char="#"):
    """A scalable ASCII checkmark sized to fill a width x height area."""
    width = max(2, int(width))
    height = max(2, int(height))
    grid = [[" "] * width for _ in range(height)]
    thick = max(2, round(height / 9))

    def stamp(x, y):
        cy = int(round(y)) - thick // 2
        cx = int(round(x)) - thick // 2
        for dy in range(thick):
            for dx in range(thick):
                yy, xx = cy + dy, cx + dx
                if 0 <= yy < height and 0 <= xx < width:
                    grid[yy][xx] = char

    def seg(x0, y0, x1, y1):
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(steps + 1):
            t = i / steps
            stamp(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    start = (0.12 * width, 0.45 * height)
    elbow = (0.37 * width, 0.86 * height)
    tip = (0.90 * width, 0.14 * height)
    seg(*start, *elbow)
    seg(*elbow, *tip)
    return ["".join(row) for row in grid]


def banner_text():    return "\n".join(BANNER).rstrip("\n")


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked():
    os.makedirs(DATA_DIR, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def migrate_old_data():
    """One-time copy of todos from the old `pomodoro` data dir, if present."""
    old = os.path.join(OLD_DATA_DIR, "todos.json")
    with contextlib.suppress(OSError):
        if not os.path.exists(TODOS_FILE) and os.path.exists(old):
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.copyfile(old, TODOS_FILE)


# --------------------------------------------------------------------------- #
# settings (remembered timer lengths)
# --------------------------------------------------------------------------- #
def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data):
    with contextlib.suppress(OSError):
        _atomic_write(SETTINGS_FILE, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def effective_settings():
    saved = load_settings()
    out = dict(NUM_DEFAULTS)
    for key in NUM_DEFAULTS:
        if key in saved:
            out[key] = saved[key]
    return out


# --------------------------------------------------------------------------- #
# todo store
# --------------------------------------------------------------------------- #
def _normalize(data):
    if not isinstance(data, dict):
        data = {}
    next_id = data.get("next_id")
    try:
        next_id = int(next_id)
    except (TypeError, ValueError):
        next_id = 1
    raw = data.get("todos")
    items = raw if isinstance(raw, list) else []
    todos = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            tid = int(it.get("id"))
        except (TypeError, ValueError):
            tid = next_id
            next_id += 1
        todos.append({
            "id": tid,
            "text": str(it.get("text", "")).strip(),
            "done": bool(it.get("done", False)),
            "created": it.get("created"),
            "done_at": it.get("done_at"),
        })
        if tid >= next_id:
            next_id = tid + 1
    data["next_id"] = next_id
    data["todos"] = todos
    try:
        data["focus_seconds"] = int(data.get("focus_seconds", 0) or 0)
    except (TypeError, ValueError):
        data["focus_seconds"] = 0
    return data


def load_todos():
    try:
        with open(TODOS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    return _normalize(data)


def save_todos(data):
    _atomic_write(TODOS_FILE, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def store_add(texts, top=False):
    ids = []
    with _locked():
        data = load_todos()
        for text in texts:
            text = text.strip()
            if not text:
                continue
            tid = data["next_id"]
            data["next_id"] = tid + 1
            item = {"id": tid, "text": text, "done": False,
                    "created": time.time(), "done_at": None}
            if top:
                data["todos"].insert(0, item)
            else:
                data["todos"].append(item)
            ids.append(tid)
        if ids:
            save_todos(data)
    return ids


def store_set_done(ids, done=True):
    changed = []
    want = set(ids)
    with _locked():
        data = load_todos()
        for it in data["todos"]:
            if it["id"] in want:
                it["done"] = done
                it["done_at"] = time.time() if done else None
                changed.append(it["id"])
        if changed:
            save_todos(data)
    return changed


def store_edit(tid, text):
    text = text.strip()
    with _locked():
        data = load_todos()
        for it in data["todos"]:
            if it["id"] == tid:
                it["text"] = text
                save_todos(data)
                return True
    return False


def store_remove(ids):
    want = set(ids)
    with _locked():
        data = load_todos()
        before = len(data["todos"])
        data["todos"] = [it for it in data["todos"] if it["id"] not in want]
        removed = before - len(data["todos"])
        if removed:
            save_todos(data)
    return removed


def store_clear_done():
    with _locked():
        data = load_todos()
        before = len(data["todos"])
        data["todos"] = [it for it in data["todos"] if not it["done"]]
        removed = before - len(data["todos"])
        if removed:
            save_todos(data)
    return removed


def store_clear_all():
    with _locked():
        data = load_todos()
        n = len(data["todos"])
        data["todos"] = []
        if n:
            save_todos(data)
    return n


def get_focus_seconds():
    """Cumulative focused seconds, stored alongside the tasks."""
    return int(load_todos().get("focus_seconds", 0) or 0)


def add_focus_seconds(seconds):
    """Add to the cumulative focused time and persist it with the tasks."""
    with _locked():
        data = load_todos()
        data["focus_seconds"] = int(data.get("focus_seconds", 0) or 0) + int(seconds)
        save_todos(data)
        return data["focus_seconds"]


# --------------------------------------------------------------------------- #
# live timer state (so an agent can `tuido status`)
# --------------------------------------------------------------------------- #
def write_state(state):
    with contextlib.suppress(OSError):
        _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False) + "\n")


def read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def clear_state():
    with contextlib.suppress(OSError):
        os.unlink(STATE_FILE)


# --------------------------------------------------------------------------- #
# curses TUI
# --------------------------------------------------------------------------- #
class Tui:
    def __init__(self, scr, cfg):
        self.scr = scr
        self.cfg = cfg
        self.durations = {"work": cfg["work"], "short": cfg["short"], "long": cfg["long"]}
        self.phase = "work"
        self.completed = 0
        self.running = False
        self.deadline = None
        self.remaining = float(self.durations["work"])
        self.total = float(self.durations["work"])
        self.todos = []
        self.sel = 0
        self.show_all = True
        self.alive = True
        self._file_sig = None
        self._last_state = 0.0
        self._message = ""
        self._message_until = 0.0
        self._color = False
        self._buttons = []
        self._hover_rows = {}
        self.editing_add = False
        self.input_text = ""
        self.show_help = False
        self._menu_until = 0.0
        self.focus_total = get_focus_seconds()
        self._tick_until = 0.0
        self._init_colors()
        self._reload(force=True)
        if cfg["autostart"]:
            self.start()

    # ---------------------------------------------------------------- setup
    def _init_colors(self):
        try:
            curses.start_color()
            with contextlib.suppress(curses.error):
                curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_CYAN, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            self._color = True
        except curses.error:
            self._color = False

    def _cp(self, pair):
        return curses.color_pair(pair) if self._color else 0

    def _accent_pair(self):
        return PHASE_PAIR.get(self.phase, 4)

    def run(self):
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        self.scr.keypad(True)
        self.scr.timeout(80)
        with contextlib.suppress(curses.error):
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        try:
            while self.alive:
                self._handle_keys()
                self._tick_timer()
                self._maybe_reload()
                self._draw()
                self.scr.refresh()
        except KeyboardInterrupt:
            pass
        finally:
            clear_state()

    # ------------------------------------------------------------- drawing
    def _add(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or not text:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        maxw = w - x
        if y == h - 1:
            maxw = min(maxw, w - 1 - x)
        if maxw <= 0:
            return
        with contextlib.suppress(curses.error):
            self.scr.addstr(y, x, text[:maxw], attr)

    def _put(self, y, x, text, width, attr=0):
        self._add(y, x, text[:width], attr)

    def _center_at(self, y, cx, text, attr=0):
        self._add(y, max(0, cx - len(text) // 2), text, attr)

    def _center_line(self, y, w, text, attr=0):
        self._add(y, max(0, (w - len(text)) // 2), text, attr)

    def _control(self, y, x, label, action, attr=0):
        """Draw a clickable control and register it for mouse hits."""
        self._add(y, x, label, attr)
        self._buttons.append((x, x + len(label) - 1, y, action))

    def _draw(self):
        with contextlib.suppress(curses.error):
            self.scr.erase()
        self._buttons = []
        self._hover_rows = {}
        h, w = self.scr.getmaxyx()
        if h < 8 or w < 34:
            self._add(0, 0, "terminal too small", curses.A_BOLD)
            return

        acc = self._cp(self._accent_pair())
        tick = int(time.monotonic() * 8)
        secs = max(0, int(math.ceil(self.remaining - 1e-9)))
        m, s = divmod(secs, 60)
        frac = 1.0 - (self.remaining / self.total) if self.total > 0 else 0.0
        frac = max(0.0, min(1.0, frac))

        self._add(0, 1, "T U i D O", acc | curses.A_BOLD)
        spin = SPINNER[tick % len(SPINNER)] if self.running else "*"
        focus = self.focus_total
        if self.phase == "work":
            focus += max(0.0, self.total - self.remaining)
        done_n = sum(1 for t in self.todos if t["done"])
        status = (f"{'running' if self.running else 'paused'} {spin}   "
                  f"focus {format_duration(focus)}   {done_n} done ")
        self._add(0, max(w - len(status) - 1, 1), status, curses.A_DIM)

        top = 2
        bottom = h - 4
        two_col = w >= 56 and (bottom - top + 1) >= 12
        if two_col:
            self._draw_wide(top, bottom, w, acc, frac, m, s, tick)
        else:
            self._draw_compact(top, bottom, w, acc, frac, m, s)

        self._draw_footer(h, w)
        if self.show_help:
            self._draw_help()

    def _draw_footer(self, h, w):
        if self.editing_add:
            self._add(h - 1, 0, "new task  \u00b7  Enter add  \u00b7  Esc cancel",
                      self._cp(4) | curses.A_BOLD)
        elif self._message and time.monotonic() < self._message_until:
            self._add(h - 1, 0, self._message, self._cp(4) | curses.A_BOLD)
        else:
            hint = "? help"
            self._add(h - 1, max(w - len(hint) - 1, 0), hint, curses.A_DIM)

    def _draw_pomodoro_controls(self, rx, rw, y, acc):
        label = " pause " if self.running else " start "
        spec = [
            ("[ -5m ]", "-", lambda: self._add_minutes(-5)),
            ("[ +5m ]", "+", lambda: self._add_minutes(5)),
            (f"[{label}]", "space", self._toggle),
            ("[ reset ]", "r", self._reset),
            ("[ timer ]", "t", self._edit_flow),
        ]
        row = " ".join(t for t, _k, _a in spec)
        x = rx + max(0, (rw - len(row)) // 2)
        for text, key, action in spec:
            if x + len(text) > rx + rw - 1:
                break
            self._add(y, x, text, acc | curses.A_BOLD)
            self._buttons.append((x, x + len(text) - 1, y, action))
            hx = x + max(0, (len(text) - len(key)) // 2)
            self._add(y + 1, hx, key, curses.A_DIM)
            x += len(text) + 1
        return 2

    def _draw_help(self):
        h, w = self.scr.getmaxyx()
        rows = [
            ("TUiDO - keyboard", curses.A_BOLD),
            ("", 0),
            ("space    start / pause", 0),
            ("s        skip phase", 0),
            ("r        reset phase", 0),
            ("+ / -    add / remove 5 minutes", 0),
            ("t        edit timer lengths", 0),
            ("", 0),
            ("a        add task", 0),
            ("e        edit selected task", 0),
            ("d        toggle done", 0),
            ("x        delete selected task", 0),
            ("j / k    move selection", 0),
            ("g / G    top / bottom", 0),
            ("c        clear completed", 0),
            ("v        hide / show done", 0),
            ("", 0),
            ("q / esc  quit", 0),
            ("?        close help", 0),
        ]
        inner = max(len(t) for t, _ in rows)
        box_w = inner + 4
        x0 = max(1, (w - box_w) // 2)
        y0 = max(1, (h - (len(rows) + 2)) // 2)
        for i in range(len(rows) + 2):
            self._add(y0 + i, x0, " " * box_w, 0)
        border = "+" + "-" * (box_w - 2) + "+"
        self._add(y0, x0, border, curses.A_DIM)
        for i, (text, attr) in enumerate(rows):
            y = y0 + 1 + i
            self._add(y, x0, "|", curses.A_DIM)
            self._add(y, x0 + 2, text.ljust(inner), attr)
            self._add(y, x0 + box_w - 2, "|", curses.A_DIM)
        self._add(y0 + len(rows) + 1, x0, border, curses.A_DIM)

    # ---------------------------------------------------------- two-column
    def _draw_wide(self, top, bottom, w, acc, frac, m, s, tick):
        left_w = max(24, w // 2)
        self._draw_tasks(1, left_w - 1, top, bottom)
        for yy in range(top, bottom + 1):
            self._add(yy, left_w, "\u2502", curses.A_DIM)

        rx = left_w + 1
        rw = w - rx
        if rw < 16:
            return
        region_h = bottom - top + 1
        cx = rx + rw // 2
        self._draw_pomodoro_controls(rx, rw, bottom + 1, acc)

        small = big_time(f"{m:02d}:{s:02d}")
        max_half = min(TOMATO_MAX_HALF, (rw - 2) // 2)

        # Pick a countdown scale that leaves room for the tomato. Kept modest
        # (max 2x) so the number stays a subtle readout, not a billboard.
        chosen = None
        for factor in (2, 1):
            num = scale_rows(small, factor)
            num_h, num_w = len(num), len(num[0])
            if num_w > rw - 2:
                continue
            tomato_h = region_h - (1 + 1 + num_h + 1)   # phase, gap, number, pct
            if tomato_h >= 5:
                chosen = (num, num_h, tomato_h)
                break
        if chosen is None:
            self._draw_big_clock(rx, top, rw, region_h, acc, m, s, frac)
            return

        num, num_h, tomato_h = chosen
        crow = len(TOMATO_CROWN) if (max_half >= 5 and tomato_h - len(TOMATO_CROWN) >= 5) else 0
        body_h = max(3, min(TOMATO_MAX_BODY, tomato_h - crow))
        tomato = render_tomato(build_tomato(body_h, max_half, bool(crow)), crow, frac, tick)

        # On "done", the check replaces the tomato (the visualizer) only - the
        # countdown timer stays put below it.
        if time.monotonic() < self._tick_until:
            tomato = big_check(2 * max_half + 1, len(tomato))
            tomato_attr = acc | curses.A_BOLD
        else:
            tomato_attr = acc

        block_h = 1 + len(tomato) + 1 + num_h + 1
        oy = top + max(0, (region_h - block_h) // 2)

        self._center_at(oy, cx, PHASE_NAME[self.phase], acc | curses.A_BOLD)
        oy += 1
        for line in tomato:
            self._center_at(oy, cx, line, tomato_attr)
            oy += 1
        oy += 1
        for line in num:
            self._center_at(oy, cx, line, acc)
            oy += 1
        self._center_at(oy, cx, f"{int(frac * 100):>3}%   {self.completed} pomodoros",
                        curses.A_DIM)

    def _draw_tasks(self, x, width, top, bottom):
        open_n = sum(1 for t in self.todos if not t["done"])
        done_n = len(self.todos) - open_n
        self._put(top, x, f"TODOS  {open_n} open \u00b7 {done_n} done",
                  width, curses.A_BOLD)

        y0 = top + 1
        # Inline "add" field lives at the top of the list, where the new task
        # will actually appear - not at the bottom of the screen.
        if self.editing_add:
            prefix = "   [ ] "
            field = (prefix + self.input_text)[:width - 1]
            self._put(y0, x, field, width, self._cp(4) | curses.A_BOLD)
            cur_x = x + len(prefix) + min(len(self.input_text),
                                          max(0, width - 1 - len(prefix)))
            self._put(y0, cur_x, "\u2588", 1, self._cp(4) | curses.A_BOLD)
            y0 += 1

        avail = bottom - y0 + 1
        if avail <= 0:
            return
        vis = self._visible()
        if not vis:
            if not self.editing_add:
                self._put(y0, x + 2, "no tasks yet", width - 2, curses.A_DIM)
                self._control(y0 + 2, x + 2, "[a] add", self._begin_add, self._cp(4))
            return

        # Wrap each task to the panel width; the selected task also gets a
        # one-row action menu ("signifiers") right below it.
        prefix_w = 8                       # "  3 [x] "
        text_w = max(8, width - prefix_w - 1)
        lines = []                         # (task_pos, text, kind, first?)
        for pos, idx in enumerate(vis):
            t = self.todos[idx]
            box = "[x]" if t["done"] else "[ ]"
            body = t["text"] or "(untitled)"
            wrapped = textwrap.wrap(body, width=text_w) or [""]
            for j, seg in enumerate(wrapped):
                text = (f"{t['id']:>3} {box} {seg}" if j == 0
                        else " " * prefix_w + seg)
                lines.append((pos, text, "task", j == 0))
        # The action menu only shows while you're navigating (arrows / j-k /
        # mouse hover), then it fades away after 5 seconds.
        last_sel = max((i for i, ln in enumerate(lines) if ln[0] == self.sel),
                       default=-1)
        if last_sel >= 0 and time.monotonic() < self._menu_until:
            lines.insert(last_sel + 1, (self.sel, "", "action", False))

        region_h = bottom - y0 + 1
        # Reserve the bottom rows for the [a] add control (with a blank line
        # above it) whenever the tasks don't leave room right below them - so
        # add never disappears, however many tasks there are.
        reserve = 2 if (len(lines) + 2 > region_h) else 0
        if region_h - reserve < 1:
            reserve = max(0, region_h - 1)
        avail = max(1, region_h - reserve)

        # Scroll so the selected task's whole block stays on screen.
        sel_first = max((i for i, ln in enumerate(lines) if ln[0] == self.sel),
                        default=0)
        sel_last = sel_first
        for i in range(sel_first, len(lines)):
            if lines[i][0] != self.sel:
                break
            sel_last = i
        scroll = 0
        if sel_last >= avail:
            scroll = sel_last - avail + 1
        if sel_first < scroll:
            scroll = sel_first
        scroll = max(0, min(scroll, max(0, len(lines) - avail)))
        shown = lines[scroll:scroll + avail]

        for row, (pos, text, kind, first) in enumerate(shown):
            y = y0 + row
            if kind == "action":
                menu = "[e] edit   [x] delete   [d] done"
                self._put(y, x + 2, menu, width - 2, self._cp(4))
                base = x + 2
                for key, off, act in (
                    ("e", 0, lambda p=pos: self._edit_task_flow(p)),
                    ("x", 11, lambda p=pos: self._delete_at(p)),
                    ("d", 24, lambda p=pos: self._toggle_at(p)),
                ):
                    self._buttons.append((base + off, base + off + 2, y, act))
                continue
            t = self.todos[vis[pos]]
            attr = curses.A_DIM if t["done"] else 0
            if pos == self.sel:
                attr |= curses.A_REVERSE
            self._put(y, x, text, width, attr)
            self._hover_rows[y] = pos

        # [a] add: one blank line below the last task, pinned to the bottom of
        # the column when the list is long so it stays reachable.
        if not self.editing_add:
            add_y = bottom if reserve else y0 + len(shown) + 1
            if add_y <= bottom:
                self._control(add_y, x + 2, "[a] add", self._begin_add, self._cp(4))

    def _toggle_at(self, pos):
        vis = self._visible()
        if 0 <= pos < len(vis):
            self.sel = pos
            self._toggle_done()

    def _delete_at(self, pos):
        vis = self._visible()
        if 0 <= pos < len(vis):
            self.sel = pos
            self._delete_sel()

    def _edit_task_flow(self, pos):
        vis = self._visible()
        if not (0 <= pos < len(vis)):
            self._message_set("no task selected")
            return
        self.sel = pos
        t = self.todos[vis[pos]]
        tid = t["id"]
        text = self._prompt("edit task: ", initial=t["text"])
        if text and text.strip():
            store_edit(tid, text)
            self._reload(force=True)
            self._message_set(f"edited #{tid}")

    def _draw_big_clock(self, rx, top, rw, region_h, acc, m, s, frac):
        cx = rx + rw // 2
        small = big_time(f"{m:02d}:{s:02d}")
        factor = 2 if len(small[0]) * 2 <= rw - 2 else 1
        num = scale_rows(small, factor)
        block = 1 + len(num) + 1
        y = top + max(0, (region_h - block) // 2)
        self._center_at(y, cx, PHASE_NAME[self.phase], acc | curses.A_BOLD)
        y += 1
        if time.monotonic() < self._tick_until:
            self._center_at(y + len(num) // 2, cx, "\u2713", acc | curses.A_BOLD)
            y += len(num)
        else:
            for line in num:
                self._center_at(y, cx, line, acc)
                y += 1
        self._center_at(y, cx, f"{int(frac * 100):>3}%", curses.A_DIM)

    # ------------------------------------------------------------ compact
    def _draw_compact(self, top, bottom, w, acc, frac, m, s):
        small = big_time(f"{m:02d}:{s:02d}")
        factor = 2 if len(small[0]) * 2 <= w - 2 else 1
        num = scale_rows(small, factor)
        y = top
        self._center_line(y, w, PHASE_NAME[self.phase], acc | curses.A_BOLD)
        y += 1
        if (bottom - y) >= len(num) + 3:
            for line in num:
                self._center_line(y, w, line, acc | curses.A_BOLD)
                y += 1
        else:
            self._center_line(y, w, f"{m:02d}:{s:02d}", acc | curses.A_BOLD)
            y += 1
        bar_w = min(w - 8, 44)
        filled = int(round(bar_w * frac))
        self._center_line(y, w, "\u2588" * filled + "\u2591" * (bar_w - filled), acc)
        y += 1
        y += self._draw_pomodoro_controls(2, w - 4, y, acc) + 1
        self._draw_tasks(2, w - 4, y, bottom)

    # --------------------------------------------------------------- todos
    def _visible(self):
        if self.show_all:
            return list(range(len(self.todos)))
        return [i for i, t in enumerate(self.todos) if not t["done"]]

    def _clamp_sel(self):
        n = len(self._visible())
        self.sel = 0 if n == 0 else max(0, min(self.sel, n - 1))

    def _selected_id(self):
        vis = self._visible()
        self._clamp_sel()
        return None if not vis else self.todos[vis[self.sel]]["id"]

    def _stat_sig(self):
        try:
            st = os.stat(TODOS_FILE)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload(self, force=False):
        sig = self._stat_sig()
        if not force and sig == self._file_sig:
            return
        self._file_sig = sig
        sel_id = self._selected_id()
        self.todos = load_todos()["todos"]
        if sel_id is not None:
            for pos, idx in enumerate(self._visible()):
                if self.todos[idx]["id"] == sel_id:
                    self.sel = pos
                    break
        self._clamp_sel()

    def _maybe_reload(self):
        self._reload(force=False)

    def _message_set(self, text, seconds=3.0):
        self._message = text
        self._message_until = time.monotonic() + seconds

    # -------------------------------------------------------------- actions
    def _toggle_done(self):
        tid = self._selected_id()
        if tid is None:
            return
        cur = next((t for t in self.todos if t["id"] == tid), None)
        if cur is None:
            return
        new = not cur["done"]
        if new:
            self._tick_until = time.monotonic() + 1.0   # flash a tick for 1s
        store_set_done([tid], new)
        self._reload(force=True)
        self._message_set(f"#{tid} " + ("done" if new else "reopened"))

    def _delete_sel(self):
        tid = self._selected_id()
        if tid is None:
            return
        store_remove([tid])
        self._reload(force=True)
        self._message_set(f"deleted #{tid}")

    def _clear_done(self):
        n = store_clear_done()
        self._reload(force=True)
        self._message_set(f"cleared {n} completed")

    def _move(self, delta):
        self.sel += delta
        self._clamp_sel()
        self._menu_until = time.monotonic() + 5.0

    def _begin_add(self):
        self.editing_add = True
        self.input_text = ""

    def _handle_add_key(self, ch):
        if ch == 27:                                  # Esc cancels
            self.editing_add = False
            self.input_text = ""
        elif ch in (10, 13, curses.KEY_ENTER):        # Enter commits
            text = self.input_text.strip()
            self.editing_add = False
            self.input_text = ""
            if text:
                ids = store_add([text], top=True)     # appears where it was typed
                self._reload(force=True)
                if ids:
                    self.sel = 0
                    self._message_set(f"added #{ids[0]}")
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.input_text = self.input_text[:-1]
        elif 32 <= ch < 127:
            self.input_text += chr(ch)

    def _prompt(self, label, initial=""):
        h, w = self.scr.getmaxyx()
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        self.scr.nodelay(False)
        self.scr.timeout(-1)
        text = initial
        row = h - 1
        try:
            while True:
                self._add(row, 0, " " * (w - 1))
                self._add(row, 0, (label + text)[: w - 1],
                          self._cp(4) | curses.A_BOLD)
                self.scr.refresh()
                ch = self.scr.getch()
                if ch in (10, 13, curses.KEY_ENTER):
                    break
                if ch == 27:
                    text = ""
                    break
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    text = text[:-1]
                elif 32 <= ch < 127:
                    text += chr(ch)
        finally:
            self.scr.nodelay(True)
            self.scr.timeout(80)
            with contextlib.suppress(curses.error):
                curses.curs_set(0)
        return text

    # ------------------------------------------------ edit timer (remembered)
    def _edit_flow(self):
        fields = [("work", "Work"), ("short", "Short break"),
                  ("long", "Long break"), ("cycles", "Cycles")]
        draft = {
            "work": max(1, round(self.durations["work"] / 60)),
            "short": max(1, round(self.durations["short"] / 60)),
            "long": max(1, round(self.durations["long"] / 60)),
            "cycles": int(self.cfg["cycles"]),
        }
        idx = 0
        self.scr.nodelay(False)
        self.scr.timeout(-1)
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        try:
            while True:
                self._draw_edit(fields, draft, idx)
                ch = self.scr.getch()
                if ch == curses.KEY_MOUSE:
                    continue
                if ch in (27, ord("q")):
                    return
                if ch in (10, 13, curses.KEY_ENTER):
                    self._apply_edit(draft)
                    return
                if ch in (ord("j"), curses.KEY_DOWN):
                    idx = (idx + 1) % len(fields)
                elif ch in (ord("k"), curses.KEY_UP):
                    idx = (idx - 1) % len(fields)
                elif ch in (ord("+"), ord("="), ord("l"), curses.KEY_RIGHT):
                    self._bump(draft, fields[idx][0], 1)
                elif ch in (ord("-"), ord("h"), curses.KEY_LEFT):
                    self._bump(draft, fields[idx][0], -1)
        finally:
            self.scr.nodelay(True)
            self.scr.timeout(80)

    @staticmethod
    def _bump(draft, key, delta):
        if key == "cycles":
            draft[key] = max(0, min(12, draft[key] + delta))
        else:
            draft[key] = max(1, min(180, draft[key] + delta))

    def _draw_edit(self, fields, draft, idx):
        with contextlib.suppress(curses.error):
            self.scr.erase()
        h, w = self.scr.getmaxyx()
        acc = self._cp(self._accent_pair())
        head = "Edit timer"
        tip = "j/k move   -/+ adjust   Enter save   Esc cancel"
        lines = []
        for i, (key, label) in enumerate(fields):
            value = f"{draft[key]} min" if key != "cycles" else f"{draft[key]}"
            text = f"  {label:<12}{value:>7}  "
            attr = curses.A_REVERSE if i == idx else 0
            lines.append((text, attr))
        inner = max(len(head), len(tip), *(len(t) for t, _ in lines))
        box_w = inner + 4
        x0 = max(1, (w - box_w) // 2)
        y0 = max(1, (h - (len(lines) + 4)) // 2)
        border = "+" + "-" * (box_w - 2) + "+"
        self._add(y0, x0, border, curses.A_DIM)
        self._add(y0 + 1, x0, "|", curses.A_DIM)
        self._add(y0 + 1, x0 + 2, head.ljust(inner), acc | curses.A_BOLD)
        self._add(y0 + 1, x0 + box_w - 2, "|", curses.A_DIM)
        for i, (text, attr) in enumerate(lines):
            row = y0 + 2 + i
            self._add(row, x0, "|", curses.A_DIM)
            self._add(row, x0 + 2, text.ljust(inner), attr)
            self._add(row, x0 + box_w - 2, "|", curses.A_DIM)
        tip_row = y0 + 2 + len(lines)
        self._add(tip_row, x0, "|", curses.A_DIM)
        self._add(tip_row, x0 + 2, tip.ljust(inner), curses.A_DIM)
        self._add(tip_row, x0 + box_w - 2, "|", curses.A_DIM)
        self._add(y0 + len(lines) + 3, x0, border, curses.A_DIM)
        self.scr.refresh()

    def _apply_edit(self, draft):
        self.cfg["cycles"] = int(draft["cycles"])
        for key in ("work", "short", "long"):
            self.durations[key] = to_seconds(draft[key])
        if self.phase in ("work", "short", "long"):
            new = float(self.durations[self.phase])
            self.total = new
            self.remaining = new
            if self.running:
                self.deadline = time.monotonic() + self.remaining
        save_settings({
            "work": int(draft["work"]), "short": int(draft["short"]),
            "long": int(draft["long"]), "cycles": int(draft["cycles"]),
        })
        self._message_set("timer updated (remembered)")

    # ---------------------------------------------------------------- keys
    def _handle_keys(self):
        while True:
            try:
                ch = self.scr.getch()
            except curses.error:
                return
            if ch == -1 or ch == curses.KEY_RESIZE:
                return
            if self.editing_add:
                self._handle_add_key(ch)
                return
            if self.show_help:
                self.show_help = False
                return
            if ch == curses.KEY_MOUSE:
                self._handle_mouse()
                continue
            if ch in (ord("q"), 27):
                self.alive = False
                return
            if ch == ord("?"):
                self.show_help = True
            elif ch == ord(" "):
                self._toggle()
            elif ch == ord("s"):
                self._skip()
            elif ch == ord("r"):
                self._reset()
            elif ch in (ord("+"), ord("=")):
                self._add_minutes(5)
            elif ch == ord("-"):
                self._add_minutes(-5)
            elif ch == ord("e"):
                self._edit_task_flow(self.sel)
            elif ch == ord("t"):
                self._edit_flow()
            elif ch == ord("a"):
                self._begin_add()
            elif ch == ord("d"):
                self._toggle_done()
            elif ch in (ord("x"), curses.KEY_DC, curses.KEY_BACKSPACE, 127):
                self._delete_sel()
            elif ch in (ord("j"), curses.KEY_DOWN):
                self._move(1)
            elif ch in (ord("k"), curses.KEY_UP):
                self._move(-1)
            elif ch == ord("g"):
                self.sel = 0
                self._menu_until = time.monotonic() + 5.0
            elif ch == ord("G"):
                self.sel = max(0, len(self._visible()) - 1)
                self._menu_until = time.monotonic() + 5.0
            elif ch == ord("c"):
                self._clear_done()
            elif ch == ord("v"):
                self.show_all = not self.show_all
                self._clamp_sel()

    def _handle_mouse(self):
        try:
            _id, x, y, _z, bstate = curses.getmouse()
        except curses.error:
            return
        clicked = bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
                            | curses.BUTTON1_RELEASED)
        if clicked:
            for x0, x1, by, action in self._buttons:
                if by == y and x0 <= x <= x1:
                    action()
                    return
        pos = self._hover_rows.get(y)
        if pos is not None:
            self.sel = pos
            self._clamp_sel()
            self._menu_until = time.monotonic() + 5.0

    # --------------------------------------------------------------- timer
    def start(self):
        if self.running:
            return
        self.running = True
        self.deadline = time.monotonic() + self.remaining

    def pause(self):
        if not self.running:
            return
        self.remaining = max(0.0, self.deadline - time.monotonic())
        self.running = False

    def _toggle(self):
        self.pause() if self.running else self.start()

    def _reset(self):
        self.running = False
        self.deadline = None
        self.total = float(self.durations[self.phase])
        self.remaining = self.total
        self._message_set("reset")

    def _add_minutes(self, delta):
        self.total = max(60.0, self.total + delta * 60)
        self.remaining = max(1.0, self.remaining + delta * 60)
        if self.running:
            self.deadline = time.monotonic() + self.remaining
        self._message_set(f"{delta:+d} min")

    def _skip(self):
        self.running = False
        self.deadline = None
        self.phase = next_phase(self.phase, self.completed, self.cfg["cycles"])
        self.total = float(self.durations[self.phase])
        self.remaining = self.total
        self._message_set(f"skipped -> {PHASE_NAME[self.phase]}")
        self.start()

    def _complete(self):
        finished = self.phase
        self.running = False
        self.deadline = None
        if finished == "work":
            self.completed += 1
            self.focus_total = add_focus_seconds(int(self.total))
        self.phase = next_phase(finished, self.completed, self.cfg["cycles"])
        self.total = float(self.durations[self.phase])
        self.remaining = self.total
        self._play_sound()
        self._message_set(f"{PHASE_NAME[finished]} done -> {PHASE_NAME[self.phase]}")
        self.start()

    def _tick_timer(self):
        if self.running:
            self.remaining = self.deadline - time.monotonic()
            if self.remaining <= 0:
                self.remaining = 0.0
                self._complete()
        now = time.monotonic()
        if now - self._last_state >= 0.9:
            self._last_state = now
            write_state({
                "phase": self.phase,
                "running": self.running,
                "remaining": max(0, int(round(self.remaining))),
                "total": int(self.total),
                "completed_work": self.completed,
                "focus_seconds": int(self.focus_total + (self.total - self.remaining
                                                         if self.phase == "work" else 0)),
                "todos_open": sum(1 for t in self.todos if not t["done"]),
                "todos_done": sum(1 for t in self.todos if t["done"]),
                "updated": time.time(),
                "pid": os.getpid(),
            })

    def _play_sound(self):
        if self.cfg["mute"]:
            return
        path = os.path.join(SOUND_DIR, f"{self.cfg['sound']}.aiff")
        try:
            if os.path.exists(path):
                subprocess.Popen(["afplay", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["osascript", "-e", "beep"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def run_tui(args):
    saved = effective_settings()

    def pick(flag_value, key):
        return flag_value if flag_value is not None else saved.get(key, NUM_DEFAULTS[key])

    cfg = {
        "work": to_seconds(pick(args.work, "work")),
        "short": to_seconds(pick(args.short, "short")),
        "long": to_seconds(pick(args.long_break, "long")),
        "cycles": int(pick(args.cycles, "cycles")),
        "sound": args.sound if args.sound is not None else DEFAULT_SOUND,
        "mute": args.mute,
        "autostart": not args.paused,
    }
    with contextlib.suppress(Exception):
        locale.setlocale(locale.LC_ALL, "")
    # Make the Esc key responsive: ncurses otherwise waits ~1s to decide whether
    # a lone ESC begins a key sequence, which feels like Esc "doesn't work".
    os.environ.setdefault("ESCDELAY", "25")

    def _start(scr):
        with contextlib.suppress(Exception):
            curses.set_escdelay(25)
        with contextlib.suppress(curses.error):
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        Tui(scr, cfg).run()

    try:
        curses.wrapper(_start)
    except curses.error as exc:
        print(f"{APP_NAME}: cannot start the TUI ({exc}). Run it in a real terminal.",
              file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #
def _read_stdin_lines():
    try:
        data = sys.stdin.read()
    except OSError:
        return []
    return [ln.strip() for ln in data.splitlines() if ln.strip()]


def cmd_tui(argv):
    p = argparse.ArgumentParser(prog=APP_NAME, usage="%(prog)s [timer options]",
                                description="Run the TUiDO TUI.",
                                epilog="commands: tui add ls done undone edit rm "
                                       "clear clear-all status config path banner")
    p.add_argument("--paused", action="store_true", help="open without starting")
    p.add_argument("-w", "--work", type=float, default=None, metavar="MIN")
    p.add_argument("-b", "--break", "--short-break", dest="short", type=float,
                   default=None, metavar="MIN")
    p.add_argument("-l", "--long", "--long-break", dest="long_break", type=float,
                   default=None, metavar="MIN")
    p.add_argument("-c", "--cycles", type=int, default=None, metavar="N")
    p.add_argument("-s", "--sound", default=None, metavar="NAME")
    p.add_argument("--mute", "--no-sound", dest="mute", action="store_true")
    return run_tui(p.parse_args(argv))


def cmd_add(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} add")
    p.add_argument("text", nargs="*", help='task text ("-" reads stdin)')
    p.add_argument("-t", "--top", action="store_true", help="insert at the top")
    p.add_argument("--json", action="store_true", help="print new ids as JSON")
    a = p.parse_args(argv)
    texts = list(a.text)
    if not texts or texts == ["-"]:
        texts = _read_stdin_lines()
    elif "-" in texts:
        texts = [t for t in texts if t != "-"] + _read_stdin_lines()
    ids = store_add(texts, top=a.top)
    if a.json:
        print(json.dumps(ids))
    else:
        for tid in ids:
            print(tid)
    return 0


def cmd_ls(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} ls")
    p.add_argument("--open", dest="only_open", action="store_true",
                   help="only uncompleted todos")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    todos = load_todos()["todos"]
    if a.only_open:
        todos = [t for t in todos if not t["done"]]
    if a.json:
        print(json.dumps(todos, indent=2, ensure_ascii=False))
        return 0
    if not todos:
        print("(no todos)")
        return 0
    for t in todos:
        box = "x" if t["done"] else " "
        print(f"{t['id']:>3} [{box}] {t['text']}")
    return 0


def cmd_done(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} done")
    p.add_argument("ids", nargs="+", type=int)
    a = p.parse_args(argv)
    changed = store_set_done(a.ids, True)
    print(f"done: {', '.join(map(str, changed))}" if changed else "no matching ids")
    return 0 if changed else 1


def cmd_undone(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} undone")
    p.add_argument("ids", nargs="+", type=int)
    a = p.parse_args(argv)
    changed = store_set_done(a.ids, False)
    print(f"reopened: {', '.join(map(str, changed))}" if changed else "no matching ids")
    return 0 if changed else 1


def cmd_edit(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} edit")
    p.add_argument("id", type=int)
    p.add_argument("text")
    a = p.parse_args(argv)
    if store_edit(a.id, a.text):
        print(f"edited #{a.id}")
        return 0
    print(f"no todo with id {a.id}", file=sys.stderr)
    return 1


def cmd_rm(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} rm")
    p.add_argument("ids", nargs="+", type=int)
    a = p.parse_args(argv)
    n = store_remove(a.ids)
    print(f"removed {n}")
    return 0 if n else 1


def cmd_clear(argv):
    n = store_clear_done()
    print(f"cleared {n} completed")
    return 0


def cmd_clear_all(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} clear-all")
    p.add_argument("--yes", action="store_true", help="required to actually delete")
    a = p.parse_args(argv)
    if not a.yes:
        print(f"{APP_NAME}: refusing to wipe all todos without --yes", file=sys.stderr)
        return 2
    n = store_clear_all()
    print(f"deleted {n} todos")
    return 0


def cmd_status(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} status")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    state = read_state()
    data = load_todos()
    open_n = sum(1 for t in data["todos"] if not t["done"])
    done_n = sum(1 for t in data["todos"] if t["done"])
    focus = int(data.get("focus_seconds", 0) or 0)
    if a.json:
        print(json.dumps({"timer": state, "open": open_n, "done": done_n,
                          "total": len(data["todos"]), "focus_seconds": focus},
                         indent=2, ensure_ascii=False))
        return 0
    if state:
        m, s = divmod(max(0, int(state.get("remaining", 0))), 60)
        where = "running" if state.get("running") else "paused"
        print(f"timer: {state.get('phase')} {where} {m:02d}:{s:02d} "
              f"(completed {state.get('completed_work', 0)})")
    else:
        print("timer: not running")
    print(f"focus: {format_duration(focus)}")
    print(f"todos: {open_n} open / {done_n} done / {len(data['todos'])} total")
    return 0


def cmd_focus(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} focus",
                                description="Show or reset cumulative focused time.")
    p.add_argument("--reset", action="store_true", help="set the total back to zero")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.reset:
        with _locked():
            data = load_todos()
            data["focus_seconds"] = 0
            save_todos(data)
        print("focus time reset")
        return 0
    total = get_focus_seconds()
    if a.json:
        print(json.dumps({"focus_seconds": total}))
    else:
        print(format_duration(total))
    return 0


def cmd_config(argv):
    p = argparse.ArgumentParser(prog=f"{APP_NAME} config",
                                description="Show or set remembered timer lengths (minutes).")
    p.add_argument("key", nargs="?",
                   help="one of: work, short, long, cycles (omit to list all)")
    p.add_argument("value", nargs="?", help="new value for the key")
    p.add_argument("--reset", action="store_true", help="forget saved settings")
    a = p.parse_args(argv)

    if a.reset:
        with contextlib.suppress(OSError):
            os.unlink(SETTINGS_FILE)
        print("settings reset to defaults")
        return 0

    if a.key is None:
        for key, value in effective_settings().items():
            print(f"{key} = {value}")
        return 0

    if a.key not in NUM_DEFAULTS:
        print(f"{APP_NAME}: unknown setting {a.key!r} "
              f"(expected one of: {', '.join(NUM_DEFAULTS)})", file=sys.stderr)
        return 2

    if a.value is None:
        print(effective_settings()[a.key])
        return 0

    try:
        value = int(a.value) if a.key == "cycles" else float(a.value)
    except ValueError:
        print(f"{APP_NAME}: {a.key!r} needs a number", file=sys.stderr)
        return 2
    saved = load_settings()
    saved[a.key] = value
    save_settings(saved)
    print(f"{a.key} = {value}")
    return 0


def cmd_path(argv):
    print(DATA_DIR)
    print(f"  todos:    {TODOS_FILE}")
    print(f"  state:    {STATE_FILE}")
    print(f"  settings: {SETTINGS_FILE}")
    return 0


def cmd_banner(argv):
    print(banner_text())
    return 0


def cmd_version(argv):
    print(banner_text())
    print(f"  {APP_NAME} {VERSION} - {TAGLINE}")
    return 0


DISPATCH = {
    "tui": cmd_tui,
    "run": cmd_tui,
    "add": cmd_add,
    "ls": cmd_ls,
    "list": cmd_ls,
    "done": cmd_done,
    "undone": cmd_undone,
    "edit": cmd_edit,
    "rm": cmd_rm,
    "remove": cmd_rm,
    "clear": cmd_clear,
    "clear-all": cmd_clear_all,
    "wipe": cmd_clear_all,
    "status": cmd_status,
    "config": cmd_config,
    "focus": cmd_focus,
    "settings": cmd_config,
    "path": cmd_path,
    "banner": cmd_banner,
    "version": cmd_version,
    "--version": cmd_version,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("help", "-h", "--help"):
        print(banner_text())
        print()
        print(USAGE)
        return 0

    migrate_old_data()

    if argv and not argv[0].startswith("-"):
        sub = argv.pop(0)
        handler = DISPATCH.get(sub)
        if handler is None:
            print(f"{APP_NAME}: unknown command {sub!r}\n", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        return handler(argv)

    return cmd_tui(argv)


if __name__ == "__main__":
    sys.exit(main())

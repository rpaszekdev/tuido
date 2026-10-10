#!/usr/bin/env python3
"""TUiDO - a terminal Pomodoro timer with a shared, agent-friendly todo list.

  TUiDO  =  TUI + todo.  One tool, two faces over the same data:

  * a curses TUI (`tuido`) that puts your todo list on the left and a big
    ASCII pomodoro on the right that fills with '#' as the focus timer runs
  * a scriptable CLI (`tuido add/ls/done/rm/...`) over the same todo file,
    so you - or an AI agent working in another pane - can add and complete
    items while the TUI is running. The TUI watches the file and reloads live.

Data lives as JSON under ~/.local/share/tuido/:
  todos.json   your todo list (source of truth)
  state.json   the live timer state, written by a running TUI

`tuido path` prints those paths. `tuido status` shows the timer state and
todo counts, so an agent can see what you are working on right now.

Keys in the TUI:
  space start/pause   s skip   r reset   j/k move   g/G top/bottom
  a add task   d toggle done   x delete   c clear done   v hide/show done
  q/esc quit
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
import time

APP_NAME = "tuido"
VERSION = "3.1.0"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "tuido")
TODOS_FILE = os.path.join(DATA_DIR, "todos.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOCK_FILE = os.path.join(DATA_DIR, ".lock")
OLD_DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pomodoro")

SOUND_DIR = "/System/Library/Sounds"

DEFAULT_WORK = 25
DEFAULT_SHORT = 5
DEFAULT_LONG = 15
DEFAULT_CYCLES = 4
DEFAULT_SOUND = "Glass"

PHASE_NAME = {"work": "WORK", "short": "SHORT BREAK", "long": "LONG BREAK"}
PHASE_PAIR = {"work": 1, "short": 2, "long": 3}  # curses color-pair numbers

# 3x5 bitmap font for the compact countdown.
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
  {APP_NAME} status [--json]            show live timer + todo counts
  {APP_NAME} path                       print the data file paths
  {APP_NAME} banner                     print the banner

Timer options (TUI):
  -w, --work MIN     work session length         (default {DEFAULT_WORK})
  -b, --break MIN    short break length          (default {DEFAULT_SHORT})
  -l, --long MIN     long break length           (default {DEFAULT_LONG})
  -c, --cycles N     work sessions per long break (default {DEFAULT_CYCLES})
  -s, --sound NAME   alert sound in {SOUND_DIR}
      --mute         no sound at session end
      --paused       open without starting the timer

Keys:
  space start/pause   s skip   r reset   a add   d done   x delete
  j/k move   g/G top/bottom   c clear done   v view   q/esc quit
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def to_seconds(minutes):
    """Convert minutes (int/float) to whole seconds."""
    return int(round(float(minutes) * 60))


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


def banner_text():
    return "\n".join(BANNER).rstrip("\n")


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
        """Draw ``text`` clipped to ``width`` columns at (y, x)."""
        self._add(y, x, text[:width], attr)

    def _draw(self):
        with contextlib.suppress(curses.error):
            self.scr.erase()
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

        # top bar
        self._add(0, 1, "T U i D O", acc | curses.A_BOLD)
        spin = SPINNER[tick % len(SPINNER)] if self.running else "*"
        status = f"{'running' if self.running else 'paused'} {spin} "
        self._add(0, max(w - len(status) - 1, 1), status, curses.A_DIM)

        content_top = 2
        content_bottom = h - 3
        content_h = content_bottom - content_top + 1

        two_col = w >= 56 and content_h >= 12
        if two_col:
            self._draw_wide(content_top, content_bottom, w, acc, frac, m, s, tick)
        else:
            self._draw_compact(content_top, content_bottom, w, acc, frac, m, s)

        self._draw_footer(h, w)

    def _draw_footer(self, h, w):
        hint = ("space start/pause  s skip  r reset  a add  d done  x del  "
                "j/k move  c clear  v view  q quit")
        self._add(h - 2, 0, hint, curses.A_DIM)
        if self._message and time.monotonic() < self._message_until:
            self._add(h - 1, 0, self._message, self._cp(4) | curses.A_BOLD)
        else:
            self._add(h - 1, 0, f"todos: {TODOS_FILE}", curses.A_DIM)

    # ---------------------------------------------------------- two-column
    def _draw_wide(self, top, bottom, w, acc, frac, m, s, tick):
        left_w = min(46, max(26, int(w * 0.42)))
        self._draw_tasks(1, left_w - 1, top, bottom)

        div_x = left_w
        for y in range(top, bottom + 1):
            self._add(y, div_x, "\u2502", curses.A_DIM)

        rx = div_x + 1
        rw = w - rx
        if rw < 12:
            return
        region_h = bottom - top + 1

        # Body of the tomato scales to the room we have (phase + tomato + line).
        max_half = min(TOMATO_MAX_HALF, (rw - 2) // 2)
        body_h = min(TOMATO_MAX_BODY, region_h - 2 - len(TOMATO_CROWN))
        crown = max_half >= 5 and body_h >= 6
        crow = len(TOMATO_CROWN) if crown else 0
        if not crown:
            max_half = min(max_half, (rw - 2) // 2)
        body_h = min(TOMATO_MAX_BODY, region_h - 2 - crow)
        if max_half < 4 or body_h < 5:
            self._draw_big_clock(rx, top, rw, region_h, acc, m, s, frac)
            return

        tomato = render_tomato(build_tomato(body_h, max_half, crown), crow, frac, tick)
        block_h = 1 + len(tomato) + 1
        oy = top + max(0, (region_h - block_h) // 2)
        cx = rx + rw // 2

        self._center_at(oy, cx, f"{PHASE_NAME[self.phase]}  \u00b7  {self.completed} done",
                        acc | curses.A_BOLD)
        oy += 1
        for line in tomato:
            self._center_at(oy, cx, line, acc)
            oy += 1
        self._center_at(oy, cx, f"{m:02d}:{s:02d}    {int(frac * 100):>3}%",
                        curses.A_BOLD)

    def _center_at(self, y, cx, text, attr=0):
        self._add(y, max(0, cx - len(text) // 2), text, attr)

    def _draw_tasks(self, x, width, top, bottom):
        open_n = sum(1 for t in self.todos if not t["done"])
        done_n = len(self.todos) - open_n
        self._put(top, x, f"TODOS  {open_n} open \u00b7 {done_n} done",
                  width, curses.A_BOLD)
        avail = bottom - (top + 1) + 1
        vis = self._visible()
        if not vis:
            self._put(top + 1, x + 2, "no tasks - press a", width - 2, curses.A_DIM)
            return
        start = 0
        if self.sel >= avail and avail > 0:
            start = self.sel - avail + 1
        for pos, idx in enumerate(vis[start:start + max(0, avail)]):
            t = self.todos[idx]
            real = start + pos
            box = "[x]" if t["done"] else "[ ]"
            attr = curses.A_DIM if t["done"] else 0
            if real == self.sel:
                attr |= curses.A_REVERSE
            self._put(top + 1 + pos, x, f"{t['id']:>3} {box} {t['text']}", width, attr)

    # ------------------------------------------------------------ compact
    def _draw_compact(self, top, bottom, w, acc, frac, m, s):
        y = top
        self._center_line(y, w, PHASE_NAME[self.phase], acc | curses.A_BOLD)
        y += 1
        if (bottom - y) >= 8:
            for line in big_time(f"{m:02d}:{s:02d}"):
                self._center_line(y, w, line, acc | curses.A_BOLD)
                y += 1
        else:
            self._center_line(y, w, f"{m:02d}:{s:02d}", acc | curses.A_BOLD)
            y += 1
        bar_w = min(w - 8, 44)
        filled = int(round(bar_w * frac))
        self._center_line(y, w, "\u2588" * filled + "\u2591" * (bar_w - filled), acc)
        y += 2
        self._draw_tasks(2, w - 4, y, bottom)

    def _center_line(self, y, w, text, attr=0):
        self._add(y, max(0, (w - len(text)) // 2), text, attr)

    def _draw_big_clock(self, rx, top, rw, region_h, acc, m, s, frac):
        cx = rx + rw // 2
        y = top + max(0, (region_h - 8) // 2)
        self._center_at(y, cx, PHASE_NAME[self.phase], acc | curses.A_BOLD)
        y += 1
        for line in big_time(f"{m:02d}:{s:02d}"):
            self._center_at(y, cx, line, acc | curses.A_BOLD)
            y += 1
        self._center_at(y, cx, f"{int(frac * 100):>3}%", curses.A_DIM)

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

    def _add_flow(self):
        text = self._prompt("new task: ")
        if text and text.strip():
            ids = store_add([text])
            self._reload(force=True)
            if ids:
                self._message_set(f"added #{ids[0]}")

    def _prompt(self, label):
        h, w = self.scr.getmaxyx()
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        self.scr.nodelay(False)
        self.scr.timeout(-1)
        text = ""
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

    # ---------------------------------------------------------------- keys
    def _handle_keys(self):
        while True:
            try:
                ch = self.scr.getch()
            except curses.error:
                return
            if ch == -1 or ch == curses.KEY_RESIZE:
                return
            if ch in (ord("q"), 27):
                self.alive = False
                return
            if ch == ord(" "):
                self._toggle()
            elif ch == ord("s"):
                self._skip()
            elif ch == ord("r"):
                self._reset()
            elif ch == ord("a"):
                self._add_flow()
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
            elif ch == ord("G"):
                self.sel = max(0, len(self._visible()) - 1)
            elif ch == ord("c"):
                self._clear_done()
            elif ch == ord("v"):
                self.show_all = not self.show_all
                self._clamp_sel()

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
                "todos_open": sum(1 for t in self.todos if not t["done"]),
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
    cfg = {
        "work": to_seconds(args.work),
        "short": to_seconds(args.short),
        "long": to_seconds(args.long_break),
        "cycles": args.cycles,
        "sound": args.sound,
        "mute": args.mute,
        "autostart": not args.paused,
    }
    with contextlib.suppress(Exception):
        locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda scr: Tui(scr, cfg).run())
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
                                       "clear clear-all status path banner")
    p.add_argument("--paused", action="store_true", help="open without starting")
    p.add_argument("-w", "--work", type=float, default=DEFAULT_WORK, metavar="MIN")
    p.add_argument("-b", "--break", "--short-break", dest="short", type=float,
                   default=DEFAULT_SHORT, metavar="MIN")
    p.add_argument("-l", "--long", "--long-break", dest="long_break", type=float,
                   default=DEFAULT_LONG, metavar="MIN")
    p.add_argument("-c", "--cycles", type=int, default=DEFAULT_CYCLES, metavar="N")
    p.add_argument("-s", "--sound", default=DEFAULT_SOUND, metavar="NAME")
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
    if a.json:
        print(json.dumps({"timer": state, "open": open_n,
                          "total": len(data["todos"])}, indent=2, ensure_ascii=False))
        return 0
    if state:
        m, s = divmod(max(0, int(state.get("remaining", 0))), 60)
        where = "running" if state.get("running") else "paused"
        print(f"timer: {state.get('phase')} {where} {m:02d}:{s:02d} "
              f"(completed {state.get('completed_work', 0)})")
    else:
        print("timer: not running")
    print(f"todos: {open_n} open / {len(data['todos'])} total")
    return 0


def cmd_path(argv):
    print(DATA_DIR)
    print(f"  todos: {TODOS_FILE}")
    print(f"  state: {STATE_FILE}")
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

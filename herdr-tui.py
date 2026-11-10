#!/usr/bin/env python3
"""herdr-tui: a terminal (SSH/phone-friendly) front-end for Herdr.

The Herdr desktop app is a client of herdr-core (`hc`), which owns every
terminal and agent, and it exposes its own structure (spaces, tabs) through
`herdr-app ls` / `herdr-app do`. This TUI uses both: it shows the same spaces
and tabs in the same order as the window, lets you peek, answer, send,
interrupt and attach (ctrl+\\ to come back), and create, rename, reorder and
close spaces and tabs. If the app isn't running it falls back to `hc` alone.

No dependencies beyond macOS's stock python3. Press ? inside for keys.
"""
import curses
import glob
import json
import locale
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

BIN = os.path.expanduser("~/.local/bin")
HC = os.environ.get("HC") or shutil.which("hc") or os.path.join(BIN, "hc")
APP = os.environ.get("HERDR_APP") or shutil.which("herdr-app") or os.path.join(BIN, "herdr-app")
APP_BUNDLE = os.environ.get("HERDR_APP_BUNDLE") or os.path.expanduser("~/Applications/Herdr.app")
REFRESH = float(os.environ.get("HERDR_TUI_REFRESH", "2"))

URGENCY = {"blocked": 0, "done": 1, "working": 2, "idle": 3, None: 4}
GLYPH = {"blocked": "!", "done": "✓", "working": "◐", "idle": "·", None: " "}
SPINNER = "◐◓◑◒"

HELP = """\
LIST
  ↑↓ j k      move            !   next agent needing you
  ⏎           attach agent (ctrl+\\ back) / fold space
  p → space   peek (live screen, answer prompts)
  s           send a message  i   interrupt
STRUCTURE (space row → space, agent row → its tab)
  c           new space       n   new tab in this space
  R           rename          J K move down / up
  X           close (asks)    o   open Herdr app if it's down
PEEK
  ↑↓ PgUp/Dn  scroll          1-9 ⏎ x  pick / enter / esc
  s i a       send / interrupt / attach      q  back
q quits to your shell.   any key closes this help"""


def run(cmd, *args, timeout=15):
    try:
        p = subprocess.run([cmd, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except FileNotFoundError:
        return 127, "", f"{cmd} not found"
    return p.returncode, p.stdout, p.stderr


def hc(*args, timeout=15):
    return run(HC, *args, timeout=timeout)


def app_do(command, **kw):
    """herdr-app do <command> <json>. Returns (ok, dict-or-error-text)."""
    args = {k: v for k, v in kw.items() if v is not None}
    code, out, err = run(APP, "do", command, json.dumps(args), timeout=20)
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {}
    if code != 0 or "error" in data:
        return False, data.get("error") or (err or out).strip() or f"{command} failed ({code})"
    return True, data


def short_agent(name):
    return {"claude-code": "claude"}.get(name or "", name or "")


def last_line(text, fallback):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else fallback


# ---------------------------------------------------------------- data


def load_world():
    """Returns (spaces, app_ok, err). spaces = [{id,name,folder,machine,tabs:[{id,name,terms:[...]}]}]."""
    code, out, err = hc("ls", "--json")
    hc_terms = {}
    hc_err = ""
    if code == 0:
        for t in json.loads(out or "{}").get("terminals", []):
            hc_terms[t["address"]] = t
    else:
        hc_err = last_line(err or out, f"hc ls failed ({code})")

    code, out, err = run(APP, "ls", timeout=6)
    if code == 0:
        try:
            doc = json.loads(out)
            return _from_app(doc, hc_terms), True, hc_err
        except (ValueError, KeyError, TypeError):
            pass
    return _from_hc(hc_terms), False, hc_err


def _term(entry, hc_terms, depth):
    if isinstance(entry, str):
        entry = {"terminal": entry}
    addr = entry.get("terminal")
    if not addr:
        return []
    h = hc_terms.get(addr, {})
    t = {
        "address": addr,
        "agent": entry.get("app") or h.get("agent"),
        "status": entry.get("status") if "status" in entry else h.get("status"),
        "cwd": h.get("cwd"),
        "name": entry.get("name"),
        "depth": depth,
    }
    out = [t]
    for sub in entry.get("sub_agents") or []:
        out += _term(sub, hc_terms, depth + 1)
    return out


def _from_app(doc, hc_terms):
    spaces = []
    for s in doc.get("spaces", []):
        sp = {"id": s["id"], "name": s.get("name") or "~", "folder": s.get("folder"),
              "machine": s.get("machine"), "tabs": []}
        for tb in s.get("tabs", []):
            terms = []
            for e in tb.get("terminals", []):
                terms += _term(e, hc_terms, 0)
            sp["tabs"].append({"id": tb["id"], "name": tb.get("name") or "", "terms": terms})
        spaces.append(sp)
    return spaces


def _from_hc(hc_terms):
    by_space = {}
    for t in hc_terms.values():
        space, _, tab = (t.get("where") or "").partition(" / ")
        sp = by_space.setdefault(space or "(no space)", {"id": None, "name": space or "(no space)",
                                                         "folder": None, "machine": None, "tabs": []})
        tb = next((x for x in sp["tabs"] if x["name"] == tab), None)
        if tb is None:
            tb = {"id": None, "name": tab, "terms": []}
            sp["tabs"].append(tb)
        tb["terms"].append({"address": t["address"], "agent": t.get("agent"), "status": t.get("status"),
                            "cwd": t.get("cwd"), "name": None, "depth": 0})
    return list(by_space.values())


def space_status(sp):
    st = [t["status"] for tb in sp["tabs"] for t in tb["terms"]]
    return min(st, key=lambda s: URGENCY.get(s, 4)) if st else None


def build_rows(spaces, folded):
    """('space', sp) | ('tab', sp, tab) for empty tabs | ('term', sp, tab, t)."""
    rows = []
    for sp in spaces:
        rows.append(("space", sp))
        if sp["id"] in folded or sp["name"] in folded:
            continue
        for tb in sp["tabs"]:
            if not tb["terms"]:
                rows.append(("tab", sp, tb))
            for t in tb["terms"]:
                rows.append(("term", sp, tb, t))
    return rows


def row_key(r):
    if r[0] == "space":
        return ("space", r[1]["id"] or r[1]["name"])
    if r[0] == "tab":
        return ("tab", r[2]["id"])
    return ("term", r[3]["address"])


def complete_path(text):
    """Tab-completion for folder prompts."""
    expanded = os.path.expanduser(text)
    matches = sorted(m for m in glob.glob(glob.escape(expanded) + "*") if os.path.isdir(m))
    if not matches:
        return text
    common = os.path.commonprefix(matches)
    if len(matches) == 1:
        common = matches[0].rstrip("/") + "/"
    home = os.path.expanduser("~")
    if text.startswith("~") and common.startswith(home):
        common = "~" + common[len(home):]
    return common if len(common) >= len(text) else text


# ---------------------------------------------------------------- app


class App:
    def __init__(self, scr):
        self.scr = scr
        self.spaces = []
        self.app_ok = True
        self.err = ""
        self.msg = ""
        self.msg_until = 0
        self.folded = set()
        self.sel = None  # row key
        self.top = 0
        self.lock = threading.Lock()
        self.tick = 0
        self.busy = False

    # ---------- infra ----------
    def flash(self, text, secs=4):
        self.msg = text
        self.msg_until = time.time() + secs

    def refresh_data(self):
        try:
            spaces, app_ok, err = load_world()
            with self.lock:
                self.spaces, self.app_ok, self.err = spaces, app_ok, err
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.err = str(e)

    def bg(self, label, fn):
        if self.busy:
            self.flash("busy…")
            return
        self.busy = True
        self.flash(label + "…", 120)

        def go():
            try:
                self.flash(fn() or "ok")
            finally:
                self.busy = False
                self.refresh_data()

        threading.Thread(target=go, daemon=True).start()

    def colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_RED, bg)
        curses.init_pair(2, curses.COLOR_GREEN, bg)
        curses.init_pair(3, curses.COLOR_YELLOW, bg)
        curses.init_pair(4, curses.COLOR_CYAN, bg)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_RED)
        self.C = {
            "blocked": curses.color_pair(1) | curses.A_BOLD,
            "done": curses.color_pair(2) | curses.A_BOLD,
            "working": curses.color_pair(3),
            "idle": 0,
            None: curses.A_DIM,
            "space": curses.color_pair(4) | curses.A_BOLD,
            "bar": curses.color_pair(5),
            "warn": curses.color_pair(6) | curses.A_BOLD,
        }

    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = s[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def bottom(self, text, attr=curses.A_BOLD):
        h, w = self.scr.getmaxyx()
        self.scr.move(h - 1, 0)
        self.scr.clrtoeol()
        self.put(h - 1, 0, text, attr)

    def prompt(self, label, initial="", complete=None):
        """Single-line editor on the bottom row. Returns text or None on Esc."""
        curses.curs_set(1)
        buf = list(initial)
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = label + "".join(buf)
                shown = text[-(w - 1):]
                self.bottom(shown)
                self.scr.move(h - 1, min(len(shown), w - 1))
                self.scr.refresh()
                try:
                    ch = self.scr.get_wch()
                except curses.error:
                    continue
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf)
                if ch == "\x1b":
                    return None
                if ch == "\t" and complete:
                    buf = list(complete("".join(buf)))
                elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif ch == "\x15":  # ctrl+u
                    buf = []
                elif ch == "\x17":  # ctrl+w
                    s = "".join(buf).rstrip()
                    cut = max(s.rfind(" "), s.rfind("/", 0, len(s) - 1))
                    buf = list(s[: cut + 1]) if cut >= 0 else []
                elif isinstance(ch, str) and ch.isprintable():
                    buf.append(ch)
        finally:
            curses.curs_set(0)
            self.scr.timeout(250)

    def choose(self, label, options):
        """options: [(key, text)]. Returns chosen key or None."""
        keys = {k for k, _ in options}
        line = label + " " + " ".join(f"[{k}]{t}" for k, t in options) + "  esc"
        self.scr.timeout(-1)
        try:
            while True:
                self.bottom(line)
                self.scr.refresh()
                try:
                    ch = self.scr.get_wch()
                except curses.error:
                    continue
                if ch == "\x1b":
                    return None
                if isinstance(ch, str) and ch.lower() in keys:
                    return ch.lower()
                if ch in ("\n", "\r") and options:
                    return options[0][0]
        finally:
            self.scr.timeout(250)

    def confirm(self, question):
        self.scr.timeout(-1)
        try:
            self.bottom(question + " [y/N] ", self.C["warn"])
            self.scr.refresh()
            try:
                ch = self.scr.get_wch()
            except curses.error:
                return False
            return ch in ("y", "Y")
        finally:
            self.scr.timeout(250)

    def show_help(self):
        self.scr.erase()
        for y, line in enumerate(HELP.splitlines()):
            self.put(y, 0, line)
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            self.scr.get_wch()
        except curses.error:
            pass
        self.scr.timeout(250)

    def attach(self, addr):
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout.write(f"\r\nattaching to {addr} — ctrl+\\ to come back\r\n")
        sys.stdout.flush()
        subprocess.call([HC, "attach", addr])
        curses.reset_prog_mode()
        self.scr.clear()
        self.refresh_data()

    # ---------- terminal actions ----------
    def act_send(self, t):
        text = self.prompt(f"send → {t['address']}: ")
        if not text:
            return
        args = ["send", t["address"], text]
        if t.get("status") == "blocked":
            args.insert(1, "--force")

        def go():
            code, out, err = hc(*args, timeout=120)
            return f"sent to {t['address']}" if code == 0 else last_line(err or out, f"send failed ({code})")

        self.bg("sending", go)

    def act_interrupt(self, t):
        def go():
            code, out, err = hc("interrupt", t["address"], timeout=20)
            return last_line(out or err, "interrupted" if code == 0 else f"failed ({code})")

        self.bg("interrupting", go)

    def act_keys(self, t, *keys):
        code, out, err = hc("keys", t["address"], *keys, timeout=10)
        self.flash(f"pressed {' '.join(keys)}" if code == 0 else last_line(err or out, "failed"))

    # ---------- structure actions ----------
    def need_app(self):
        if self.app_ok:
            return True
        self.flash("needs the Herdr app running — press o to open it", 6)
        return False

    def ask_program(self):
        """Returns (argv or None, task or None), or False if cancelled."""
        pick = self.choose("run:", [("c", "claude"), ("s", "shell"), ("o", "other")])
        if pick is None:
            return False
        argv = None
        if pick == "c":
            argv = ["claude"]
        elif pick == "o":
            cmd = self.prompt("command: ")
            if not cmd:
                return False
            argv = shlex.split(cmd)
        task = None
        if argv:
            task = self.prompt("first message (⏎ for none): ")
            if task is None:
                return False
            task = task or None
        return argv, task

    def after_create(self, ok, res, what):
        if not ok:
            self.flash(f"{what} failed: {res}", 6)
            return
        self.refresh_data()
        if res.get("terminal"):
            self.sel = ("term", res["terminal"])
        self.flash(f"{what} created" + (f" · {res['terminal']}" if res.get("terminal") else ""))

    def act_new_space(self, sp, t):
        if not self.need_app():
            return
        default = (t or {}).get("cwd") or (sp or {}).get("folder") or os.path.expanduser("~")
        home = os.path.expanduser("~")
        if default.startswith(home):
            default = "~" + default[len(home):]
        folder = self.prompt("new space folder (tab completes): ", default.rstrip("/") + "/", complete_path)
        if folder is None:
            return
        folder = os.path.abspath(os.path.expanduser(folder.strip() or "~"))
        if not os.path.isdir(folder):
            self.flash(f"no such folder: {folder}", 6)
            return
        prog = self.ask_program()
        if prog is False:
            return
        argv, task = prog
        ok, res = app_do("space.new", folder=folder, argv=argv, task=task, machine=(sp or {}).get("machine"))
        self.after_create(ok, res, "space")

    def act_new_tab(self, sp, t):
        if not self.need_app() or not sp:
            return
        prog = self.ask_program()
        if prog is False:
            return
        argv, task = prog
        folder = (t or {}).get("cwd") or sp.get("folder")
        ok, res = app_do("tab.new", space=sp["id"], folder=folder, argv=argv, task=task)
        self.after_create(ok, res, "tab")

    def act_rename(self, kind, sp, tb):
        if not self.need_app():
            return
        if kind == "space":
            name = self.prompt(f"rename space (empty = auto): ", sp["name"])
            if name is None:
                return
            ok, res = app_do("space.rename", id=sp["id"], name=name)
        else:
            cur = tb["name"] if not tb["name"].isdigit() else ""
            name = self.prompt(f"rename tab in {sp['name']} (empty = number): ", cur)
            if name is None:
                return
            ok, res = app_do("tab.rename", id=tb["id"], name=name)
        self.flash("renamed" if ok else f"rename failed: {res}", 4 if ok else 6)
        self.refresh_data()

    def act_move(self, kind, sp, tb, d):
        if not self.need_app():
            return
        if kind == "space":
            peers = [s for s in self.spaces if s.get("machine") == sp.get("machine")]
            pos = next(i for i, s in enumerate(peers) if s["id"] == sp["id"])
            new = pos + d
            if not 0 <= new < len(peers):
                return
            ok, res = app_do("space.move", id=sp["id"], index=new + 1)
        else:
            pos = next(i for i, x in enumerate(sp["tabs"]) if x["id"] == tb["id"])
            new = pos + d
            if not 0 <= new < len(sp["tabs"]):
                return
            ok, res = app_do("tab.move", id=tb["id"], index=new + 1)
        if not ok:
            self.flash(f"move failed: {res}", 6)
        self.refresh_data()

    def act_close(self, kind, sp, tb):
        if not self.need_app():
            return
        if kind == "space":
            n = sum(len(x["terms"]) for x in sp["tabs"])
            if not self.confirm(f"close space {sp['name']} and end {n} terminal(s)?"):
                return
            ok, res = app_do("space.close", id=sp["id"], confirmed=True)
        else:
            last = len(sp["tabs"]) == 1
            q = f"close tab {tb['name'] or '?'} ({len(tb['terms'])} terminal(s))"
            q += f" — last tab, closes space {sp['name']}?" if last else "?"
            if not self.confirm(q):
                return
            ok, res = app_do("tab.close", id=tb["id"], confirmed=True)
        self.flash("closed" if ok else f"close failed: {res}", 4 if ok else 6)
        self.refresh_data()

    def open_app(self):
        code, out, err = run("open", APP_BUNDLE, timeout=10)
        self.flash("opening Herdr…" if code == 0 else last_line(err or out, "couldn't open Herdr"), 6)

    # ---------- list view ----------
    def draw(self, rows):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        counts = {}
        for sp in self.spaces:
            for tb in sp["tabs"]:
                for t in tb["terms"]:
                    if t["status"]:
                        counts[t["status"]] = counts.get(t["status"], 0) + 1
        spin = SPINNER[self.tick % 4] if counts.get("working") else " "
        parts = [f"{counts[s]} {s}" for s in ("blocked", "done", "working", "idle") if counts.get(s)]
        head = f" herdr {spin} " + (" · ".join(parts) or "no agents")
        clock = time.strftime("%H:%M ")
        self.put(0, 0, head.ljust(w), self.C["bar"])
        if len(head) + len(clock) < w:
            self.put(0, w - len(clock), clock, self.C["bar"])

        top_y = 1
        if not self.app_ok:
            self.put(1, 0, " Herdr app not running — read-only structure · o: open it ".ljust(w), self.C["warn"])
            top_y = 2
        body_h = h - top_y - 1

        keys = [row_key(r) for r in rows]
        if self.sel not in keys:
            self.sel = next((k for k in keys if k[0] == "term"), keys[0] if keys else None)
        sel_i = keys.index(self.sel) if self.sel in keys else 0
        if sel_i < self.top:
            self.top = sel_i
        if sel_i >= self.top + body_h:
            self.top = sel_i - body_h + 1
        self.top = max(0, min(self.top, max(0, len(rows) - body_h)))

        for y, i in enumerate(range(self.top, min(len(rows), self.top + body_h)), start=top_y):
            r = rows[i]
            selected = i == sel_i
            if r[0] == "space":
                sp = r[1]
                folded = (sp["id"] or sp["name"]) in self.folded
                st = space_status(sp)
                mark = "▸" if folded else "▾"
                tail = f" {GLYPH.get(st, ' ')}" if st else ""
                if folded:
                    n = sum(1 for tb in sp["tabs"] for t in tb["terms"] if t["status"])
                    tail += f" {n} agent{'s' if n != 1 else ''}" if n else ""
                line = f"{mark} {sp['name']}" + (f" @{sp['machine']}" if sp.get("machine") else "")
                attr = self.C["space"]
                if selected:
                    self.put(y, 0, (line + tail).ljust(w), curses.A_REVERSE | curses.A_BOLD)
                else:
                    self.put(y, 0, line, attr)
                    self.put(y, len(line), tail, self.C.get(st, 0))
                continue
            if r[0] == "tab":
                line = f"    {r[2]['name'] or '?'} · (empty)"
                self.put(y, 0, line.ljust(w) if selected else line, curses.A_REVERSE if selected else curses.A_DIM)
                continue
            _, sp, tb, t = r
            st = t.get("status")
            indent = "  " + "  " * t["depth"] + ("↳" if t["depth"] else "")
            left = f"{indent}{GLYPH.get(st, '?')} "
            label = t.get("name") or short_agent(t.get("agent")) or "shell"
            mid_w = max(4, min(40, w - len(left) - 9 - 5))
            mid = f"{tb['name'] or '-'} · {label}"[:mid_w].ljust(mid_w)
            line = f"{left}{mid} {(st or '')[:8]:<8} {t['address']:>4}"
            attr = self.C.get(st, 0) if st else curses.A_DIM
            if selected:
                attr = curses.A_REVERSE | (attr & curses.A_BOLD)
                line = line.ljust(w)
            self.put(y, 0, line, attr)

        cur = rows[sel_i] if rows else None
        if self.err:
            footer = "! " + self.err
        elif self.msg and time.time() < self.msg_until:
            footer = self.msg
        elif cur and cur[0] == "space":
            footer = "⏎fold n:tab c:space R:ren J/K:move X:close ?"
        elif cur and cur[0] == "term":
            footer = "⏎attach p:peek s:send i:int n c R J/K X ?"
        else:
            footer = "c:new space n:new tab ?:help q:quit"
        self.put(h - 1, 0, footer.ljust(w), self.C["bar"])
        self.scr.refresh()

    def move_sel(self, rows, d):
        keys = [row_key(r) for r in rows]
        if not keys:
            return
        cur = keys.index(self.sel) if self.sel in keys else 0
        self.sel = keys[max(0, min(len(keys) - 1, cur + d))]

    def current(self, rows):
        for r in rows:
            if row_key(r) == self.sel:
                return r
        return None

    def run(self):
        locale.setlocale(locale.LC_ALL, "")
        curses.curs_set(0)
        self.colors()
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        self.scr.timeout(250)
        self.refresh_data()
        last = time.time()
        while True:
            with self.lock:
                rows = build_rows(self.spaces, self.folded)
            self.draw(rows)
            try:
                ch = self.scr.get_wch()
            except curses.error:
                ch = None
            if time.time() - last >= REFRESH:
                self.tick += 1
                threading.Thread(target=self.refresh_data, daemon=True).start()
                last = time.time()
            if ch is None:
                continue
            r = self.current(rows)
            kind = r[0] if r else None
            sp = r[1] if r else None
            tb = r[2] if r and kind != "space" else None
            t = r[3] if kind == "term" else None
            if ch in ("q", "Q"):
                return
            elif ch == "?":
                self.show_help()
            elif ch in (curses.KEY_DOWN, "j"):
                self.move_sel(rows, 1)
            elif ch in (curses.KEY_UP, "k"):
                self.move_sel(rows, -1)
            elif ch == curses.KEY_NPAGE:
                self.move_sel(rows, 10)
            elif ch == curses.KEY_PPAGE:
                self.move_sel(rows, -10)
            elif ch in ("g", curses.KEY_HOME):
                self.move_sel(rows, -10**6)
            elif ch in ("G", curses.KEY_END):
                self.move_sel(rows, 10**6)
            elif ch == "r":
                self.refresh_data()
            elif ch == "o":
                self.open_app()
            elif ch == "!":
                need = [("term", x[3]["address"]) for x in rows
                        if x[0] == "term" and x[3].get("status") in ("blocked", "done")]
                if not need:
                    # also look inside folded spaces
                    for s in self.spaces:
                        for x in s["tabs"]:
                            for y in x["terms"]:
                                if y.get("status") in ("blocked", "done"):
                                    self.folded.discard(s["id"] or s["name"])
                                    need.append(("term", y["address"]))
                if need:
                    self.sel = need[(need.index(self.sel) + 1) % len(need)] if self.sel in need else need[0]
                else:
                    self.flash("nobody needs you")
            elif ch == "c":
                self.act_new_space(sp, t)
            elif ch == "n":
                self.act_new_tab(sp, t)
            elif ch == "R" and r:
                self.act_rename("space" if kind == "space" else "tab", sp, tb)
            elif ch in ("J", "K") and r:
                self.act_move("space" if kind == "space" else "tab", sp, tb, 1 if ch == "J" else -1)
            elif ch == "X" and r:
                self.act_close("space" if kind == "space" else "tab", sp, tb)
            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                i = self.top + my - (1 if self.app_ok else 2)
                if 0 <= i < len(rows) and bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED):
                    k = row_key(rows[i])
                    if k == self.sel:
                        if rows[i][0] == "term":
                            Peek(self, rows[i][3], rows[i][1], rows[i][2]).run()
                        elif rows[i][0] == "space":
                            self.toggle_fold(rows[i][1])
                    self.sel = k
                elif bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                    self.move_sel(rows, -1)
                elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                    self.move_sel(rows, 1)
            elif ch in ("\n", "\r", curses.KEY_ENTER, "a"):
                if kind == "term":
                    self.attach(t["address"])
                elif kind == "space":
                    self.toggle_fold(sp)
            elif ch in ("p", " ", curses.KEY_RIGHT, "l"):
                if kind == "term":
                    Peek(self, t, sp, tb).run()
                elif kind == "space":
                    self.toggle_fold(sp)
            elif ch == "s" and t:
                self.act_send(t)
            elif ch == "i" and t:
                self.act_interrupt(t)
            elif ch == curses.KEY_RESIZE:
                self.scr.clear()

    def toggle_fold(self, sp):
        k = sp["id"] or sp["name"]
        self.folded.symmetric_difference_update({k})


class Peek:
    """Live view of one terminal's screen + history."""

    def __init__(self, app, t, sp, tb):
        self.app = app
        self.t = t
        self.where = f"{sp['name']}/{tb['name']}"
        self.lines = []
        self.off = 0
        self.last = 0

    def load(self):
        code, out, err = hc("read", self.t["address"], "--lines", "400")
        if code == 0:
            lines = out.rstrip("\n").split("\n")
            while lines and not lines[-1].strip():
                lines.pop()
            self.lines = lines
        else:
            self.lines = [f"(read failed: {(err or out).strip()})"]
        for sp in self.app.spaces:
            for tb in sp["tabs"]:
                for nt in tb["terms"]:
                    if nt["address"] == self.t["address"]:
                        self.t = nt
        self.last = time.time()

    def draw(self):
        a = self.app
        a.scr.erase()
        h, w = a.scr.getmaxyx()
        st = self.t.get("status")
        label = self.t.get("name") or short_agent(self.t.get("agent")) or "shell"
        a.put(0, 0, f" {self.t['address']} {self.where} {label} {st or ''}".ljust(w),
              a.C["warn"] if st == "blocked" else a.C["bar"])
        body = h - 2
        self.off = max(0, min(self.off, max(0, len(self.lines) - body)))
        end = len(self.lines) - self.off
        for y, ln in enumerate(self.lines[max(0, end - body):end], start=1):
            a.put(y, 0, ln.expandtabs(4))
        if a.msg and time.time() < a.msg_until:
            foot = a.msg
        elif st == "blocked":
            foot = "BLOCKED 1-9:pick ⏎:enter x:esc s:send a:att q"
        else:
            foot = "s:send i:int 1-9 ⏎ x:esc a:attach q:back"
        if self.off:
            foot = f"↑{self.off} " + foot
        a.put(h - 1, 0, foot.ljust(w), a.C["bar"])
        a.scr.refresh()

    def run(self):
        a = self.app
        self.load()
        while True:
            self.draw()
            try:
                ch = a.scr.get_wch()
            except curses.error:
                ch = None
            if time.time() - self.last >= REFRESH:
                threading.Thread(target=a.refresh_data, daemon=True).start()
                self.load()
            if ch is None:
                continue
            h, _ = a.scr.getmaxyx()
            if ch in ("q", "Q", curses.KEY_LEFT, "\x1b"):
                return
            elif ch in (curses.KEY_UP, "k"):
                self.off += 1
            elif ch in (curses.KEY_DOWN, "j"):
                self.off -= 1
            elif ch in (curses.KEY_PPAGE, "b"):
                self.off += h - 3
            elif ch in (curses.KEY_NPAGE, " ", "f"):
                self.off -= h - 3
            elif ch == "G":
                self.off = 0
            elif ch == "g":
                self.off = 10**6
            elif ch == "r":
                self.load()
            elif ch == "s":
                a.act_send(self.t)
            elif ch == "i":
                a.act_interrupt(self.t)
            elif ch == "a":
                a.attach(self.t["address"])
                self.load()
            elif isinstance(ch, str) and ch in "123456789":
                a.act_keys(self.t, ch)
                self.load()
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                a.act_keys(self.t, "enter")
                self.load()
            elif ch == "x":
                a.act_keys(self.t, "esc")
                self.load()
            elif ch == curses.KEY_MOUSE:
                try:
                    _, _, _, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                    self.off += 3
                elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                    self.off -= 3


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    if not os.path.exists(HC):
        sys.exit(f"herdr-tui: can't find hc (looked at {HC}); set HC=/path/to/hc")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: App(scr).run())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""macrokb_ui: desktop editor for macrokb. Shows a numpad; click a key to bind an action.

Run:  python macrokb_ui.py
Needs Tkinter (Fedora: sudo dnf install python3-tkinter), plus evdev + pyyaml.
Keep it in the same folder as macrokb.py, actions.py and config.yaml.
"""
import inspect
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import yaml

import Actions as actions
import Macrokb as macrokb

CONFIG = Path(__file__).with_name("config.yaml")
DAEMON = Path(__file__).with_name("Macrokb.py")
LOG = Path(__file__).with_name("macrokb.log")

# (evdev key name, label, row, col, rowspan, colspan)
LAYOUT = [
    ("KEY_NUMLOCK", "Num", 0, 0, 1, 1), ("KEY_KPSLASH", "/", 0, 1, 1, 1),
    ("KEY_KPASTERISK", "*", 0, 2, 1, 1), ("KEY_KPMINUS", "-", 0, 3, 1, 1),
    ("KEY_KP7", "7", 1, 0, 1, 1), ("KEY_KP8", "8", 1, 1, 1, 1),
    ("KEY_KP9", "9", 1, 2, 1, 1), ("KEY_KPPLUS", "+", 1, 3, 2, 1),
    ("KEY_KP4", "4", 2, 0, 1, 1), ("KEY_KP5", "5", 2, 1, 1, 1),
    ("KEY_KP6", "6", 2, 2, 1, 1),
    ("KEY_KP1", "1", 3, 0, 1, 1), ("KEY_KP2", "2", 3, 1, 1, 1),
    ("KEY_KP3", "3", 3, 2, 1, 1), ("KEY_KPENTER", "Enter", 3, 3, 2, 1),
    ("KEY_KP0", "0", 4, 0, 1, 2), ("KEY_KPDOT", ".", 4, 2, 1, 1),
]


def to_text(v) -> str:
    if isinstance(v, (list, dict)):
        return yaml.safe_dump(v, default_flow_style=False, sort_keys=False).rstrip()
    return str(v)


def summarize(spec) -> str:
    if not spec:
        return ""
    if isinstance(spec, str):
        return spec
    params = " ".join(
        "..." if isinstance(v, (list, dict)) else str(v)
        for k, v in spec.items() if k not in ("action", "repeat"))
    if len(params) > 22:
        params = params[:21] + "…"
    return str(spec.get("action", "?")) + ("\n" + params if params else "")


# ---------------------------------------------------------------- edit dialog

class EditDialog(tk.Toplevel):
    """Pick an action and fill in its parameters (built from the function signature)."""

    def __init__(self, parent, key, spec):
        super().__init__(parent)
        self.title(f"Bind {key}")
        self.transient(parent)
        self.result = None          # dict, "clear", or None (cancel)
        self.fields = {}
        self.spec = {"action": spec} if isinstance(spec, str) else dict(spec or {})

        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text="Action").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.action = tk.StringVar(value=self.spec.get("action", ""))
        cb = ttk.Combobox(frm, textvariable=self.action, state="readonly",
                          values=sorted(actions.ACTIONS), width=28)
        cb.grid(row=0, column=1, sticky="ew")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.build_params())

        self.desc = ttk.Label(frm, wraplength=380, foreground="gray")
        self.desc.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 6))
        self.pframe = ttk.Frame(frm)
        self.pframe.grid(row=2, column=0, columnspan=2, sticky="ew")

        self.repeat = tk.BooleanVar(value=bool(self.spec.get("repeat")))
        ttk.Checkbutton(frm, text="Repeat while key is held",
                        variable=self.repeat).grid(row=3, column=0, columnspan=2,
                                                   sticky="w", pady=(8, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Save", command=self.on_save).pack(side="left", padx=2)
        ttk.Button(btns, text="Clear binding", command=self.on_clear).pack(side="left", padx=2)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=2)

        self.build_params()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_visibility()
        self.grab_set()

    def build_params(self):
        for w in self.pframe.winfo_children():
            w.destroy()
        self.fields = {}
        fn = actions.ACTIONS.get(self.action.get())
        if fn is None:
            self.desc.config(text="Choose an action.")
            return
        self.desc.config(text=(fn.__doc__ or "").strip())
        same = self.spec.get("action") == self.action.get()
        row = 0
        for pname, p in inspect.signature(fn).parameters.items():
            if pname == "ctx":
                continue
            optional = p.default is not p.empty
            ttk.Label(self.pframe, text=pname + (" (optional)" if optional else "")
                      ).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
            cur = self.spec.get(pname) if same else None
            if p.annotation is list:
                w = tk.Text(self.pframe, width=44, height=6)
                if cur is not None:
                    w.insert("1.0", to_text(cur))
            else:
                w = ttk.Entry(self.pframe, width=46)
                if cur is not None:
                    w.insert(0, to_text(cur))
            w.grid(row=row, column=1, sticky="ew", pady=2)
            self.fields[pname] = (p, w)
            row += 1
        if not self.fields:
            ttk.Label(self.pframe, text="(no parameters)", foreground="gray").grid(row=0, column=0)

    def parse(self) -> dict:
        if not self.action.get():
            raise ValueError("Choose an action first.")
        spec = {"action": self.action.get()}
        for pname, (p, w) in self.fields.items():
            raw = (w.get("1.0", "end") if isinstance(w, tk.Text) else w.get()).strip()
            if not raw:
                if p.default is p.empty:
                    raise ValueError(f"'{pname}' is required.")
                continue
            if p.annotation is str:
                spec[pname] = raw
            else:
                try:
                    spec[pname] = yaml.safe_load(raw)
                except yaml.YAMLError as ex:
                    raise ValueError(f"'{pname}' is not valid YAML: {ex}")
        if self.repeat.get():
            spec["repeat"] = True
        return spec

    def on_save(self):
        try:
            self.result = self.parse()
        except ValueError as ex:
            messagebox.showerror("Invalid binding", str(ex), parent=self)
            return
        self.destroy()

    def on_clear(self):
        self.result = "clear"
        self.destroy()


# ---------------------------------------------------------------- main window

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("macrokb")
        self.dirty = False
        self.backed_up = False
        self.cfg = self.load()
        self.keybtns = {}
        self.proc = None            # daemon process started from this UI
        self.logf = None
        self.stopping = False
        self.safe = tk.BooleanVar(value=True)

        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Profile").pack(side="left")
        self.profile = tk.StringVar()
        self.pcombo = ttk.Combobox(top, textvariable=self.profile, state="readonly", width=18)
        self.pcombo.pack(side="left", padx=6)
        self.pcombo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Button(top, text="+", width=3, command=self.new_profile).pack(side="left")
        ttk.Button(top, text="Keyboard…", command=self.pick_keyboard).pack(side="left", padx=6)

        self.devlabel = ttk.Label(self, foreground="gray", padding=(10, 2))
        self.devlabel.grid(row=1, column=0, sticky="w")

        pad = ttk.Frame(self, padding=10)
        pad.grid(row=2, column=0)
        for c in range(4):
            pad.columnconfigure(c, weight=1, uniform="col")
        for key, label, r, c, rs, cs in LAYOUT:
            b = tk.Button(pad, width=11, height=3, wraplength=110,
                          command=lambda k=key: self.edit(k))
            b.grid(row=r, column=c, rowspan=rs, columnspan=cs, sticky="nsew", padx=3, pady=3)
            self.keybtns[key] = (b, label)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.grid(row=3, column=0, sticky="ew")
        self.status = ttk.Label(bottom, text="", foreground="gray")
        self.status.pack(side="left")
        ttk.Button(bottom, text="Save", command=self.save).pack(side="right")
        ttk.Button(bottom, text="Save & apply to daemon", command=self.apply).pack(side="right", padx=6)

        dbar = ttk.Frame(self, padding=(10, 0, 10, 10))
        dbar.grid(row=4, column=0, sticky="ew")
        self.dstatus = ttk.Label(dbar, text="Daemon: stopped")
        self.dstatus.pack(side="left")
        self.dbtn = ttk.Button(dbar, text="Start daemon", command=self.toggle_daemon)
        self.dbtn.pack(side="right")
        ttk.Button(dbar, text="Restart", command=self.restart_daemon).pack(side="right", padx=6)
        ttk.Checkbutton(dbar, text="Auto-release after 60s (safe test)",
                        variable=self.safe).pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()
        self.poll_daemon()

    # -- config
    def load(self) -> dict:
        try:
            with open(CONFIG) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            cfg = {}
        except (OSError, yaml.YAMLError) as ex:
            messagebox.showerror("Config error", f"Could not read {CONFIG}:\n{ex}")
            raise SystemExit(1)
        cfg.setdefault("profiles", {})
        if not cfg["profiles"]:
            cfg["profiles"]["default"] = {"device": {"name": "CHANGE ME"}, "bindings": {}}
        return cfg

    def current(self) -> dict:
        prof = self.cfg["profiles"][self.profile.get()]
        if not isinstance(prof.get("bindings"), dict):
            prof["bindings"] = {}
        return prof

    def set_dirty(self, d=True):
        self.dirty = d
        self.title("macrokb" + (" *" if d else ""))

    # -- view
    def refresh(self):
        names = list(self.cfg["profiles"])
        self.pcombo["values"] = names
        if self.profile.get() not in names:
            self.profile.set(names[0])
        prof = self.current()
        bindings = prof["bindings"]
        for key, (b, label) in self.keybtns.items():
            spec = bindings.get(key)
            text = f"{label}\n{summarize(spec)}" if spec else label
            if spec:
                b.config(text=text, bg="#cfe8ff", fg="black", activebackground="#b5d9fb")
            else:
                b.config(text=text, bg=self.cget("bg"), fg="red", activebackground="#b5d9fb")
        dev = prof.get("device") or {}
        self.devlabel.config(text="Keyboard: " + (", ".join(f"{k}={v}" for k, v in dev.items()) or "not set"))

    # -- actions
    def edit(self, key):
        bindings = self.current()["bindings"]
        dlg = EditDialog(self, key, bindings.get(key))
        self.wait_window(dlg)
        if dlg.result is None:
            return
        if dlg.result == "clear":
            bindings.pop(key, None)
        else:
            bindings[key] = dlg.result
        self.set_dirty()
        self.refresh()

    def new_profile(self):
        name = simpledialog.askstring("New profile", "Profile name:", parent=self)
        if not name or name in self.cfg["profiles"]:
            return
        self.cfg["profiles"][name] = {"device": {"name": "CHANGE ME"}, "bindings": {}}
        self.profile.set(name)
        self.set_dirty()
        self.refresh()

    def pick_keyboard(self):
        found = []
        for dev in macrokb.list_keyboards():
            found.append((dev.name, dev.info.vendor, dev.info.product, dev.phys or "",
                          macrokb.is_builtin(dev)))
            dev.close()
        if not found:
            messagebox.showwarning(
                "No keyboards", "No readable keyboards found.\n"
                "Make sure you are in the 'input' group (log out/in after adding).")
            return
        win = tk.Toplevel(self)
        win.title("Choose keyboard")
        win.transient(self)
        lb = tk.Listbox(win, width=70, height=min(12, len(found)), exportselection=False)
        for name, v, p, phys, builtin in found:
            lb.insert("end", f"{name}   [{v:04x}:{p:04x}]   {phys}" + ("   (built-in)" if builtin else ""))
        lb.pack(padx=10, pady=10)

        def choose():
            sel = lb.curselection()
            if not sel:
                return
            name, v, p, _phys, _b = found[sel[0]]
            self.current()["device"] = {"name": name, "vendor": v, "product": p}
            self.set_dirty()
            self.refresh()
            win.destroy()

        ttk.Button(win, text="Use this keyboard", command=choose).pack(pady=(0, 10))

    def save(self) -> bool:
        try:
            if CONFIG.exists() and not self.backed_up:
                shutil.copy(CONFIG, CONFIG.with_name("config.yaml.bak"))
                self.backed_up = True
            with open(CONFIG, "w") as f:
                f.write("# Managed by macrokb_ui.py (comments are not preserved; backup: config.yaml.bak)\n")
                yaml.safe_dump(self.cfg, f, sort_keys=False, allow_unicode=True)
        except OSError as ex:
            messagebox.showerror("Save failed", str(ex))
            return False
        self.set_dirty(False)
        self.status.config(text="Saved.")
        return True

    def apply(self):
        if not self.save():
            return
        r = subprocess.run(["pkill", "-HUP", "-f", r"Macrokb\.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.status.config(text="Saved. Daemon reloaded." if r.returncode == 0
                           else "Saved. No running daemon found (use Start daemon).")

    # -- daemon control
    def daemon_running(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        r = subprocess.run(["pgrep", "-f", r"Macrokb\.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0    # also catches a daemon started from a terminal

    def start_daemon(self):
        if self.daemon_running():
            return
        if self.dirty and not self.save():
            return
        cmd = [sys.executable, str(DAEMON)]
        if self.safe.get():
            cmd += ["--timeout", "60"]
        try:
            self.logf = open(LOG, "w")
            self.proc = subprocess.Popen(cmd, stdout=self.logf, stderr=subprocess.STDOUT,
                                         cwd=DAEMON.parent)
        except OSError as ex:
            messagebox.showerror("Could not start daemon", str(ex))
            return
        self.stopping = False
        self.status.config(text=f"Daemon started (log: {LOG.name}).")

    def stop_daemon(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()   # SIGTERM -> daemon releases the keyboards cleanly
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        else:                       # started elsewhere (e.g. a terminal)
            subprocess.run(["pkill", "-TERM", "-f", r"Macrokb\.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_daemon(self):
        self.stop_daemon() if self.daemon_running() else self.start_daemon()

    def restart_daemon(self):
        if self.daemon_running():
            self.stop_daemon()
        self.after(500, self.start_daemon)

    def log_tail(self, n=8) -> str:
        try:
            return "".join(LOG.read_text().splitlines(True)[-n:])
        except OSError:
            return ""

    def poll_daemon(self):
        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            if self.logf:
                self.logf.close()
                self.logf = None
            if code != 0 and not self.stopping:
                messagebox.showerror("Daemon stopped",
                                     f"Exit code {code}.\n\n" + self.log_tail())
            self.stopping = False
        running = self.daemon_running()
        self.dstatus.config(text="Daemon: running" if running else "Daemon: stopped")
        self.dbtn.config(text="Stop daemon" if running else "Start daemon")
        self.after(1000, self.poll_daemon)

    def on_close(self):
        if self.dirty:
            ans = messagebox.askyesnocancel("Unsaved changes", "Save before closing?")
            if ans is None:
                return
            if ans and not self.save():
                return
        if self.proc and self.proc.poll() is None:
            self.stop_daemon()      # release the keyboards before exiting
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
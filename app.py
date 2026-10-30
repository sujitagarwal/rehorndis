#!/usr/bin/env python3
"""ReHoRNDIS menu bar app. Runs as you; only the tunnel worker (main.py --worker) runs as root."""
import fcntl
import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from pathlib import Path

import AppKit
import objc
import rumps

import control
import usbio

HERE = Path(__file__).resolve().parent
HOME_DIR = Path(os.environ.get("REHORNDIS_HOME", Path.home() / ".rehorndis"))
CONFIG = HOME_DIR / "config.json"
LABEL = "com.sujitagarwal.rehorndis"
AGENT = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = "/var/log/rehorndis.log"
LIBS = "/opt/homebrew/lib:/usr/local/lib"

START_TIMEOUT = 40  # seconds from admin approval to the worker's control socket appearing
MENU_WIDTH = 260


def load_cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg))


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def applescript_quote(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def enable_login():
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    AGENT.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(HERE / "app.py")],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after Quit
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1", "DYLD_FALLBACK_LIBRARY_PATH": LIBS,
                                 "REHORNDIS_HOME": str(HOME_DIR)},
        "StandardOutPath": str(HOME_DIR / "app.log"),
        "StandardErrorPath": str(HOME_DIR / "app.log"),
    }))


def disable_login():
    AGENT.unlink(missing_ok=True)


class SwitchTarget(AppKit.NSObject):
    """Receives the NSSwitch action and forwards the new on/off value to Python."""

    def initWithCallback_(self, callback):
        self = objc.super(SwitchTarget, self).init()
        self.callback = callback
        return self

    def toggle_(self, sender):
        self.callback(bool(sender.state()))


class PowerSwitch:
    """A menu row like the Wi-Fi one: bold title on the left, native switch on the right."""

    def __init__(self, title, callback):
        self.target = SwitchTarget.alloc().initWithCallback_(callback)  # keep a reference alive
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (MENU_WIDTH, 30)))
        label = AppKit.NSTextField.labelWithString_(title)
        label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
        label.setFrame_(((14, 6), (MENU_WIDTH - 80, 18)))
        self.switch = AppKit.NSSwitch.alloc().init()
        self.switch.setControlSize_(AppKit.NSControlSizeSmall)
        self.switch.sizeToFit()
        w, h = self.switch.frame().size
        self.switch.setFrameOrigin_((MENU_WIDTH - 14 - w, (30 - h) / 2))
        self.switch.setTarget_(self.target)
        self.switch.setAction_("toggle:")
        view.addSubview_(label)
        view.addSubview_(self.switch)
        self.item = rumps.MenuItem(title, callback=lambda _: None)  # keeps the row enabled
        self.item._menuitem.setView_(view)

    def set(self, on, enabled):
        self.switch.setState_(1 if on else 0)
        self.switch.setEnabled_(bool(enabled))


class App(rumps.App):
    def __init__(self):
        super().__init__("ReHoRNDIS", title="⚪ USB", quit_button=None)
        self.uid = os.getuid()
        self.cfg = load_cfg()
        self.power = PowerSwitch("ReHoRNDIS", self.on_switch)
        self.i_status = rumps.MenuItem("Starting…")
        self.i_info = rumps.MenuItem("")
        self.i_phones = rumps.MenuItem("Phones")
        self.i_fix = rumps.MenuItem("Troubleshoot…", callback=self.on_troubleshoot)
        self.i_login = rumps.MenuItem("Launch at login", callback=self.on_login)
        self.i_auto = rumps.MenuItem("Connect when phone is plugged in", callback=self.on_auto)
        self.menu = [self.power.item, self.i_status, self.i_info, None, self.i_phones, None,
                     self.i_fix, self.i_login, self.i_auto, None,
                     rumps.MenuItem("Quit ReHoRNDIS", callback=self.on_quit)]
        self.i_phones.set_callback(None)  # section header, not clickable
        self.i_login.state = AGENT.exists()
        self.i_auto.state = bool(self.cfg.get("auto"))
        self._phones = []          # last scan: [{bus, address, vid, pid, name}]
        self._shown = None         # what the phone rows currently display
        self._row_titles = []
        self._sel = None           # (bus, address) of the chosen phone
        self._proc = None          # osascript process while waiting for admin approval
        self._deadline = 0         # worker must show its socket before this
        self._fail = None          # last start/worker failure message
        self._auto_tried = False   # auto-connect once per plug-in
        self._st = None            # last status from the worker
        self._fix = None           # osascript process running the repair
        self._timer = rumps.Timer(self.poll, 2)
        self._timer.start()
        self.poll(None)

    # ---- phone list ----
    def _selected_phone(self):
        for p in self._phones:
            if (p["bus"], p["address"]) == self._sel:
                return p
        return None

    def _render_phones(self, locked):
        """Rebuild the phone rows only when something visible changed."""
        view = (tuple((p["bus"], p["address"], p["name"]) for p in self._phones), self._sel, locked)
        if view == self._shown:
            return
        self._shown = view
        for t in self._row_titles:
            del self.menu[t]
        self._row_titles = []
        anchor = "Phones"
        names = [p["name"] for p in self._phones]
        for p in self._phones or [None]:
            if p is None:
                title, cb, mark = "No phone — plug in, enable USB tethering", None, False
            else:
                title = p["name"] if names.count(p["name"]) == 1 else f"{p['name']} ({p['bus']}.{p['address']})"
                key = (p["bus"], p["address"])
                cb = None if locked else (lambda _s, k=key: self.on_select(k))
                mark = key == self._sel
            row = rumps.MenuItem(title, callback=cb)
            row.state = 1 if mark else 0
            if cb is None:
                row.set_callback(None)
            self.menu.insert_after(anchor, row)
            self._row_titles.append(row.title)
            anchor = row.title

    def on_select(self, key):
        self._sel = key
        self._shown = None
        self.poll(None)

    # ---- state ----
    def poll(self, _):
        st = control.request(self.uid, "status")
        self._st = st
        if self._proc is not None and self._proc.poll() is not None:
            rc, err = self._proc.returncode, self._proc.stderr.read()
            self._proc = None
            if rc != 0:
                self._deadline = 0
                self._fail = "Cancelled" if "-128" in err else (err.strip().splitlines() or ["start failed"])[-1][:90]
        if st is None and self._proc is None and self._deadline and time.time() > self._deadline:
            self._deadline, self._fail = 0, f"Worker did not start (see {LOG})"
        if st:
            self._deadline = 0
            if st.get("state") == "error":
                self._fail = st.get("message", "error")
            elif st.get("state") == "connected":
                self._fail = None
        if self._fix is not None and self._fix.poll() is not None:
            self.finish_troubleshoot()

        if st is None and self._fix is None:  # while connected the device is busy: keep the last list
            try:
                self._phones = usbio.list_rndis_devices()
            except Exception:
                self._phones = []
        if st and st.get("phone"):  # the worker knows which phone it holds, even when we can't scan
            self._phones = [st["phone"]]
            self._sel = (st["phone"]["bus"], st["phone"]["address"])
        keys = [(p["bus"], p["address"]) for p in self._phones]
        if self._sel not in keys:
            self._sel = keys[0] if keys else None
        phone = bool(st) or self._selected_phone() is not None
        if not self._phones:
            self._auto_tried, self._fail = False, None
        busy = st is not None or self._proc is not None or bool(self._deadline) or self._fix is not None

        if self._fix is not None:
            line, title = "Troubleshooting…", "🟡 USB"
        elif st:
            state = st.get("state")
            if state == "connected":
                line, title = f"Connected — {st['ip']}", "🟢 USB"
                self.i_info.title = (f"{st['iface']} via {st['gateway']}   ↓ {human(st.get('rx_bytes', 0))}"
                                     f"  ↑ {human(st.get('tx_bytes', 0))}")
            elif state == "error":
                line, title = f"Error: {st.get('message', '')[:80]}", "🔴 USB"
            elif state == "stopping":
                line, title = "Turning off…", "🟡 USB"
            else:
                line, title = f"Connecting… {st.get('detail', '')}", "🟡 USB"
        elif self._proc is not None:
            line, title = "Waiting for administrator approval…", "🟡 USB"
        elif self._deadline:
            line, title = "Starting…", "🟡 USB"
        elif self._fail:
            line, title = f"Failed: {self._fail}", "🔴 USB"
        elif phone:
            line, title = "Off — ready", "⚪ USB"
        else:
            line, title = "Off — no phone", "⚪ USB"
        self.i_status.title, self.title = line, title
        if not st or st.get("state") != "connected":
            self.i_info.title = ""

        on = bool(st) and st.get("state") != "stopping" or self._proc is not None or bool(self._deadline)
        self.power.set(on, enabled=bool(st) and st.get("state") != "stopping" or (phone and not busy))
        self._render_phones(locked=busy)
        self.i_fix.set_callback(self.on_troubleshoot if self._fix is None and self._proc is None else None)

        if self.cfg.get("auto") and phone and not busy and not self._auto_tried and not self._fail:
            self._auto_tried = True
            self.start_worker()

    # ---- actions ----
    def on_switch(self, on):
        if on and self._st is None and self._selected_phone():
            self.start_worker()
        elif not on and self._st:
            control.request(self.uid, "stop")
            self._auto_tried = True  # don't auto-reconnect right after a manual off
        self.poll(None)

    def start_worker(self):
        p = self._selected_phone()
        pick = f"--bus {p['bus']} --address {p['address']} " if p else ""
        cmd = (f"env PYTHONDONTWRITEBYTECODE=1 DYLD_FALLBACK_LIBRARY_PATH={LIBS} {shlex.quote(sys.executable)} "
               f"{shlex.quote(str(HERE / 'main.py'))} --worker --control-uid {self.uid} {pick}>> {LOG} 2>&1 &")
        script = (f'do shell script "{applescript_quote(cmd)}" with administrator privileges '
                  'with prompt "ReHoRNDIS needs administrator access to create the USB network interface."')
        self._fail = None
        self._deadline = time.time() + START_TIMEOUT
        self._proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)

    def on_troubleshoot(self, _):
        cmd = (f"env PYTHONDONTWRITEBYTECODE=1 DYLD_FALLBACK_LIBRARY_PATH={LIBS} {shlex.quote(sys.executable)} "
               f"{shlex.quote(str(HERE / 'main.py'))} --repair 2>&1")
        script = (f'do shell script "{applescript_quote(cmd)}" with administrator privileges '
                  'with prompt "ReHoRNDIS needs administrator access to clean up leftover network settings."')
        self._fail = None
        self._fix = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)

    def finish_troubleshoot(self):
        proc, self._fix = self._fix, None
        out, err = proc.stdout.read().strip(), proc.stderr.read()
        if proc.returncode != 0:
            if "-128" in err:
                return  # user cancelled the password dialog
            out = f"The repair could not run:\n{err.strip()[-300:]}"
        report = [out or "Nothing to do."]
        try:  # re-enumerate the phone so the next start begins from a clean USB state
            dev = usbio.find_rndis_device()
            if dev is None:
                report.append("\nNo RNDIS phone found: plug it in and enable USB tethering.")
            else:
                dev.reset()
                report.append("\nReset the phone's USB connection.")
        except Exception as e:  # reset is best effort
            report.append(f"\nCould not reset USB: {e}")
        self._auto_tried = False
        self._shown = None
        rumps.alert("Troubleshoot", "\n".join(report))

    def on_auto(self, item):
        item.state = not item.state
        self.cfg["auto"] = bool(item.state)
        save_cfg(self.cfg)

    def on_login(self, item):
        disable_login() if AGENT.exists() else enable_login()
        item.state = AGENT.exists()

    def on_quit(self, _):
        if self._st:
            control.request(self.uid, "stop")  # worker restores routes/DNS on its own
        rumps.quit_application()


def main():
    if "--install-login" in sys.argv:
        return enable_login()
    if "--remove-login" in sys.argv:
        return disable_login()
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(HOME_DIR / "app.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("ReHoRNDIS menu bar app is already running.")
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory)  # menu bar only, no Dock icon
    App().run()


if __name__ == "__main__":
    main()

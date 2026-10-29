#!/usr/bin/env python3
"""macrokb: turn specific keyboards into macro keyboards on Linux.

  python macrokb.py --list              show keyboards you can match
  python macrokb.py --actions           show available actions
  python macrokb.py --dry-run           watch events, do NOT grab or execute anything
  python macrokb.py                     run for real
  python macrokb.py --timeout 60        run for real, auto-release after 60s (safe testing)

Send SIGHUP to reload the config:  kill -HUP <pid>
"""
import argparse
import asyncio
import logging
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

import evdev
import yaml
from evdev import ecodes as e, InputDevice, UInput

import Actions
from Actions import Context, run_action

log = logging.getLogger("macrokb")
VIRTUAL_NAME = "macrokb-virtual"


# ---------------------------------------------------------------- devices

def list_keyboards():
    """All real keyboard-like input devices (excludes our own virtual one)."""
    paths = evdev.list_devices()
    if not paths:
        log.error("No /dev/input/event* devices found at all.")
    for path in paths:
        try:
            dev = InputDevice(path)
        except OSError as ex:
            log.warning("Cannot open %s: %s", path, ex)
            if isinstance(ex, PermissionError):
                log.warning("Permission denied: add yourself to the 'input' group and "
                            "log out/in, or test with sudo.")
            continue
        keys = dev.capabilities().get(e.EV_KEY, [])
        if dev.name != VIRTUAL_NAME and (e.KEY_A in keys or e.KEY_KP1 in keys or e.KEY_1 in keys):
            yield dev
        else:
            dev.close()


def matches(dev: InputDevice, m: dict) -> bool:
    """Match by any of: name (substring), vendor, product, phys (substring), uniq."""
    if not m:
        return False
    if "name" in m and str(m["name"]).lower() not in dev.name.lower():
        return False
    if "vendor" in m and dev.info.vendor != m["vendor"]:
        return False
    if "product" in m and dev.info.product != m["product"]:
        return False
    if "phys" in m and str(m["phys"]) not in (dev.phys or ""):
        return False
    if "uniq" in m and m["uniq"] != dev.uniq:
        return False
    return True


def is_builtin(dev: InputDevice) -> bool:
    # The laptop's own keyboard sits on the i8042 / "isa" bus.
    return (dev.phys or "").startswith("isa")


def key_names(code: int) -> list[str]:
    n = e.KEY.get(code) or e.BTN.get(code)
    if n is None:
        return []
    return n if isinstance(n, list) else [n]


@dataclass
class DeviceState:
    dev: InputDevice
    profile: str
    swallowed: set = field(default_factory=set)  # keys whose press we consumed


# ---------------------------------------------------------------- daemon

class Daemon:
    def __init__(self, cfg_path: Path, dry_run=False, timeout=None, allow_builtin=False):
        self.cfg_path = cfg_path
        self.dry_run = dry_run
        self.timeout = timeout
        self.allow_builtin = allow_builtin
        self.cfg: dict = {}
        self.ui: UInput | None = None
        self.states: list[DeviceState] = []
        self.stop = asyncio.Event()

    # -- config
    def load_config(self):
        try:
            with open(self.cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as ex:
            log.error("Could not load config %s: %s", self.cfg_path, ex)
            return
        self.cfg = cfg
        # keep each device on its profile if it still exists
        for st in self.states:
            if st.profile not in cfg.get("profiles", {}):
                log.warning("Profile %r vanished, device %s unbound", st.profile, st.dev.name)
        log.info("Config loaded: profiles = %s", list(cfg.get("profiles", {})))

    def profile_cfg(self, name: str) -> dict:
        return self.cfg.get("profiles", {}).get(name, {})

    def switch_profile(self, state: DeviceState, name: str):
        if name not in self.cfg.get("profiles", {}):
            log.error("switch_profile: unknown profile %r", name)
            return
        state.profile = name
        log.info("%s -> profile %r", state.dev.name, name)

    # -- setup
    def find_devices(self) -> list[DeviceState]:
        states = []
        profiles = self.cfg.get("profiles", {})
        for dev in list_keyboards():
            for pname, p in profiles.items():
                if matches(dev, p.get("device", {})):
                    if is_builtin(dev) and not self.allow_builtin:
                        log.error("Refusing to use built-in keyboard %r (use --allow-builtin)", dev.name)
                        break
                    states.append(DeviceState(dev, pname))
                    log.info("Matched %r (%s) -> profile %r", dev.name, dev.path, pname)
                    break
            else:
                dev.close()
        return states

    # -- event handling
    async def handle(self, st: DeviceState, ev):
        code, value = ev.code, ev.value  # value: 1 press, 0 release, 2 autorepeat
        profile = self.profile_cfg(st.profile)
        bindings = profile.get("bindings", {}) or {}
        spec = next((bindings[n] for n in key_names(code) if n in bindings), None)

        if spec is not None:
            if isinstance(spec, str):
                spec = {"action": spec}
            if value == 1 or (value == 2 and spec.get("repeat")):
                if self.dry_run:
                    log.info("[dry-run] %s -> %s", key_names(code), spec)
                else:
                    asyncio.create_task(run_action(Context(self.ui, self, st), spec))
            if value == 1:
                st.swallowed.add(code)
            elif value == 0:
                st.swallowed.discard(code)
            return  # never forward bound keys

        if value == 1 and self.dry_run:
            log.info("[dry-run] unbound key %s", key_names(code))

        # Unbound key: forward it so the keyboard still types normally
        if code in st.swallowed:           # release of a key whose press we ate
            st.swallowed.discard(code)
            return
        passthrough = profile.get("passthrough_unbound", self.cfg.get("passthrough_unbound", True))
        if passthrough and not self.dry_run:
            self.ui.write(e.EV_KEY, code, value)
            self.ui.syn()

    async def pump(self, st: DeviceState):
        try:
            async for ev in st.dev.async_read_loop():
                if ev.type == e.EV_KEY:
                    await self.handle(st, ev)
        except OSError:
            log.warning("Device %s disconnected", st.dev.name)

    # -- main loop
    async def run(self):
        self.load_config()
        self.states = self.find_devices()
        if not self.states:
            log.error("No keyboard matched any profile. Run with --list and edit the config.")
            return 1

        self.ui = UInput(name=VIRTUAL_NAME)  # default capabilities = all keys
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self.load_config)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)

        grabbed = []
        try:
            if not self.dry_run:
                for st in self.states:
                    try:
                        st.dev.grab()
                        grabbed.append(st)
                    except OSError as ex:
                        log.error("Could not grab %s: %s", st.dev.name, ex)
                log.info("Grabbed %d device(s). Ctrl+C to quit.", len(grabbed))
            tasks = [asyncio.create_task(self.pump(st)) for st in self.states]
            try:
                await asyncio.wait_for(self.stop.wait(), self.timeout)
            except asyncio.TimeoutError:
                log.info("Timeout reached, releasing keyboards.")
            for t in tasks:
                t.cancel()
        finally:
            for st in grabbed:
                try:
                    st.dev.ungrab()
                except OSError:
                    pass
            for st in self.states:
                st.dev.close()
            self.ui.close()
        return 0


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    ap.add_argument("--list", action="store_true", help="list keyboards and exit")
    ap.add_argument("--actions", action="store_true", help="list actions and exit")
    ap.add_argument("--dry-run", action="store_true", help="don't grab devices or run actions")
    ap.add_argument("--timeout", type=float, help="auto-release keyboards after N seconds")
    ap.add_argument("--allow-builtin", action="store_true", help="allow grabbing the built-in keyboard")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.list:
        for dev in list_keyboards():
            tag = "  [built-in]" if is_builtin(dev) else ""
            print(f"{dev.path}  name={dev.name!r}  vendor=0x{dev.info.vendor:04x} "
                  f"product=0x{dev.info.product:04x}  phys={dev.phys!r}{tag}")
        return 0
    if args.actions:
        for name, doc in actions.describe_actions().items():
            print(f"{name:16} {doc}")
        return 0

    daemon = Daemon(args.config, args.dry_run, args.timeout, args.allow_builtin)
    return asyncio.run(daemon.run())


if __name__ == "__main__":
    sys.exit(main())
"""Action registry: every function a key can trigger.

An action is an async function `fn(ctx, **params)` registered with @action("name").
Config bindings look like:  {action: send_keys, keys: "ctrl+shift+c"}
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from evdev import ecodes as e

log = logging.getLogger("macrokb.actions")

ACTIONS: dict[str, Any] = {}


def action(name):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


@dataclass
class Context:
    """What an action gets access to."""
    ui: Any        # evdev.UInput used to inject keys
    daemon: Any    # the Daemon (for switch_profile etc.)
    state: Any     # DeviceState of the keyboard that fired the key


# ---------------------------------------------------------------- key parsing

ALIASES = {
    "ctrl": "LEFTCTRL", "control": "LEFTCTRL", "shift": "LEFTSHIFT",
    "alt": "LEFTALT", "altgr": "RIGHTALT", "super": "LEFTMETA",
    "meta": "LEFTMETA", "win": "LEFTMETA", "esc": "ESC", "escape": "ESC",
    "enter": "ENTER", "return": "ENTER", "del": "DELETE", "pgup": "PAGEUP",
    "pgdn": "PAGEDOWN", "minus": "MINUS", "plus": "KPPLUS", "equal": "EQUAL",
    "comma": "COMMA", "dot": "DOT", "slash": "SLASH",
}


def resolve_key(name: str) -> int:
    n = name.strip()
    if n.upper().startswith("KEY_"):
        n = n[4:]
    n = ALIASES.get(n.lower(), n.upper())
    code = e.ecodes.get("KEY_" + n)
    if code is None:
        raise ValueError(f"Unknown key: {name!r}")
    return code


def parse_combo(combo: str) -> list[int]:
    return [resolve_key(k) for k in combo.split("+")]


async def tap_combo(ui, combo: str, hold: float = 0.01):
    """Press all keys in order, then release in reverse order."""
    codes = parse_combo(combo)
    for c in codes:
        ui.write(e.EV_KEY, c, 1)
    ui.syn()
    await asyncio.sleep(hold)
    for c in reversed(codes):
        ui.write(e.EV_KEY, c, 0)
    ui.syn()


# ---------------------------------------------------------------- core actions

@action("send_keys")
async def send_keys(ctx, keys: str):
    """Send a key combo, e.g. 'ctrl+shift+c'."""
    await tap_combo(ctx.ui, keys)


@action("run_command")
async def run_command(ctx, cmd: str):
    """Run a shell command (fire and forget)."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    asyncio.create_task(proc.wait())  # reap it so it doesn't become a zombie


@action("open_url")
async def open_url(ctx, url: str):
    """Open a URL with the default browser."""
    proc = await asyncio.create_subprocess_exec(
        "xdg-open", url,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    asyncio.create_task(proc.wait())


@action("delay")
async def delay(ctx, ms: int):
    """Wait (only useful inside a sequence)."""
    await asyncio.sleep(ms / 1000)


@action("sequence")
async def sequence(ctx, steps: list):
    """Run a list of actions one after another."""
    for step in steps:
        await run_action(ctx, step)


@action("switch_profile")
async def switch_profile(ctx, profile: str):
    """Switch the keyboard that pressed this key to another profile."""
    ctx.daemon.switch_profile(ctx.state, profile)


MEDIA_KEYS = {
    "play_pause": "KEY_PLAYPAUSE", "next": "KEY_NEXTSONG",
    "prev": "KEY_PREVIOUSSONG", "stop": "KEY_STOPCD", "mute": "KEY_MUTE",
    "vol_up": "KEY_VOLUMEUP", "vol_down": "KEY_VOLUMEDOWN",
}


@action("media_control")
async def media_control(ctx, command: str):
    """command: play_pause | next | prev | stop | mute | vol_up | vol_down"""
    await tap_combo(ctx.ui, MEDIA_KEYS[command])


# ---------------------------------------------------------------- presets
# One-word shortcuts, usable as {action: terminal_copy}

PRESETS = {
    "copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x",
    "undo": "ctrl+z", "redo": "ctrl+shift+z", "select_all": "ctrl+a",
    "terminal_copy": "ctrl+shift+c", "terminal_paste": "ctrl+shift+v",
    "back": "alt+left", "forward": "alt+right",
    "new_tab": "ctrl+t", "close_tab": "ctrl+w", "reopen_tab": "ctrl+shift+t",
    "next_tab": "ctrl+tab", "prev_tab": "ctrl+shift+tab", "reload": "ctrl+r",
    "screenshot": "print", "lock_screen": "super+l",
}


def _make_preset(combo):
    async def preset(ctx):
        await tap_combo(ctx.ui, combo)
    preset.__doc__ = f"Sends {combo}"
    return preset


for _name, _combo in PRESETS.items():
    ACTIONS[_name] = _make_preset(_combo)


# ---------------------------------------------------------------- dispatcher

async def run_action(ctx, spec):
    """spec is a dict like {action: name, ...params} or just a name string."""
    if isinstance(spec, str):
        spec = {"action": spec}
    name = spec.get("action")
    fn = ACTIONS.get(name)
    if fn is None:
        log.error("Unknown action %r", name)
        return
    params = {k: v for k, v in spec.items() if k not in ("action", "repeat")}
    try:
        await fn(ctx, **params)
    except Exception:
        log.exception("Action %r failed", name)


def describe_actions() -> dict[str, str]:
    """Name -> description. The future UI can serve this from an API endpoint."""
    return {n: (f.__doc__ or "").strip() for n, f in sorted(ACTIONS.items())}
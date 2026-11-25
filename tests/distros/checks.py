#!/usr/bin/env python3
"""Checks run inside a distro container, as a normal user with a real systemd
user manager and session bus, after the app was installed (script or package).

Everything goes through the installed services, the way a user runs them:
the daemon publishes a fake game to a fake Discord socket, the tray registers
with a fake StatusNotifier host (the protocol Plasma uses), its menu is read
and clicked over dbusmenu, and the pause it sets reaches the daemon.

Prints one line per check:  RESULT|<name>|ok|<detail>  or  RESULT|<name>|fail|<detail>
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time

SRC = pathlib.Path(__file__).resolve().parent.parent.parent
HOME = pathlib.Path.home()
RUN = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
CONFIG = HOME / ".config" / "discord-game-presence.json"
STATUS = RUN / "discord-game-presence" / "status.json"
CLIENT = "111111111111111111"
EXE = r"Z:\Games\Jogo Falso\fakegame.exe"
KIND = sys.argv[1] if len(sys.argv) > 1 else "script"   # script | package
FAILED = []


def result(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    detail = str(detail).replace("\n", " ")[:300]
    print(f"RESULT|{name}|{'ok' if ok else 'fail'}|{detail}", flush=True)


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def user(*args):
    return sh("systemctl", "--user", *args)


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def wait(test, timeout, step=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = test()
        if value:
            return value
        time.sleep(step)
    return None


def status():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


# ----------------------------------------------------------------- daemon
def check_daemon():
    daemon = shutil.which("discord-game-presence") or str(HOME / ".local/bin/discord-game-presence.py")
    out = sh(daemon, "--version")
    result("daemon runs with this python", out.returncode == 0, out.stdout.strip() or out.stderr)
    result("ss (iproute2) present", bool(shutil.which("ss")))
    result("busctl present", bool(shutil.which("busctl")))

    # a fake Discord on the socket the daemon looks for, like Vesktop's
    fake = load("fake", SRC / "tests" / "e2e_fake_discord.py")
    discord = fake.FakeDiscord(str(RUN / "discord-ipc-0"))
    game = subprocess.Popen(["bash", "-c", f"exec -a '{EXE}' sleep 300"])
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"games": {"fakegame.exe": {
        "client_id": CLIENT, "details": "Jogo Falso", "state": "Jogando",
        "large_text": "Jogo Falso"}}}))
    restarted = user("restart", "discord-game-presence.service")
    active = wait(lambda: user("is-active", "discord-game-presence.service").stdout.strip() == "active", 10)
    result("user service starts (systemctl --user)", restarted.returncode == 0 and bool(active),
           restarted.stderr.strip())
    shown = discord.wait(lambda e: e[1] == CLIENT and e[2], 25)
    result("publishes a running game to Discord", bool(shown),
           (shown[2].get("details"), shown[2].get("state")) if shown else "nothing published")
    showing = wait(lambda: status().get("showing", {}).get("fakegame.exe"), 5)
    result("status file says what is published", bool(showing), showing)

    killed = time.time()
    game.kill()
    game.wait()
    cleared = discord.wait(lambda e: e[1] == CLIENT and e[2] is None and e[0] > killed, 15)
    result("clears the presence when the game closes", bool(cleared),
           f"{cleared[0] - killed:.1f}s" if cleared else "never cleared")
    return discord


# ------------------------------------------------------------------- tray
WATCHER_XML = """<node><interface name='org.kde.StatusNotifierWatcher'>
<method name='RegisterStatusNotifierItem'><arg type='s' direction='in'/></method>
<method name='RegisterStatusNotifierHost'><arg type='s' direction='in'/></method>
<property name='RegisteredStatusNotifierItems' type='as' access='read'/>
<property name='IsStatusNotifierHostRegistered' type='b' access='read'/>
<property name='ProtocolVersion' type='i' access='read'/>
<signal name='StatusNotifierItemRegistered'><arg type='s'/></signal>
</interface></node>"""


class FakeHost:
    """The StatusNotifier side of a panel: what Plasma does with a tray icon."""

    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.items = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        node = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML)
        self.bus.register_object("/StatusNotifierWatcher", node.interfaces[0],
                                 self._call, self._prop, None)
        Gio.bus_own_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher", 0, None, None)
        self.loop = GLib.MainLoop()
        threading.Thread(target=self.loop.run, daemon=True).start()

    def _call(self, _conn, sender, _path, _iface, method, params, invocation):
        if method == "RegisterStatusNotifierItem":
            service = params.unpack()[0]
            if service.startswith("/"):
                self.items.append((sender, service))
            else:
                self.items.append((service, "/StatusNotifierItem"))
        invocation.return_value(None)

    def _prop(self, _conn, _sender, _path, _iface, name):
        GLib = self.GLib
        return {"RegisteredStatusNotifierItems": GLib.Variant("as", [i[0] for i in self.items]),
                "IsStatusNotifierHostRegistered": GLib.Variant("b", True),
                "ProtocolVersion": GLib.Variant("i", 0)}[name]

    def call(self, name, path, iface, method, signature, args):
        return self.bus.call_sync(name, path, iface, method,
                                  self.GLib.Variant(signature, args), None, 0, 5000, None).unpack()

    def prop(self, name, path, prop):
        return self.call(name, path, "org.freedesktop.DBus.Properties", "Get", "(ss)",
                         ("org.kde.StatusNotifierItem", prop))[0]

    def layout(self, name, menu):
        root = self.call(name, menu, "com.canonical.dbusmenu", "GetLayout", "(iias)",
                         (0, -1, ["label"]))[1]
        flat = {}

        def walk(node, parents=()):
            ident, props, kids = node
            label = props.get("label", "").replace("_", "")
            if label:
                flat[ident] = " > ".join(parents + (label,))
            for kid in kids:
                walk(kid, parents + ((label,) if label else ()))
        for kid in root[2]:     # the root is the menu itself ("Label Empty" on ayatana)
            walk(kid)
        return flat

    def click(self, name, menu, ident):
        self.call(name, menu, "com.canonical.dbusmenu", "Event", "(isvu)",
                  (ident, "clicked", self.GLib.Variant("s", ""), int(time.time())))


def check_tray():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        result("PyGObject + GTK3 import", True)
    except Exception as error:  # noqa: BLE001
        result("PyGObject + GTK3 import", False, error)
        return
    host = FakeHost()
    # a desktop session exports its display to the user manager the same way
    user("set-environment", "DISPLAY=:99", "GDK_BACKEND=x11")
    user("restart", "discord-game-presence-tray.service")
    found = wait(lambda: host.items, 25)
    result("tray icon registers with the panel (StatusNotifier)", bool(found),
           user("status", "discord-game-presence-tray.service", "--no-pager").stdout[-300:])
    if not found:
        return
    name, path = host.items[0]
    try:
        title = host.prop(name, path, "Title")
        menu = host.prop(name, path, "Menu")
        labels = wait(lambda: host.layout(name, menu), 10) or {}
    except Exception as error:  # noqa: BLE001
        result("tray menu readable over dbusmenu", False, error)
        return
    texts = set(labels.values())
    (RUN / "tray-menu.txt").write_text("\n".join(sorted(texts)))
    result("tray menu readable over dbusmenu", len(labels) > 10, f"title={title!r}, {len(labels)} items")
    expected = ["Pause presence", "Apps Discord can see", "Quit tray"]
    missing = [e for e in expected if not any(t.split(" > ")[-1].startswith(e) for t in texts)]
    result("menu has Pause, Apps and Quit", not missing, f"missing {missing}" if missing else "")
    result("Jogo Falso listed under Apps",
           any(t.startswith("Apps Discord can see > ") and "Jogo Falso" in t for t in texts))

    pause = next((i for i, t in labels.items() if t == "Pause presence > 15 minutes"), None)
    if pause is None:
        result("clicking Pause > 15 minutes pauses the daemon", False, "item not found")
    else:
        host.click(name, menu, pause)
        paused = wait(lambda: status().get("paused"), 10)
        result("clicking Pause > 15 minutes pauses the daemon", bool(paused),
               f"paused_until={status().get('paused_until')}")
        resume = wait(lambda: next((i for i, t in host.layout(name, menu).items()
                                    if t.endswith("Resume presence")), None), 10)
        if resume is not None:
            host.click(name, menu, resume)
        result("Resume brings it back", bool(resume) and bool(wait(lambda: not status().get("paused"), 10)))

    if KIND == "script":
        entry = HOME / ".local/share/applications/discord-presence-tray.desktop"
        ok = entry.exists()
        detail = ""
        if ok and shutil.which("desktop-file-validate"):
            check = sh("desktop-file-validate", str(entry))
            ok = check.returncode == 0
            detail = (check.stdout + check.stderr).strip()
        result("application menu entry created and valid", ok, detail or str(entry))
    else:
        entries = [p for p in ("/usr/share/applications/discord-presence-tray.desktop",) if os.path.exists(p)]
        result("package ships the application menu entry", bool(entries))

    # a second launch from the menu must not add a second icon
    tray_bin = shutil.which("discord-presence-tray") or str(HOME / ".local/bin/discord-presence-tray.py")
    env = dict(os.environ, DISPLAY=":99", GDK_BACKEND="x11")
    second = subprocess.run([tray_bin], env=env, capture_output=True, text=True, timeout=20)
    result("second tray exits instead of adding an icon",
           second.returncode == 0 and "already running" in second.stderr, second.stderr.strip()[-120:])

    user("stop", "discord-game-presence-tray.service")


def main():
    check_daemon()
    check_tray()
    print(f"SUMMARY|{KIND}|{'fail' if FAILED else 'ok'}|{', '.join(FAILED)}", flush=True)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

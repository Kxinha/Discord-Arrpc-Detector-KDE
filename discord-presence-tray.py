#!/usr/bin/env python3
"""Tray icon for the Discord presence daemon.

GTK3 + AppIndicator, no Qt and no windows: the menu is the whole interface.
It drives the systemd user service, decides which apps Discord gets to see,
and keeps its own look in the same config file the daemon reads.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
try:  # the maintained fork on most distros, the old name on the rest
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ImportError, ValueError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
from gi.repository import GLib, Gtk  # noqa: E402

APP = "discord-game-presence"
SERVICE = f"{APP}.service"
PROJECT_URL = "https://github.com/gabrielmf1998/Discord-Arrpc-Detector-KDE"

HOME = pathlib.Path.home()
CONFIG_HOME = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
CACHE_HOME = pathlib.Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache"))
CONFIG = CONFIG_HOME / f"{APP}.json"
CACHE = CACHE_HOME / APP
ICONS = CACHE / "icons"
TRAY_SERVICE = f"{APP}-tray.service"
KDEGLOBALS = CONFIG_HOME / "kdeglobals"

REFRESH_SECONDS = 8
UPDATE_FIRST_CHECK = 45          # seconds after the tray starts
UPDATE_EVERY = 6 * 3600          # then every six hours
UPDATE_MODES = [("Install updates automatically", "auto"),
                ("Notify me, I click to install", "notify"),
                ("Never check", "off")]
PAUSES = [("15 minutes", 15), ("1 hour", 60), ("4 hours", 240), ("Until I resume", None)]
DETAIL_CHOICES = [("Everything: title, cover, links, buttons", "full"),
                  ("Title and cover, nothing clickable", "basic"),
                  ("Only the app name and time", "minimal")]
SHOW_CHOICES = [("Cover image", "thumbnail"), ("Links in the text", "links"),
                ("Buttons", "buttons"), ("Timer and progress bar", "time")]
# entries that play media; the daemon reads the same keys (MEDIA_DEFAULTS)
MEDIA_KEYS = {"youtube": True, "youtube-music": True, "stremio": False}  # True: web
MEDIA_CHOICES = [("Show ads as \u201cAd\u201d", "show_ads"),
                 ("Show browsing (home, channel, search)", "show_browsing"),
                 ("Show what I search", "show_search")]
PAUSE_MINUTES = [1, 5, 15, 30, 60]
KIND_LABELS = {"script": "installer script", "pacman": "pacman package",
               "deb": ".deb package", "rpm": ".rpm package", "appimage": "AppImage",
               "manual": "manual copy"}

COLORS = [
    # Discord's own palette
    ("Blurple", "#5865F2"),
    ("Green", "#57F287"),
    ("Yellow", "#FEE75C"),
    ("Fuchsia", "#EB459E"),
    ("Red", "#ED4245"),
    # Breeze/Plasma, to sit well next to a KDE tray
    ("Plasma Blue", "#3DAEE9"),
    ("Breeze Green", "#27AE60"),
    ("Breeze Orange", "#F67400"),
    ("Breeze Purple", "#9B59B6"),
    ("Breeze Teal", "#16A085"),
    ("Breeze Red", "#DA4453"),
    # neutrals, for panels that prefer monochrome icons
    ("White", "#FFFFFF"),
    ("Light Gray", "#BDC3C7"),
    ("Graphite", "#4E5058"),
    ("Near Black", "#232629"),
]
ACCENT = "accent"          # resolved from the current KDE color scheme
IDLE_COLOR = "#72767D"     # whenever the daemon is not running

SHAPES = [
    ("Blob", "blob"),
    ("Controller", "controller"),
    ("Broadcast", "broadcast"),
    ("Bolt", "bolt"),
    ("Chat", "chat"),
    ("Headset", "headset"),
    ("Rocket", "rocket"),
    ("Ghost", "ghost"),
    ("Dot", "dot"),
]

# Icons from the current icon theme (Breeze on KDE), for a tray that matches
# the rest of the panel instead of standing out.
THEME_ICONS = [
    ("Discord", "discord"),
    ("Discord (tray)", "discord-tray"),
    ("Vesktop", "vesktop"),
    ("Games", "applications-games"),
    ("Gamepad", "input-gaming"),
    ("Gaming settings", "preferences-desktop-gaming"),
    ("Multimedia", "applications-multimedia"),
    ("Recording", "media-record"),
    ("Network", "network-connect"),
    ("User online", "user-online"),
]

# Every shape is drawn here rather than shipped as a file, so a new color is
# just a re-render. The mask is what carves the eyes and buttons out.
SHAPE_SVG = {
    "blob": """
      <mask id="cut">
        <rect width="24" height="24" fill="#fff"/>
        <ellipse cx="9.3" cy="12.5" rx="1.6" ry="2" fill="#000"/>
        <ellipse cx="14.7" cy="12.5" rx="1.6" ry="2" fill="#000"/>
      </mask>
      <path mask="url(#cut)" fill="{color}" d="M4.6 6.4C6.9 5 9.4 4.3 12 4.3
        s5.1.7 7.4 2.1c1.9 2.6 2.7 5.7 2.3 8.8-.1.6-.5 1.1-1.1 1.4-1.3.6-2.7
        1.1-4.1 1.4l-.9-1.7c.5-.2 1-.4 1.5-.6-3.3 1.4-7 1.4-10.3 0 .5.2 1 .5
        1.5.6l-.9 1.7c-1.4-.3-2.8-.7-4.1-1.4-.5-.3-.9-.8-1-1.4-.4-3.1.4-6.2
        2.3-8.8Z"/>
    """,
    "controller": """
      <mask id="cut">
        <rect width="24" height="24" fill="#fff"/>
        <rect x="5.9" y="12.1" width="3.8" height="1.3" rx=".65" fill="#000"/>
        <rect x="7.15" y="10.85" width="1.3" height="3.8" rx=".65" fill="#000"/>
        <circle cx="15.7" cy="12" r="1" fill="#000"/>
        <circle cx="17.9" cy="14" r="1" fill="#000"/>
      </mask>
      <path mask="url(#cut)" fill="{color}" d="M8.2 7.5h7.6c3 0 5.2 2.4 5.2 5.4
        v3.6c0 1.5-1.2 2.7-2.7 2.7-.9 0-1.7-.4-2.2-1.1l-1.6-2.2H9.5l-1.6 2.2
        c-.5.7-1.3 1.1-2.2 1.1-1.5 0-2.7-1.2-2.7-2.7v-3.6c0-3 2.2-5.4 5.2-5.4Z"/>
    """,
    "broadcast": """
      <circle cx="12" cy="12" r="2.7" fill="{color}"/>
      <g fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round">
        <path d="M7.9 7.9a5.8 5.8 0 0 0 0 8.2"/>
        <path d="M16.1 7.9a5.8 5.8 0 0 1 0 8.2"/>
        <path d="M5.2 5.2a9.6 9.6 0 0 0 0 13.6"/>
        <path d="M18.8 5.2a9.6 9.6 0 0 1 0 13.6"/>
      </g>
    """,
    "bolt": """
      <path fill="{color}" d="M13.9 2 5.2 13.4h5.1L9.5 22l9.3-11.7h-5.5L13.9 2Z"/>
    """,
    "chat": """
      <mask id="cut">
        <rect width="24" height="24" fill="#fff"/>
        <circle cx="8.6" cy="11.4" r="1.15" fill="#000"/>
        <circle cx="12" cy="11.4" r="1.15" fill="#000"/>
        <circle cx="15.4" cy="11.4" r="1.15" fill="#000"/>
      </mask>
      <path mask="url(#cut)" fill="{color}" d="M12 3.4c5 0 9 3.4 9 7.7 0 4.2-4
        7.6-9 7.6-1 0-2-.1-2.9-.4l-4.6 2.3 1.4-4.1C3.4 15.2 3 13.2 3 11.1
        3 6.8 7 3.4 12 3.4Z"/>
    """,
    "headset": """
      <path fill="none" stroke="{color}" stroke-width="1.9" stroke-linecap="round"
        d="M4.7 14.6v-2.4a7.3 7.3 0 0 1 14.6 0v2.4"/>
      <rect x="2.6" y="13.1" width="4.1" height="6.3" rx="1.9" fill="{color}"/>
      <rect x="17.3" y="13.1" width="4.1" height="6.3" rx="1.9" fill="{color}"/>
    """,
    "rocket": """
      <mask id="cut">
        <rect width="24" height="24" fill="#fff"/>
        <circle cx="12" cy="9.4" r="1.6" fill="#000"/>
      </mask>
      <path mask="url(#cut)" fill="{color}" d="M12 1.8c2.9 2.4 4.6 6 4.6 9.8v3.3
        l-1.7 1.7H9.1l-1.7-1.7v-3.3c0-3.8 1.7-7.4 4.6-9.8Z"/>
      <path fill="{color}" d="M7.4 11.9 4.8 14.5v3.8l2.6-1.7v-4.7Zm9.2 0v4.7
        l2.6 1.7v-3.8l-2.6-2.6Z"/>
      <path fill="{color}" d="M10.4 18.4h3.2L12 22.2l-1.6-3.8Z"/>
    """,
    "ghost": """
      <mask id="cut">
        <rect width="24" height="24" fill="#fff"/>
        <circle cx="9.6" cy="10.9" r="1.25" fill="#000"/>
        <circle cx="14.4" cy="10.9" r="1.25" fill="#000"/>
      </mask>
      <path mask="url(#cut)" fill="{color}" d="M12 2.6c4 0 7.2 3.2 7.2 7.2v11.6
        l-2.4-1.8-2.4 1.8-2.4-1.8-2.4 1.8-2.4-1.8-2.4 1.8V9.8c0-4 3.2-7.2
        7.2-7.2Z"/>
    """,
    "dot": """
      <circle cx="12" cy="12" r="6.4" fill="{color}"/>
    """,
}


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def systemctl(*args):
    return run("systemctl", "--user", *args)


def service_active():
    return systemctl("is-active", SERVICE).stdout.strip() == "active"


def service_enabled():
    return systemctl("is-enabled", SERVICE).stdout.strip() == "enabled"


def restart_daemon():
    """Restart without waiting: stopping clears every presence first, which
    takes a few seconds, and the menu used to freeze for that long."""
    systemctl("--no-block", "restart", SERVICE)
    if Tray.current:
        Tray.current.refresh_soon()


def reload_daemon(daemon):
    """Apply a config change without restarting, when the daemon allows it.

    Display options and the end of a pause are read again on SIGHUP, so the
    presence never leaves Discord. A restart reconnects every app being
    shown and Discord slows down a client id that reconnects right away
    (about 12 s without presence, measured on Vesktop 1.6.9). Older daemons,
    or none running, fall back to the restart.
    """
    status = daemon_status(daemon)
    if status and status.get("reload") is True:
        try:
            os.kill(int(status["pid"]), signal.SIGHUP)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        else:
            if Tray.current:
                Tray.current.refresh_soon()
            return True
    if service_active():
        restart_daemon()
    return False


def notify(body, title="Discord Presence", icon=None):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Discord Presence"]
                         + (["-i", icon] if icon else []) + [title, body])


def ask(title, body, actions, on_choice, icon=None):
    """A notification with buttons; on_choice(name) runs on the GTK thread.

    notify-send --action blocks until the notification goes away and prints
    the button that was clicked, so it waits on its own thread.
    """
    if not shutil.which("notify-send"):
        return

    def wait():
        command = ["notify-send", "-a", "Discord Presence"] + (["-i", icon] if icon else [])
        for name, label in actions:
            command += ["-A", f"{name}={label}"]
        try:
            done = subprocess.run(command + [title, body], capture_output=True,
                                  text=True, timeout=24 * 3600)
        except (OSError, subprocess.TimeoutExpired):
            return
        choice = done.stdout.strip()
        if choice:
            GLib.idle_add(on_choice, choice)
    threading.Thread(target=wait, daemon=True).start()


def release_summary(notes, limit=180):
    """First real line of the release notes, without the Markdown."""
    for line in (notes or "").splitlines():
        line = re.sub(r"^[#>*+\-\s]+", "", line)          # heading, quote, bullet
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # [text](link)
        line = re.sub(r"\*\*|__|`", "", line).strip()
        if line:
            return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"
    return ""


def open_path(target):
    subprocess.Popen(["xdg-open", str(target)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kde_accent():
    """Accent color of the current KDE color scheme, as #rrggbb."""
    try:
        text = KDEGLOBALS.read_text(errors="replace")
    except OSError:
        return COLORS[0][1]
    for pattern in (r"^AccentColor=(\d+),(\d+),(\d+)",
                    r"^\[Colors:Selection\][^\[]*?^BackgroundNormal=(\d+),(\d+),(\d+)"):
        m = re.search(pattern, text, re.M | re.S)
        if m:
            return "#%02X%02X%02X" % tuple(int(v) for v in m.groups())
    return COLORS[0][1]


def load_daemon():
    """Import the daemon module to read its catalog, without running it.

    Packaged installs drop the daemon in /usr/bin with no .py suffix, so the
    loader is spelled out instead of inferred from the file name.
    """
    here = pathlib.Path(__file__).resolve().parent
    candidates = [os.environ.get("DGP_DAEMON"),
                  HOME / ".local/bin/discord-game-presence.py",
                  HOME / ".local/bin/discord-game-presence",
                  here / "discord-game-presence.py",
                  here / "discord-game-presence",
                  pathlib.Path("/usr/bin/discord-game-presence"),
                  pathlib.Path("/usr/local/bin/discord-game-presence")]
    for candidate in candidates:
        if not candidate or not pathlib.Path(candidate).is_file():
            continue
        loader = importlib.machinery.SourceFileLoader("dgp", str(candidate))
        spec = importlib.util.spec_from_loader("dgp", loader)
        module = importlib.util.module_from_spec(spec)
        try:
            loader.exec_module(module)
        except Exception:
            continue  # a broken daemon must not take the tray down with it
        return module
    return None


def read_config():
    try:
        data = json.loads(CONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(config):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def daemon_status(daemon):
    """What the running daemon says it is publishing, or None.

    The daemon writes this file itself (see status_path() there) whenever a
    presence goes up or comes down, so the tray never has to repeat the
    detection to know. A file left by a daemon that died does not count.
    """
    if not daemon or not hasattr(daemon, "status_path"):
        return None
    try:
        data = json.loads(daemon.status_path().read_text())
        pid = int(data["pid"])
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"discord-game-presence" not in f.read():
                return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data if isinstance(data.get("showing"), dict) else None


def suggest_apps(daemon, known_ids, known_keys, refresh=False):
    """Running apps that Discord's catalog recognizes but we don't track.

    The work runs in a separate daemon process (--suggest): the catalog is
    13 MB of JSON and Python keeps most of that memory after a scan in a
    long-lived process, which is how the tray used to sit at 100 MB.
    """
    if not daemon or not hasattr(daemon, "suggest_running"):
        return []
    request = json.dumps({"known_ids": sorted(filter(None, known_ids)),
                          "known_keys": sorted(known_keys), "refresh": refresh})
    path = getattr(daemon, "__file__", None)
    if path:
        try:
            done = subprocess.run([sys.executable, "-I", path, "--suggest"], input=request,
                                  capture_output=True, text=True, timeout=120)
            found = json.loads(done.stdout) if done.returncode == 0 else None
            if isinstance(found, list):
                return found
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:  # no helper process: same work here, costs memory but still works
        apps = daemon.detectable_apps(force=refresh)
        return daemon.suggest_running(known_ids, known_keys, apps)
    except Exception:
        return []


class Tray:
    current = None  # the running tray, for refresh_soon() after a restart

    def __init__(self):
        Tray.current = self
        self.daemon = load_daemon()
        self.config = read_config()
        self.detected = set()
        self.published = False  # detected came from the daemon, not a scan
        self.scanning = False
        self.menu_paused = False
        self.suggestions = []
        self.updating = False   # guards programmatic toggles
        self.release = None     # a newer release, once a check finds one
        self.update_busy = None  # "checking" or "installing"
        self.kind = None

        ICONS.mkdir(parents=True, exist_ok=True)
        self.current_icon = None
        self.indicator = AppIndicator3.Indicator.new(
            APP, "", AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_icon_theme_path(str(ICONS))
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Discord Presence")
        self.rebuild_menu()

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)
        threading.Thread(target=self.rescan_suggestions, daemon=True).start()
        threading.Thread(target=self.find_kind, daemon=True).start()
        GLib.timeout_add_seconds(UPDATE_FIRST_CHECK, self.first_update_check)
        GLib.idle_add(self.announce_new_version)

    # ---------------------------------------------------------------- look
    def tray_prefs(self):
        return self.config.setdefault("tray", {})

    def shape(self):
        return self.tray_prefs().get("shape", "blob")

    def theme_icon(self):
        return self.tray_prefs().get("theme_icon")

    def color(self):
        color = self.tray_prefs().get("color", COLORS[0][1])
        return kde_accent() if color == ACCENT else color

    def icon_ref(self, active):
        """Render the current shape/color pair and return what to announce.

        The absolute file path, not a bare icon name: the tray host caches its
        icon-theme lookups, so a freshly written file under the theme path is
        not picked up and the icon would never change. A path is loaded
        straight from disk.
        """
        if self.theme_icon():        # an icon from the desktop theme instead
            return self.theme_icon()
        shape = self.shape()
        color = self.color() if active else IDLE_COLOR
        path = ICONS / f"{APP}-{shape}-{color.lstrip('#')}.svg"
        if not path.exists():
            body = SHAPE_SVG.get(shape, SHAPE_SVG["blob"]).format(color=color)
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
                f'width="24" height="24">{body}</svg>')
        return str(path)

    def apply_icon(self, active):
        icon = self.icon_ref(active)
        if icon == getattr(self, "current_icon", None):
            return
        self.current_icon = icon
        self.indicator.set_icon_full(icon, "Discord Presence")

    # ---------------------------------------------------------------- menu
    def rebuild_menu(self):
        self.hidden_radios = []
        self.menu = self.build_menu()
        self.indicator.set_menu(self.menu)

    def build_menu(self):
        menu = Gtk.Menu()
        self.menu_paused = self.pause_until() is not None
        self.menu_games = self.games_snapshot()

        self.status_item = Gtk.MenuItem(label="Checking…")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)

        self.showing_item = Gtk.MenuItem(label="")
        self.showing_item.set_sensitive(False)
        menu.append(self.showing_item)

        if self.pause_until() is not None:
            resume = Gtk.MenuItem(label="▶  Resume presence")
            resume.connect("activate", lambda *_: self.resume())
            menu.append(resume)
        else:
            pause = Gtk.MenuItem(label="Pause presence")
            pause_menu = Gtk.Menu()
            for label, minutes in PAUSES:
                item = Gtk.MenuItem(label=label)
                item.connect("activate", lambda _i, m=minutes: self.pause_for(m))
                pause_menu.append(item)
            pause.set_submenu(pause_menu)
            menu.append(pause)

        if self.release or self.update_busy == "installing":
            menu.append(Gtk.SeparatorMenuItem())
            if self.update_busy == "installing":
                item = Gtk.MenuItem(label=f"Updating to {self.release['version']}…")
                item.set_sensitive(False)
            else:
                item = Gtk.MenuItem(label=f"⬆  Update to {self.release['version']}")
                item.connect("activate", lambda *_: self.start_update())
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        apps = Gtk.MenuItem(label="Apps Discord can see")
        apps.set_submenu(self.build_apps_menu())
        menu.append(apps)

        details = Gtk.MenuItem(label="Presence details")
        details.set_submenu(self.build_presence_menu())
        menu.append(details)

        appearance = Gtk.MenuItem(label="Appearance")
        appearance.set_submenu(self.build_appearance_menu())
        menu.append(appearance)

        daemon = Gtk.MenuItem(label="Daemon")
        daemon.set_submenu(self.build_daemon_menu())
        menu.append(daemon)

        config = Gtk.MenuItem(label="Configuration")
        config.set_submenu(self.build_config_menu())
        menu.append(config)

        updates = Gtk.MenuItem(label="Updates")
        updates.set_submenu(self.build_updates_menu())
        menu.append(updates)

        menu.append(Gtk.SeparatorMenuItem())

        self.toggle_item = Gtk.MenuItem(label="Start")
        self.toggle_item.connect("activate", self.on_toggle_service)
        menu.append(self.toggle_item)

        restart = Gtk.MenuItem(label="Restart daemon")
        restart.connect("activate", self.on_restart)
        menu.append(restart)

        quit_item = Gtk.MenuItem(label="Quit tray")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    # ------------------------------------------------------------ catalog
    def builtin_games(self):
        return dict(getattr(self.daemon, "GAMES", {})) if self.daemon else {}

    def catalog(self):
        """Everything the daemon knows plus everything the config added."""
        games = self.builtin_games()
        custom = set()
        for proc, game in (self.config.get("games") or {}).items():
            if not isinstance(game, dict):
                continue
            if proc in games:
                merged = {**games[proc], **game}
                if any(k in game for k in ("exe", "args", "cmdline", "match")):
                    merged.pop("running", None)  # same rule as the daemon
                games[proc] = merged
            else:
                games[proc] = {"large_text": proc, **game}
                custom.add(proc)
        entries = []
        for proc, game in games.items():
            label = game.get("label") or game.get("large_text") or proc
            entries.append((proc, label, game, proc in custom))
        entries.sort(key=lambda e: e[1].lower())
        return entries

    def app_enabled(self, proc, game):
        override = (self.config.get("games") or {}).get(proc, {})
        if "enabled" in override:
            return bool(override["enabled"])
        return bool(game.get("enabled", True)) and bool(game.get("client_id"))

    def build_apps_menu(self):
        menu = Gtk.Menu()
        self.app_items = {}

        for proc, label, game, custom in self.catalog():
            item = Gtk.CheckMenuItem(label=label)
            item.set_active(self.app_enabled(proc, game))
            item.connect("toggled", self.on_app_toggled, proc)
            item.set_tooltip_text(f"process match: {proc}")
            menu.append(item)
            self.app_items[proc] = (item, label)

        menu.append(Gtk.SeparatorMenuItem())

        add = Gtk.MenuItem(label="Add a running app")
        add.set_submenu(self.build_add_menu())
        menu.append(add)

        remove = Gtk.MenuItem(label="Remove an app I added")
        remove.set_submenu(self.build_remove_menu())
        menu.append(remove)

        menu.append(Gtk.SeparatorMenuItem())
        for label, value in (("Enable all", True), ("Disable all", False)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self.on_set_all, value)
            menu.append(item)

        menu.show_all()
        return menu

    def build_add_menu(self):
        """Running apps that Discord's own catalog recognizes."""
        menu = Gtk.Menu()
        if not self.suggestions:
            empty = Gtk.MenuItem(label="Nothing new found while scanning")
            empty.set_sensitive(False)
            menu.append(empty)
        for found in self.suggestions:
            item = Gtk.MenuItem(label=f"{found['name']}  ({found['process']})")
            item.connect("activate", self.on_add_app, found)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        rescan = Gtk.MenuItem(label="Scan running apps again")
        rescan.connect("activate", lambda *_: threading.Thread(
            target=self.rescan_suggestions, daemon=True).start())
        menu.append(rescan)

        refresh = Gtk.MenuItem(label="Update Discord's game list")
        refresh.connect("activate", lambda *_: threading.Thread(
            target=self.rescan_suggestions, args=(True,), daemon=True).start())
        menu.append(refresh)

        menu.show_all()
        return menu

    def build_remove_menu(self):
        menu = Gtk.Menu()
        custom = [(proc, label) for proc, label, _game, is_custom in self.catalog()
                  if is_custom]
        if not custom:
            empty = Gtk.MenuItem(label="No apps added by you yet")
            empty.set_sensitive(False)
            menu.append(empty)
        for proc, label in custom:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self.on_remove_app, proc)
            menu.append(item)
        menu.show_all()
        return menu

    def build_presence_menu(self):
        """How much each app shows: one submenu per app, same keys as the
        daemon reads from the config."""
        menu = Gtk.Menu()
        hint = Gtk.MenuItem(label="What your friends see, per app")
        hint.set_sensitive(False)
        menu.append(hint)
        menu.append(Gtk.SeparatorMenuItem())
        for proc, label, game, _custom in self.catalog():
            if not self.app_enabled(proc, game):
                continue
            item = Gtk.MenuItem(label=label)
            item.set_submenu(self.build_app_detail_menu(proc, game))
            menu.append(item)
        menu.show_all()
        return menu

    def build_app_detail_menu(self, proc, game):
        menu = Gtk.Menu()
        level = game.get("detail") if game.get("detail") in {v for _l, v in DETAIL_CHOICES} \
            else "full"
        group = []
        for label, value in DETAIL_CHOICES:
            item = Gtk.RadioMenuItem(label=label, group=group[0] if group else None)
            group.append(item)
            item.set_active(value == level)
            item.connect("toggled", self.on_detail_level, proc, value)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        shown = self.presence_options(game)
        for label, name in SHOW_CHOICES:
            item = Gtk.CheckMenuItem(label=label)
            item.set_active(shown.get(name, True))
            item.set_sensitive(shown.get("text", True) or name == "time")
            item.connect("toggled", self.on_app_option, proc, f"show_{name}")
            menu.append(item)

        if proc in MEDIA_KEYS:
            menu.append(Gtk.SeparatorMenuItem())
            keep = Gtk.CheckMenuItem(label="Keep showing while paused")
            keep.set_active(self.media_option(game, "show_paused"))
            keep.connect("toggled", self.on_app_option, proc, "show_paused")
            menu.append(keep)

            after = Gtk.MenuItem(label="Hide paused media after")
            after_menu = Gtk.Menu()
            current = self.media_option(game, "pause_minutes")
            choices = sorted(set(PAUSE_MINUTES) | {current})
            group = []
            for minutes in choices:
                text = f"{minutes:g} minute" + ("" if minutes == 1 else "s")
                item = Gtk.RadioMenuItem(label=text, group=group[0] if group else None)
                group.append(item)
                item.set_active(minutes == current)
                item.connect("toggled", self.on_pause_minutes, proc, minutes)
                after_menu.append(item)
            after.set_submenu(after_menu)
            after.set_sensitive(self.media_option(game, "show_paused"))
            menu.append(after)

            if MEDIA_KEYS[proc]:
                for label, name in MEDIA_CHOICES:
                    item = Gtk.CheckMenuItem(label=label)
                    item.set_active(self.media_option(game, name))
                    item.connect("toggled", self.on_app_option, proc, name)
                    menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        reset = Gtk.MenuItem(label="Back to the defaults")
        reset.connect("activate", lambda *_: self.reset_app_options(proc))
        menu.append(reset)
        menu.show_all()
        return menu

    def presence_options(self, game):
        options = getattr(self.daemon, "presence_options", None)
        if not options:
            return {name: True for _label, name in SHOW_CHOICES}
        try:
            return options(game)
        except Exception:
            return {name: True for _label, name in SHOW_CHOICES}

    def media_option(self, game, name):
        defaults = getattr(self.daemon, "MEDIA_DEFAULTS", {})
        default = defaults.get(name, True if name.startswith("show_") else 5)
        value = game.get(name)
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return value
        return default

    def set_app_options(self, proc, change, message=None):
        self.config = read_config()
        entry = self.config.setdefault("games", {}).setdefault(proc, {})
        change(entry)
        write_config(self.config)
        reload_daemon(self.daemon)  # live, the presence stays up
        self.rebuild_menu()
        if message:
            notify(message)

    def on_detail_level(self, item, proc, level):
        if self.updating or not item.get_active():
            return

        def change(entry):
            entry["detail"] = level
            for _label, name in SHOW_CHOICES:  # the level decides again
                entry.pop(f"show_{name}", None)
        self.set_app_options(proc, change)

    def on_app_option(self, item, proc, key):
        if self.updating:
            return
        self.set_app_options(proc, lambda entry: entry.__setitem__(key, item.get_active()))

    def on_pause_minutes(self, item, proc, minutes):
        if self.updating or not item.get_active():
            return
        self.set_app_options(proc, lambda entry: entry.__setitem__("pause_minutes", minutes))

    def reset_app_options(self, proc):
        keys = (["detail", "pause_minutes", "show_paused", "browse_minutes"]
                + [f"show_{name}" for _label, name in SHOW_CHOICES]
                + [name for _label, name in MEDIA_CHOICES])

        def change(entry):
            for key in keys:
                entry.pop(key, None)
        self.set_app_options(proc, change, "Presence details back to the defaults.")

    # ------------------------------------------------------------- pause
    def pause_until(self):
        """None when not paused, 0 for "until I resume", else the epoch."""
        pause = self.config.get("pause")
        if not isinstance(pause, dict):
            return None
        until = pause.get("until")
        if until is None:
            return 0
        if isinstance(until, (int, float)) and until > time.time():
            return until
        return None

    def pause_for(self, minutes):
        self.config = read_config()
        until = None if minutes is None else int(time.time() + minutes * 60)
        self.config["pause"] = {"until": until}
        write_config(self.config)
        status = daemon_status(self.daemon)
        if status and status.get("paused"):
            reload_daemon(self.daemon)  # already waiting, only the end changes
        elif service_active():
            restart_daemon()  # it clears everything and waits out the pause
        notify("Presence paused " + ("until you resume it." if until is None else
                                     time.strftime("until %H:%M.", time.localtime(until))))
        self.rebuild_menu()
        self.refresh()

    def resume(self, quiet=False):
        self.config = read_config()
        had = self.config.pop("pause", None) is not None
        if had:
            write_config(self.config)
        status = daemon_status(self.daemon)
        if status and status.get("paused"):
            reload_daemon(self.daemon)  # wakes the waiting daemon right away
        elif service_active() and not status:
            restart_daemon()
        if not quiet:
            notify("Presence resumed.")
        self.rebuild_menu()
        self.refresh()

    def radio_leader(self):
        """A radio group whose first item is never shown.

        GTK always keeps one item of a group checked, the first by default.
        When no visible choice applies (a theme icon instead of a shape), that
        put a false check on the first item, and clicking it did nothing,
        since it was already "on".
        """
        leader = Gtk.RadioMenuItem(label="none")
        self.hidden_radios.append(leader)  # unparented: keep it alive
        return leader

    def build_appearance_menu(self):
        menu = Gtk.Menu()

        shapes = Gtk.MenuItem(label="Icon shape")
        shape_menu = Gtk.Menu()
        group = [self.radio_leader()]  # none checked while a theme icon is in use
        for label, shape in SHAPES:
            item = Gtk.RadioMenuItem(label=label, group=group[0] if group else None)
            group.append(item)
            item.set_active(not self.theme_icon() and shape == self.shape())
            item.connect("toggled", self.on_shape_chosen, shape)
            shape_menu.append(item)
        shapes.set_submenu(shape_menu)
        menu.append(shapes)

        themed = Gtk.MenuItem(label="Icon from desktop theme")
        theme_menu = Gtk.Menu()
        icon_theme = Gtk.IconTheme.get_default()
        group = [self.radio_leader()]  # none checked while a drawn shape is in use
        for label, name in THEME_ICONS:
            if not icon_theme.has_icon(name):
                continue
            item = Gtk.RadioMenuItem(label=label, group=group[0] if group else None)
            group.append(item)
            item.set_active(self.theme_icon() == name)
            item.connect("toggled", self.on_theme_icon_chosen, name)
            theme_menu.append(item)
        themed.set_submenu(theme_menu)
        menu.append(themed)

        colors = Gtk.MenuItem(label="Color")
        color_menu = Gtk.Menu()
        group = []
        accent = Gtk.RadioMenuItem(label=f"Match KDE accent  {kde_accent()}")
        group.append(accent)
        accent.set_active(self.tray_prefs().get("color") == ACCENT)
        accent.connect("toggled", self.on_color_chosen, ACCENT)
        color_menu.append(accent)
        color_menu.append(Gtk.SeparatorMenuItem())
        for label, value in COLORS:
            item = Gtk.RadioMenuItem(label=f"{label}  {value}", group=group[0])
            group.append(item)
            item.set_active(self.tray_prefs().get("color", COLORS[0][1]) == value)
            item.connect("toggled", self.on_color_chosen, value)
            color_menu.append(item)
        colors.set_submenu(color_menu)
        menu.append(colors)

        menu.show_all()
        return menu

    def build_daemon_menu(self):
        menu = Gtk.Menu()

        self.boot_item = Gtk.CheckMenuItem(label="Start at boot")
        self.boot_item.connect("toggled", self.on_boot_toggled)
        menu.append(self.boot_item)

        self.login_item = Gtk.CheckMenuItem(label="Start tray at login")
        self.login_item.connect("toggled", self.on_login_toggled)
        menu.append(self.login_item)

        menu.append(Gtk.SeparatorMenuItem())

        for label, handler in (
            ("Clear stuck presence", lambda *_: threading.Thread(
                target=self.clear_presence, daemon=True).start()),
            ("Reload catalog", lambda *_: self.on_reload_catalog()),
            ("View log", lambda *_: self.view_log()),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            menu.append(item)

        menu.show_all()
        return menu

    def build_config_menu(self):
        menu = Gtk.Menu()
        path_item = Gtk.MenuItem(label=str(CONFIG))
        path_item.set_sensitive(False)
        menu.append(path_item)
        menu.append(Gtk.SeparatorMenuItem())
        for label, handler in (
            ("Open config file", lambda *_: self.open_config()),
            ("Open config folder", lambda *_: open_path(CONFIG.parent)),
            ("Copy config path", lambda *_: self.copy_config_path()),
            ("Project page", lambda *_: open_path(PROJECT_URL)),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            menu.append(item)
        menu.show_all()
        return menu

    def build_updates_menu(self):
        menu = Gtk.Menu()
        kind = KIND_LABELS.get(self.kind, "checking…") if self.kind else "checking…"
        version = Gtk.MenuItem(label=f"Version {self.version()}  ·  {kind}")
        version.set_sensitive(False)
        menu.append(version)

        if self.update_busy == "checking":
            check = Gtk.MenuItem(label="Checking…")
            check.set_sensitive(False)
        else:
            check = Gtk.MenuItem(label="Check for updates now")
            check.connect("activate", lambda *_: self.check_updates(manual=True))
            check.set_sensitive(self.update_busy is None and self.can_update())
        menu.append(check)

        menu.append(Gtk.SeparatorMenuItem())
        group = []
        for label, mode in UPDATE_MODES:
            if mode == "auto" and self.kind and self.kind not in self.self_updating():
                label = "Install automatically (not for packages: needs your password)"
            item = Gtk.RadioMenuItem(label=label, group=group[0] if group else None)
            group.append(item)
            item.set_active(self.update_mode() == mode)
            if mode == "auto" and self.kind and self.kind not in self.self_updating():
                item.set_sensitive(False)
            item.connect("toggled", self.on_update_mode, mode)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        notes = Gtk.MenuItem(label="Release notes")
        notes.connect("activate", lambda *_: open_path(self.release_page()))
        menu.append(notes)

        menu.show_all()
        return menu

    # ------------------------------------------------------------- updates
    def version(self):
        return getattr(self.daemon, "VERSION", "?") if self.daemon else "?"

    def can_update(self):
        return bool(self.daemon) and hasattr(self.daemon, "latest_release")

    def self_updating(self):
        return getattr(self.daemon, "SELF_UPDATING", ("script", "appimage"))

    def release_page(self):
        if self.release:
            return self.release["page"]
        return getattr(self.daemon, "RELEASES_PAGE", PROJECT_URL + "/releases")

    def update_mode(self):
        mode = self.tray_prefs().get("updates", "notify")
        return mode if mode in {m for _label, m in UPDATE_MODES} else "notify"

    def on_update_mode(self, item, mode):
        if self.updating or not item.get_active():
            return
        self.config = read_config()
        self.config.setdefault("tray", {})["updates"] = mode
        write_config(self.config)
        if mode != "off" and self.release is None:
            self.check_updates()

    def find_kind(self):
        kind = "manual"
        if self.can_update():
            try:
                kind = self.daemon.install_kind(__file__)
            except Exception:
                pass
        GLib.idle_add(self.set_kind, kind)

    def set_kind(self, kind):
        self.kind = kind
        self.rebuild_menu()
        self.refresh()
        return False

    def announce_new_version(self):
        """After an update, the new tray says so (once)."""
        version = self.version()
        self.config = read_config()
        prefs = self.config.setdefault("tray", {})
        before = prefs.get("version")
        if before != version and version != "?":
            prefs["version"] = version
            write_config(self.config)
            parse = getattr(self.daemon, "version_tuple", lambda _v: None)
            new, old = parse(version), parse(before) if before else None
            if new and old and new > old:
                ask(f"Discord Presence updated to {version}", f"You were on {before}.",
                    [("notes", "What's new")],
                    lambda _choice: open_path(self.release_page()),
                    icon="software-update-available")
        return False

    def first_update_check(self):
        self.check_updates()
        GLib.timeout_add_seconds(UPDATE_EVERY, self.timed_update_check)
        return False

    def timed_update_check(self):
        self.check_updates()
        return True

    def check_updates(self, manual=False):
        if self.update_busy or not self.can_update():
            return
        if not manual and self.update_mode() == "off":
            return
        self.update_busy = "checking"
        self.rebuild_menu()
        self.refresh()

        def work():
            try:
                release, error = self.daemon.latest_release(), None
            except Exception as problem:  # offline, rate limited, bad JSON
                release, error = None, problem
            GLib.idle_add(self.on_checked, release, error, manual)
        threading.Thread(target=work, daemon=True).start()

    def on_checked(self, release, error, manual):
        self.update_busy = None
        newer = release if release and self.daemon.is_newer(release, self.version()) else None
        self.release = newer
        self.rebuild_menu()
        self.refresh()
        if error is not None:
            if manual:
                notify(f"Could not check for updates: {error}")
            return False
        if not newer:
            if manual:
                notify(f"You are on the latest version ({self.version()}).")
            return False

        kind = self.kind or "manual"
        if self.update_mode() == "auto" and kind in self.self_updating():
            self.start_update()
            return False
        prefs = self.tray_prefs()
        if not manual and prefs.get("update_notified") == newer["version"]:
            return False  # already told about this one; the menu item stays
        self.config = read_config()
        self.config.setdefault("tray", {})["update_notified"] = newer["version"]
        write_config(self.config)

        body = release_summary(newer["notes"]) or newer["name"]
        actions = [("notes", "What's new")]
        if kind == "manual":
            body += "\nThis copy is not managed by the installer: get it from the release page."
        else:
            actions.insert(0, ("update", "Update now"))
        ask(f"Discord Presence {newer['version']} is out", body, actions,
            self.on_update_choice, icon="software-update-available")
        return False

    def on_update_choice(self, choice):
        if choice == "update":
            self.start_update()
        elif choice == "notes":
            open_path(self.release_page())
        return False

    def start_update(self):
        release = self.release
        if not release or self.update_busy:
            return
        if (self.kind or "manual") == "manual":
            open_path(release["page"])
            return
        self.update_busy = "installing"
        self.rebuild_menu()

        def work():
            try:
                self.daemon.install_update(release, self.kind, __file__)
                error = None
            except Exception as problem:  # UpdateError says why; anything else too
                error = problem
            GLib.idle_add(self.on_updated, release, error)
        threading.Thread(target=work, daemon=True).start()

    def on_updated(self, release, error):
        if error is not None:
            self.update_busy = None
            self.rebuild_menu()
            ask("Discord Presence update failed", str(error),
                [("notes", "Open the release page")],
                lambda _choice: open_path(release["page"]),
                icon="dialog-error")
            return False
        # the new tray announces itself (announce_new_version), so just restart
        try:
            self.daemon.restart_services(tray=False)
        except Exception:
            pass
        self.restart_tray()
        return False

    def restart_tray(self):
        """Hand over to the new tray: through systemd when it started us."""
        unit = systemctl("show", "-p", "MainPID", "--value", TRAY_SERVICE).stdout.strip()
        if unit == str(os.getpid()):
            systemctl("--no-block", "restart", TRAY_SERVICE)
            return
        if os.environ.get("APPIMAGE"):
            os.execv(os.environ["APPIMAGE"], [os.environ["APPIMAGE"], "tray"])
        os.execv(sys.executable, [sys.executable, str(pathlib.Path(__file__).resolve())]
                 + sys.argv[1:])

    # ------------------------------------------------------------- actions
    def on_toggle_service(self, *_):
        if service_active():
            systemctl("--no-block", "stop", SERVICE)
        else:
            if self.pause_until() is not None:  # starting by hand ends a pause
                self.resume(quiet=True)
            systemctl("--no-block", "start", SERVICE)
        self.refresh()
        self.refresh_soon()

    def on_restart(self, *_):
        restart_daemon()
        notify("Daemon restarted.")
        self.refresh()

    def on_boot_toggled(self, item):
        if self.updating:
            return
        systemctl("enable" if item.get_active() else "disable", SERVICE)

    def on_login_toggled(self, item):
        if self.updating:
            return
        systemctl("enable" if item.get_active() else "disable", TRAY_SERVICE)

    def on_app_toggled(self, item, proc):
        if self.updating:
            return
        self.config = read_config()
        games = self.config.setdefault("games", {})
        games.setdefault(proc, {})["enabled"] = item.get_active()
        write_config(self.config)
        restart_daemon()
        name = self.app_items.get(proc, (None, proc))[1]
        notify(f"{name} {'enabled' if item.get_active() else 'disabled'}.")
        self.refresh()

    def on_set_all(self, _item, value):
        self.config = read_config()
        games = self.config.setdefault("games", {})
        for proc, _label, _game, _custom in self.catalog():
            games.setdefault(proc, {})["enabled"] = value
        write_config(self.config)
        restart_daemon()
        notify("All apps " + ("enabled." if value else "disabled."))
        self.refresh()

    def on_add_app(self, _item, found):
        self.config = read_config()
        games = self.config.setdefault("games", {})
        entry = {
            "client_id": found["client_id"],
            "details": found["name"],
            "state": "Playing",
            "large_text": found["name"],
            "enabled": True,
            # the exact executable, so a grep or an editor never counts
            "exe": found["process"],
        }
        if found.get("args"):
            entry["args"] = found["args"]
        if found.get("icon"):
            entry["large_image"] = ("https://cdn.discordapp.com/app-icons/"
                                    f"{found['client_id']}/{found['icon']}.png")
        games[found["process"]] = entry
        write_config(self.config)
        restart_daemon()
        notify(f"{found['name']} added.")
        self.suggestions = [s for s in self.suggestions
                            if s["client_id"] != found["client_id"]]
        self.rebuild_menu()
        self.refresh()

    def on_remove_app(self, _item, proc):
        self.config = read_config()
        (self.config.get("games") or {}).pop(proc, None)
        write_config(self.config)
        restart_daemon()
        notify("App removed.")
        self.rebuild_menu()
        self.refresh()

    def on_shape_chosen(self, item, shape):
        if self.updating or not item.get_active():
            return
        self.config = read_config()
        prefs = self.config.setdefault("tray", {})
        prefs["shape"] = shape
        prefs.pop("theme_icon", None)   # a drawn shape wins over a theme icon
        write_config(self.config)
        self.apply_icon(service_active())

    def on_theme_icon_chosen(self, item, name):
        if self.updating or not item.get_active():
            return
        self.config = read_config()
        self.config.setdefault("tray", {})["theme_icon"] = name
        write_config(self.config)
        self.apply_icon(service_active())

    def on_color_chosen(self, item, color):
        if self.updating or not item.get_active():
            return
        self.config = read_config()
        prefs = self.config.setdefault("tray", {})
        prefs["color"] = color
        prefs.pop("theme_icon", None)   # color only applies to drawn shapes
        write_config(self.config)
        self.apply_icon(service_active())

    def on_reload_catalog(self):
        self.daemon = load_daemon()
        self.config = read_config()
        self.rebuild_menu()
        self.refresh()
        notify("Catalog reloaded.")

    def clear_presence(self):
        """Ask Discord to drop every presence this project may have left."""
        if not self.daemon:
            return
        ids = {game.get("client_id") for game in self.builtin_games().values()}
        ids |= {game.get("client_id")
                for game in (self.config.get("games") or {}).values()}
        for client_id in filter(None, ids):
            try:
                self.daemon.clear_presence(client_id)
            except Exception:
                pass
        GLib.idle_add(notify, "Cleared any leftover presence.")

    def open_config(self):
        if not CONFIG.exists():  # a starting point beats an editor full of nothing
            write_config({"games": {}, "tray": self.tray_prefs()})
        open_path(CONFIG)

    def copy_config_path(self):
        for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(tool[0]):
                subprocess.run(tool, input=str(CONFIG), text=True)
                notify(f"Copied: {CONFIG}")
                return
        notify(str(CONFIG))

    def view_log(self):
        command = ["journalctl", "--user", "-u", SERVICE, "-f", "-n", "80"]
        for terminal in (["konsole", "-e"], ["gnome-terminal", "--"],
                         ["xfce4-terminal", "-e"], ["alacritty", "-e"],
                         ["kitty"], ["xterm", "-e"]):
            if shutil.which(terminal[0]):
                subprocess.Popen(terminal + command)
                return
        notify("No terminal found. Run: journalctl --user -u " + SERVICE)

    # ------------------------------------------------------------- refresh
    def rescan_suggestions(self, force=False):
        entries = self.catalog()
        known_ids = {game.get("client_id") for _p, _l, game, _c in entries}
        known_keys = {proc for proc, _l, _g, _c in entries}
        found = suggest_apps(self.daemon, known_ids, known_keys, refresh=force)
        GLib.idle_add(self.show_suggestions, found)

    def show_suggestions(self, found):
        self.suggestions = found
        self.rebuild_menu()
        self.refresh()
        return False

    def games_snapshot(self):
        """The app settings the menu was drawn from, to notice hand edits."""
        return json.dumps(self.config.get("games"), sort_keys=True, default=str)

    def refresh(self):
        """Repaint status from systemd and from what the daemon says it is
        showing; only scan for open apps when the daemon cannot tell."""
        self.config = read_config()
        active = service_active()
        paused = self.pause_until()
        if paused is None and "pause" in self.config:  # it ran out by itself
            self.config.pop("pause")
            write_config(self.config)
        if ((paused is not None) != self.menu_paused
                or self.games_snapshot() != self.menu_games):  # edited by hand
            self.rebuild_menu()

        self.updating = True
        if not active:
            status = "Daemon: stopped"
        elif paused is None:
            status = "Daemon: running"
        elif paused:
            status = time.strftime("Daemon: paused until %H:%M", time.localtime(paused))
        else:
            status = "Daemon: paused until you resume"
        self.status_item.set_label(status)
        self.toggle_item.set_label("Stop daemon" if active else "Start daemon")
        self.boot_item.set_active(service_enabled())
        self.login_item.set_active(
            systemctl("is-enabled", TRAY_SERVICE).stdout.strip() == "enabled")
        games = self.builtin_games()
        for proc, (item, _label) in getattr(self, "app_items", {}).items():
            item.set_active(self.app_enabled(proc, games.get(proc, {})))
        self.updating = False

        self.apply_icon(active)
        reported = daemon_status(self.daemon) if active else None
        if reported is not None:
            self.show_detected(set(reported["showing"]), active, paused, True)
        elif not self.scanning:
            self.scanning = True
            threading.Thread(target=self.scan, args=(active, paused), daemon=True).start()
        return True  # keep the timer alive

    def refresh_soon(self, seconds=2):
        """One more repaint once a non-blocking start/stop has settled."""
        GLib.timeout_add_seconds(seconds, lambda: self.refresh() and False)

    def scan(self, active, paused):
        """Which enabled apps are open right now (pgrep/MPRIS, so off the UI).
        Only for a daemon that is stopped or too old to report."""
        found = set()
        try:
            if self.daemon:
                for proc, _label, entry, _custom in self.catalog():
                    if not self.app_enabled(proc, entry):
                        continue
                    try:
                        if hasattr(self.daemon, "is_running"):
                            hit = self.daemon.is_running(proc, entry)
                        else:  # a 1.1 daemon next to a newer tray
                            hit = self.daemon.running(entry.get("running", proc))
                        if hit:
                            found.add(proc)
                    except Exception:
                        pass  # a detector may fail on its own; never take the tray down
        finally:
            GLib.idle_add(self.show_detected, found, active, paused, False)

    def show_detected(self, found, active, paused, published):
        self.scanning = self.scanning and published
        self.detected, self.published = found, published
        mark = "  •  showing" if published else "  •  open"
        for proc, (item, label) in self.app_items.items():
            item.set_label(f"{label}{mark if proc in found else ''}")
        labels = {proc: label for proc, (_item, label) in self.app_items.items()}
        names = sorted(labels.get(proc, proc) for proc in found)
        listed = ", ".join(names) if names else "nothing"
        if active and paused is not None:
            self.showing_item.set_label("Nothing is shown while paused")
            self.indicator.set_title("Discord Presence — paused")
        elif active:
            self.showing_item.set_label(f"Showing now: {listed}")
            self.indicator.set_title(f"Discord Presence — {listed}")
        else:  # open apps are not being published while the daemon is stopped
            self.showing_item.set_label(f"Open, but not published: {listed}")
            self.indicator.set_title("Discord Presence — stopped")
        return False


def main():
    Tray()
    Gtk.main()


if __name__ == "__main__":
    main()

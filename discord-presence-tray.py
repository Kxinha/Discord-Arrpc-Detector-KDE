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
import subprocess
import threading
import time
import urllib.request

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
PROJECT_URL = "https://github.com/gabrielmf1998/arrpc-personalizado-pessoal-linux"
DETECTABLE_URL = "https://discord.com/api/v9/applications/detectable"

HOME = pathlib.Path.home()
CONFIG_HOME = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
CACHE_HOME = pathlib.Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache"))
CONFIG = CONFIG_HOME / f"{APP}.json"
CACHE = CACHE_HOME / APP
ICONS = CACHE / "icons"
DETECTABLE_CACHE = CACHE / "detectable.json"
TRAY_SERVICE = f"{APP}-tray.service"
KDEGLOBALS = CONFIG_HOME / "kdeglobals"

REFRESH_SECONDS = 8
DETECTABLE_MAX_AGE = 7 * 24 * 3600

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


def notify(body):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Discord Presence",
                          "Discord Presence", body])


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


def detectable_apps(force=False):
    """Discord's public list of detectable games, cached on disk."""
    fresh = (DETECTABLE_CACHE.exists()
             and time.time() - DETECTABLE_CACHE.stat().st_mtime < DETECTABLE_MAX_AGE)
    if fresh and not force:
        try:
            return json.loads(DETECTABLE_CACHE.read_text())
        except (OSError, ValueError):
            pass
    try:
        request = urllib.request.Request(DETECTABLE_URL,
                                         headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        CACHE.mkdir(parents=True, exist_ok=True)
        DETECTABLE_CACHE.write_text(json.dumps(data))
        return data
    except (OSError, ValueError):
        try:
            return json.loads(DETECTABLE_CACHE.read_text())
        except (OSError, ValueError):
            return []


def executables_index(apps):
    """basename (no .exe) -> (app id, name), for matching running processes."""
    index = {}
    for app in apps:
        for executable in app.get("executables") or []:
            name = (executable.get("name") or "").replace("\\", "/").split("/")[-1]
            name = name.lower().removesuffix(".exe").removesuffix(".app").lstrip(">")
            if len(name) >= 4 and any(c.isalpha() for c in name):
                index.setdefault(name, (app["id"], app["name"]))
    return index


# words that show up as executables everywhere and would match the wrong game
GENERIC_NAMES = {
    "main", "game", "play", "start", "run", "home", "launcher", "launch",
    "update", "updater", "setup", "install", "installer", "client", "server",
    "player", "data", "test", "tools", "tool", "core", "base", "shell",
    "python", "python3", "bash", "node", "java", "electron", "wine", "wine64",
    "steam", "steamwebhelper", "containers", "default", "system", "desktop",
}

# where a real game is *not* going to live; keeps desktop apps out of the list
SYSTEM_PREFIXES = ("/usr/bin/", "/usr/lib/", "/usr/libexec/", "/usr/sbin/",
                   "/bin/", "/sbin/", "/opt/google/")


SHELL_NAMES = {"bash", "sh", "zsh", "dash", "fish", "ksh", "csh", "tcsh"}


def running_processes():
    """(argv0, full command line), skipping shell one-liners.

    A command typed in a terminal carries the very name being searched for, so
    counting it would suggest a game that is not running at all.
    """
    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = pathlib.Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue  # the process died mid-read
        argv = raw.decode("utf-8", "replace").split("\0")
        if not argv or not argv[0]:
            continue  # kernel thread
        if (os.path.basename(argv[0]) in SHELL_NAMES
                and any(a.startswith("-") and "c" in a for a in argv[1:2])):
            continue
        rows.append((argv[0], " ".join(argv).strip()))
    return rows


def process_names(argv0, args):
    """Candidate executable names for one process.

    Only two things count: the program actually being run, and any .exe named
    on the command line (which is how a game under Proton shows up). Matching
    loose words from the command line finds the wrong game far too often.
    """
    names = []
    if not argv0.startswith(SYSTEM_PREFIXES):
        names.append(argv0.replace("\\", "/").split("/")[-1].lower())
    for token in re.split(r"[\s]+", args):
        token = token.strip('"').replace("\\", "/")
        if token.lower().endswith(".exe"):
            names.append(token.split("/")[-1].lower().removesuffix(".exe"))
    return [n for n in names
            if len(n) >= 4 and any(c.isalpha() for c in n) and n not in GENERIC_NAMES]


def suggest_apps(known):
    """Running processes that Discord's catalog recognizes but we don't track."""
    index = executables_index(detectable_apps())
    known_lower = [k.lower() for k in known]
    seen, found = set(), []
    for argv0, args in running_processes():
        for name in process_names(argv0, args):
            hit = index.get(name)
            if not hit or hit[0] in seen:
                continue
            if any(name in existing or existing in name for existing in known_lower):
                continue
            seen.add(hit[0])
            found.append({"client_id": hit[0], "name": hit[1], "process": name})
    return sorted(found, key=lambda item: item["name"].lower())[:20]


class Tray:
    def __init__(self):
        self.daemon = load_daemon()
        self.config = read_config()
        self.detected = set()
        self.suggestions = []
        self.updating = False   # guards programmatic toggles

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
        self.menu = self.build_menu()
        self.indicator.set_menu(self.menu)

    def build_menu(self):
        menu = Gtk.Menu()

        self.status_item = Gtk.MenuItem(label="Checking…")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)

        self.showing_item = Gtk.MenuItem(label="")
        self.showing_item.set_sensitive(False)
        menu.append(self.showing_item)

        menu.append(Gtk.SeparatorMenuItem())

        apps = Gtk.MenuItem(label="Apps Discord can see")
        apps.set_submenu(self.build_apps_menu())
        menu.append(apps)

        appearance = Gtk.MenuItem(label="Appearance")
        appearance.set_submenu(self.build_appearance_menu())
        menu.append(appearance)

        daemon = Gtk.MenuItem(label="Daemon")
        daemon.set_submenu(self.build_daemon_menu())
        menu.append(daemon)

        config = Gtk.MenuItem(label="Configuration")
        config.set_submenu(self.build_config_menu())
        menu.append(config)

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
            if proc in games:
                games[proc] = {**games[proc], **game}
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

    def build_appearance_menu(self):
        menu = Gtk.Menu()

        shapes = Gtk.MenuItem(label="Icon shape")
        shape_menu = Gtk.Menu()
        group = []
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
        group = []
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

    # ------------------------------------------------------------- actions
    def on_toggle_service(self, *_):
        systemctl("stop" if service_active() else "start", SERVICE)
        self.refresh()

    def on_restart(self, *_):
        systemctl("restart", SERVICE)
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
        systemctl("restart", SERVICE)
        name = self.app_items.get(proc, (None, proc))[1]
        notify(f"{name} {'enabled' if item.get_active() else 'disabled'}.")
        self.refresh()

    def on_set_all(self, _item, value):
        self.config = read_config()
        games = self.config.setdefault("games", {})
        for proc, _label, _game, _custom in self.catalog():
            games.setdefault(proc, {})["enabled"] = value
        write_config(self.config)
        systemctl("restart", SERVICE)
        notify("All apps " + ("enabled." if value else "disabled."))
        self.refresh()

    def on_add_app(self, _item, found):
        self.config = read_config()
        games = self.config.setdefault("games", {})
        games[found["process"]] = {
            "client_id": found["client_id"],
            "details": found["name"],
            "state": "Playing",
            "large_text": found["name"],
            "enabled": True,
        }
        write_config(self.config)
        systemctl("restart", SERVICE)
        notify(f"{found['name']} added.")
        self.suggestions = [s for s in self.suggestions
                            if s["client_id"] != found["client_id"]]
        self.rebuild_menu()
        self.refresh()

    def on_remove_app(self, _item, proc):
        self.config = read_config()
        (self.config.get("games") or {}).pop(proc, None)
        write_config(self.config)
        systemctl("restart", SERVICE)
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
        known = set(self.builtin_games()) | set(self.config.get("games") or {})
        if force:
            detectable_apps(force=True)
        found = suggest_apps(known)
        GLib.idle_add(self.show_suggestions, found)

    def show_suggestions(self, found):
        self.suggestions = found
        self.rebuild_menu()
        self.refresh()
        return False

    def refresh(self):
        """Repaint status from systemd, then scan for running apps off-thread."""
        self.config = read_config()
        active = service_active()

        self.updating = True
        self.status_item.set_label("Daemon: running" if active else "Daemon: stopped")
        self.toggle_item.set_label("Stop daemon" if active else "Start daemon")
        self.boot_item.set_active(service_enabled())
        self.login_item.set_active(
            systemctl("is-enabled", TRAY_SERVICE).stdout.strip() == "enabled")
        games = self.builtin_games()
        for proc, (item, label) in getattr(self, "app_items", {}).items():
            item.set_active(self.app_enabled(proc, games.get(proc, {})))
            mark = "  •  detected" if proc in self.detected else ""
            item.set_label(f"{label}{mark}")
        self.updating = False

        self.apply_icon(active)
        threading.Thread(target=self.scan, daemon=True).start()
        return True  # keep the timer alive

    def scan(self):
        """Which enabled apps are open right now (pgrep/MPRIS, so off the UI)."""
        found = set()
        if self.daemon:
            games = self.builtin_games()
            for proc, _label, game, _custom in self.catalog():
                entry = games.get(proc, game)
                if not self.app_enabled(proc, entry):
                    continue
                try:
                    if self.daemon.running(entry.get("running", proc)):
                        found.add(proc)
                except Exception:
                    pass  # a detector may fail on its own; never take the tray down
        GLib.idle_add(self.show_detected, found)

    def show_detected(self, found):
        self.detected = found
        names = sorted(label for proc, (item, label) in self.app_items.items()
                       if proc in found)
        listed = ", ".join(names) if names else "nothing"
        if service_active():
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

"""Bandeja: o estado do Discord e o relatorio de problema sem dados pessoais.

Carrega a bandeja sem criar o icone (so as funcoes). Pula quando o GTK nao
esta instalado, como num CI sem PyGObject.
"""
import importlib.machinery
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    loader = importlib.machinery.SourceFileLoader("dgp_tray", str(ROOT / "discord-presence-tray.py"))
    spec = importlib.util.spec_from_loader("dgp_tray", loader)
    tray = importlib.util.module_from_spec(spec)
    loader.exec_module(tray)
except (ImportError, ValueError) as error:  # sem gi, Gtk ou AppIndicator
    tray, SKIP = None, str(error)
else:
    SKIP = ""


class FakeProc:
    def __init__(self, *names):
        self.names, self.comm = set(names), names[0] if names else ""


class FakeDaemon:
    def __init__(self, sockets=(), procs=()):
        self.sockets, self.procs = list(sockets), list(procs)

    def ipc_candidates(self):
        return self.sockets

    def processes(self):
        return self.procs


@unittest.skipIf(tray is None, f"bandeja sem GTK: {SKIP}")
class DiscordState(unittest.TestCase):
    def test_ready(self):
        kind, text, help_text = tray.discord_state(
            FakeDaemon(["/run/user/1000/discord-ipc-0"], [FakeProc("vesktop")]))
        self.assertEqual((kind, text, help_text), ("ok", "Discord: Vesktop ready", None))

    def test_vesktop_without_rich_presence(self):
        kind, text, help_text = tray.discord_state(FakeDaemon([], [FakeProc("vesktop")]))
        self.assertEqual(kind, "off")
        self.assertIn("Vesktop", text)
        self.assertIn("Rich Presence", help_text)

    def test_nothing_open(self):
        kind, text, _help = tray.discord_state(FakeDaemon([], [FakeProc("bash")]))
        self.assertEqual((kind, text), ("closed", "⚠  Discord not found"))

    def test_canary_is_not_plain_discord(self):
        _kind, text, _help = tray.discord_state(FakeDaemon([], [FakeProc("discordcanary")]))
        self.assertIn("Canary", text)


@unittest.skipIf(tray is None, f"bandeja sem GTK: {SKIP}")
class CleanLog(unittest.TestCase):
    def test_titles_links_paths_and_quotes_go(self):
        home = str(tray.HOME)
        lines = tray.clean_log([
            "[19:05:10] publicou YouTube: My secret video | Channel [desde 19:05]",
            "[19:05:11] publicou Grand Theft Auto: San Andreas: Ganton | 100 HP",
            "[19:05:12] publicou Some App I Renamed: text",
            f"[19:06:00] CS2: instalado em {home}/games/cfg",
            "[19:07:00] o Discord recusou Roblox: \"Request terminated\" https://a.b/c",
            "",
        ], ["YouTube", "Grand Theft Auto: San Andreas"])
        self.assertEqual(lines[0], "[19:05:10] publicou YouTube: (text hidden)")
        self.assertEqual(lines[1],
                         "[19:05:11] publicou Grand Theft Auto: San Andreas: (text hidden)")
        self.assertEqual(lines[2], "[19:05:12] publicou (an app): (text hidden)")
        self.assertEqual(lines[3], "[19:06:00] CS2: instalado em ~/games/cfg")
        self.assertNotIn("Request terminated", lines[4])
        self.assertNotIn("https://", lines[4])
        self.assertEqual(len(lines), 5)

    def test_app_id_pattern(self):
        self.assertTrue(tray.APP_ID.fullmatch("1538427046760423474"))
        self.assertFalse(tray.APP_ID.fullmatch("12345"))
        self.assertFalse(tray.APP_ID.fullmatch("1538427046760423474x"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(tray is None, f"bandeja sem GTK: {SKIP}")
class MenuEntry(unittest.TestCase):
    """A instalacao por script ganha uma entrada no menu; pacote nao."""

    def setUp(self):
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp)
        saved = {n: getattr(tray, n) for n in ("MENU_ENTRY", "MENU_ICON", "SYSTEM_ENTRIES")}
        self.addCleanup(lambda: [setattr(tray, k, v) for k, v in saved.items()])
        tray.MENU_ENTRY = self.tmp / "applications/discord-presence-tray.desktop"
        tray.MENU_ICON = self.tmp / "icons/discord-game-presence.svg"
        tray.SYSTEM_ENTRIES = [self.tmp / "usr/discord-presence-tray.desktop"]
        self.script = tray.HOME / ".local/bin/discord-presence-tray.py"

    def test_script_install_gets_entry_and_icon(self):
        self.assertEqual(tray.ensure_menu_entry(self.script), tray.MENU_ENTRY)
        text = tray.MENU_ENTRY.read_text()
        self.assertIn("Exec=systemctl --user start discord-game-presence-tray.service", text)
        self.assertIn(f"Icon={tray.MENU_ICON}", text)
        self.assertTrue(tray.MENU_ICON.read_text().startswith("<svg"))
        self.assertIsNone(tray.ensure_menu_entry(self.script))  # ja esta igual

    def test_package_or_clone_gets_nothing(self):
        self.assertIsNone(tray.ensure_menu_entry("/usr/bin/discord-presence-tray"))
        tray.SYSTEM_ENTRIES[0].parent.mkdir(parents=True)
        tray.SYSTEM_ENTRIES[0].write_text("x")
        self.assertIsNone(tray.ensure_menu_entry(self.script))
        self.assertFalse(tray.MENU_ENTRY.exists())

    def test_notifications_carry_the_app_icon(self):
        tray.ensure_menu_entry(self.script)
        command = tray.notify_command()
        self.assertEqual(command[command.index("-i") + 1], str(tray.MENU_ICON))
        self.assertIn("string:desktop-entry:discord-presence-tray", command)
        self.assertEqual(tray.notify_command("dialog-error")[4], "dialog-error")

"""Controle do que cada app mostra, pausa, arquivo de estado e sugestoes.

So biblioteca padrao (e GTK, nos testes da bandeja, que pulam sem ele). Nada
fala com o Discord, o D-Bus ou a rede: o config, o cache da base do Discord e
o arquivo de estado ficam numa pasta temporaria.
"""
import importlib.machinery
import importlib.util
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DAEMON = ROOT / "discord-game-presence.py"
WATCH = "https://www.youtube.com/watch?v="


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dgp = load("dgp_options", DAEMON)


def patch(test, module, **values):
    saved = {name: getattr(module, name) for name in values}
    test.addCleanup(lambda: [setattr(module, k, v) for k, v in saved.items()])
    for name, value in values.items():
        setattr(module, name, value)


def entry_options(test, key, **values):
    """Troca campos de uma entrada do catalogo so durante o teste."""
    game = dgp.GAMES[key]
    saved = {k: game[k] for k in values if k in game}

    def restore():
        for k in values:
            game.pop(k, None)
        game.update(saved)
    test.addCleanup(restore)
    game.update(values)


def media(url, length=60_000_000, status="Playing", ad=False):
    return {"player": "p", "url": url, "video": dgp.youtube_video_id(url),
            "status": status, "ad": ad, "pending": False, "tab": None,
            "meta": {"xesam:title": "Lofi", "xesam:artist": ["Lofi Girl"],
                     "mpris:length": length}}


class Fakes(unittest.TestCase):
    def setUp(self):
        patch(self, dgp,
              youtube_oembed=lambda video: {"title": "Lofi", "author_name": "Lofi Girl",
                                            "author_url": "https://www.youtube.com/@LofiGirl"},
              video_thumbnail=lambda video: f"https://i.ytimg.com/vi/{video}/hqdefault.jpg",
              mpris_position=lambda player, tries=3: 10_000_000)


class DetailLevels(Fakes):
    def card(self, start=1_700_000_000, **options):
        game = {**dgp.GAMES["youtube"], **options}
        state = dgp.video_state(media(WATCH + "AAAAAAAAAAA"))
        return dgp.build_activity(game, state, start)

    def test_everything_by_default(self):
        act = self.card()
        self.assertEqual((act["details"], act["state"]), ("Lofi", "Lofi Girl"))
        self.assertIn("buttons", act)
        self.assertIn("details_url", act)
        self.assertIn("large_url", act["assets"])
        self.assertTrue(act["assets"]["large_image"].startswith("https://i.ytimg.com/"))
        self.assertIn("end", act["timestamps"])

    def test_basic_has_nothing_clickable(self):
        act = self.card(detail="basic")
        self.assertEqual((act["details"], act["state"]), ("Lofi", "Lofi Girl"))
        for key in ("buttons", "details_url", "state_url"):
            self.assertNotIn(key, act)
        self.assertNotIn("large_url", act["assets"])
        self.assertTrue(act["assets"]["large_image"].startswith("https://i.ytimg.com/"))
        self.assertIn("end", act["timestamps"])

    def test_minimal_is_only_the_app(self):
        act = self.card(detail="minimal", start=1_700_000_000)
        self.assertEqual(act["details"], "YouTube")
        self.assertNotIn("state", act)
        self.assertEqual(act["assets"], {"large_image": dgp.YT_ICON, "large_text": "YouTube"})
        self.assertEqual(act["timestamps"], {"start": 1_700_000_000})  # nao a barra do video
        self.assertEqual(act["status_display_type"], 0)
        self.assertNotIn("buttons", act)

    def test_single_switches_win_over_the_level(self):
        act = self.card(detail="basic", show_buttons=True)
        self.assertIn("buttons", act)
        self.assertNotIn("details_url", act)
        act = self.card(show_thumbnail=False)
        self.assertEqual(act["assets"]["large_image"], dgp.YT_ICON)
        self.assertNotIn("small_image", act["assets"])
        self.assertIn("details_url", act)
        self.assertNotIn("timestamps", self.card(show_time=False))
        self.assertIn("buttons", self.card(detail="nonsense"))  # nivel invalido: tudo
        self.assertIn("buttons", self.card(show_buttons="no"))  # so booleano vale

    def test_games_with_live_details(self):
        game = {"client_id": "1", "details": lambda: "Bed Wars", "state": "x",
                "large_text": "Roblox", "detail": "minimal"}
        act = dgp.build_activity(game, ("12 jogadores", [3, 12]), 1_700_000_000)
        self.assertNotIn("details", act)
        self.assertNotIn("state", act)
        self.assertNotIn("party", act)
        self.assertEqual(act["timestamps"], {"start": 1_700_000_000})
        self.assertEqual(act["assets"]["large_image"], "game")


class MediaOptions(Fakes):
    def test_defaults_and_bad_values(self):
        self.assertEqual(dgp.media_option("youtube", "pause_minutes"), 5.0)
        self.assertTrue(dgp.media_option("ytm", "show_ads"))
        self.assertFalse(dgp.media_option("youtube", "show_search"))
        for bad in ("x", -1, True, None):
            entry_options(self, "youtube", pause_minutes=bad)
            self.assertEqual(dgp.media_option("youtube", "pause_minutes"), 5.0)
        entry_options(self, "youtube", pause_minutes=0, show_ads="no")
        self.assertEqual(dgp.media_option("youtube", "pause_minutes"), 0.0)
        self.assertTrue(dgp.media_option("youtube", "show_ads"))
        self.assertEqual(dgp.media_option("nao-existe", "browse_minutes"), 15.0)

    def test_pause_grace(self):
        self.assertEqual(dgp.pause_grace("stremio"), 300)
        entry_options(self, "youtube-music", pause_minutes=1)
        self.assertEqual(dgp.pause_grace("ytm"), 60)
        entry_options(self, "youtube-music", show_paused=False)
        self.assertEqual(dgp.pause_grace("ytm"), 0)

    def test_idle_right_away_without_grace(self):
        status = {"value": "Paused"}
        patch(self, dgp, mpris=lambda prop, player=None: status["value"])
        dgp._PAUSED_SINCE.pop("p", None)
        self.assertTrue(dgp.media_idle("p", 0))
        self.assertFalse(dgp.media_idle("p", 300))
        status["value"] = "Playing"
        self.assertFalse(dgp.media_idle("p", 0))

    def test_hidden_ads_on_youtube(self):
        entry_options(self, "youtube", show_ads=False)
        text, extra = dgp.video_state(media(WATCH + "AAAAAAAAAAA", 15_000_000, ad=True))
        self.assertEqual((text, extra["details"]), ("Lofi Girl", "Lofi"))
        self.assertIsNone(extra["timestamps"])
        self.assertEqual(extra["state_url"], "https://www.youtube.com/@LofiGirl")

    def test_hidden_ads_on_music(self):
        entry_options(self, "youtube-music", show_ads=False)
        ad = media("https://music.youtube.com/watch?v=BBBBBBBBBBB", 15_000_000, ad=True)
        text, extra = dgp.song_state(ad)
        self.assertEqual((text, extra["details"]), ("Lofi Girl", "Lofi"))
        self.assertNotEqual(extra["details"], "Anúncio")
        patch(self, dgp, youtube_oembed=lambda video: {})
        before = ("Artista", {"details": "Faixa anterior"})
        patch(self, dgp, _LAST_STATE={"ytm": before})
        self.assertEqual(dgp.song_state(ad), before)
        entry_options(self, "youtube-music", show_ads=True)
        self.assertEqual(dgp.song_state(ad)[1]["details"], "Anúncio")

    def test_browsing_can_be_left_out(self):
        tab = dgp.Tab(("x", 1), 1, 0, True, "https://www.youtube.com/", "YouTube")
        patch(self, dgp, media_player=lambda service: None,
              browsing=lambda service: (tab, "YouTube"))
        dgp._BROWSE.pop("youtube", None)
        self.assertEqual(dgp.service_now("youtube")[0], "navegando")
        entry_options(self, "youtube", show_browsing=False)
        self.assertIsNone(dgp.service_now("youtube"))
        self.assertIsNone(dgp.yt_running())

    def test_browse_minutes(self):
        tab = dgp.Tab(("x", 1), 1, 0, True, "https://www.youtube.com/", "YouTube")
        dgp._BROWSE.pop("youtube", None)
        entry_options(self, "youtube", browse_minutes=0.0001)  # 6 ms
        self.assertIsNotNone(dgp.browse_since("youtube", tab))
        time.sleep(0.02)
        self.assertIsNone(dgp.browse_since("youtube", tab))


class LiveReload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = pathlib.Path(self.dir.name) / "config.json"
        patch(self, dgp, CONFIG=self.config)
        saved = {key: dict(game) for key, game in dgp.GAMES.items()}

        def restore():
            for key, game in dgp.GAMES.items():
                game.clear()
                game.update(saved[key])
        self.addCleanup(restore)

    def test_options_change_without_a_restart(self):
        self.config.write_text(json.dumps({"games": {"youtube": {"detail": "minimal"}}}))
        dgp.load_config()
        self.assertEqual(dgp.GAMES["youtube"]["detail"], "minimal")
        game = dgp.GAMES["youtube"]  # o mesmo dict que a sessao segura
        before = dgp.RELOADS[0]
        self.config.write_text(json.dumps({"games": {
            "youtube": {"show_ads": False, "status_display": "state",
                        "client_id": "outro", "exe": "nao-muda"},
            "libexec/stremio/stremio": {"pause_minutes": 1}}}))
        dgp.reload_options()
        self.assertIs(dgp.GAMES["youtube"], game)
        self.assertNotIn("detail", game)  # saiu do config: volta ao padrao
        self.assertFalse(game["show_ads"])
        self.assertEqual(game["status_display"], "state")
        self.assertEqual(game["client_id"], "1538427046760423474")  # so opcoes de exibicao
        self.assertNotIn("exe", game)
        self.assertEqual(dgp.GAMES["stremio"]["pause_minutes"], 1)  # chave antiga
        self.assertEqual(dgp.RELOADS[0], before + 1)
        self.config.write_text("{}")
        dgp.reload_options()
        self.assertEqual(game["status_display"], "details")  # o padrao da entrada
        self.assertFalse(game["show_search"])


class WholeWordArguments(unittest.TestCase):
    def test_has_argument(self):
        self.assertTrue(dgp.has_argument("-steam -game hl2 -w 1920", "-game hl2"))
        self.assertTrue(dgp.has_argument("-game hl2", "-game hl2"))
        self.assertFalse(dgp.has_argument("-game hl2mp", "-game hl2"))
        self.assertFalse(dgp.has_argument("x-game hl2", "-game hl2"))
        self.assertFalse(dgp.has_argument("", "-game hl2"))

    def test_catalog_needs_the_whole_argument(self):
        apps = [{"id": "1", "name": "Half-Life 2",
                 "executables": [{"name": "hl2.exe", "os": "win32", "arguments": "-game hl2"}]},
                {"id": "2", "name": "HL2 Deathmatch",
                 "executables": [{"name": "hl2.exe", "os": "win32", "arguments": "-game hl2mp"}]}]
        db = dgp.detectable_index(apps)
        path = ["C:\\Games\\hl2.exe"]
        self.assertEqual(dgp.match_detectable(path, "-game hl2mp", db)[0]["id"], "2")
        self.assertEqual(dgp.match_detectable(path, "-game hl2 -console", db)[0]["id"], "1")


class Pause(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        config = pathlib.Path(self.dir.name) / "config.json"
        status = pathlib.Path(self.dir.name) / "run" / "status.json"
        patch(self, dgp, CONFIG=config, status_path=lambda: status)
        self.config, self.status = config, status
        self.addCleanup(dgp.STOPPING.clear)
        self.addCleanup(dgp.WAKE.clear)

    def write(self, data):
        self.config.write_text(json.dumps(data))

    def test_paused_until(self):
        self.assertIsNone(dgp.paused_until())  # sem config
        self.write({"pause": {"until": None}})
        self.assertEqual(dgp.paused_until(), math.inf)
        future = time.time() + 600
        self.write({"pause": {"until": future}})
        self.assertAlmostEqual(dgp.paused_until(), future)
        for gone in ({"pause": {"until": time.time() - 1}}, {"pause": {"until": True}},
                     {"pause": "sim"}, {}, []):
            self.assertIsNone(dgp.paused_until(gone))

    def test_waits_until_the_end(self):
        self.write({"pause": {"until": time.time() + 1.2}})
        started = time.monotonic()
        self.assertTrue(dgp.wait_pause())
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertFalse(json.loads(self.status.read_text())["paused"])

    def test_resume_from_the_tray(self):
        self.write({"pause": {"until": None}})
        threading.Timer(0.3, lambda: self.write({})).start()
        dgp.STOPPING.clear()
        started = time.monotonic()
        self.assertTrue(dgp.wait_pause())  # releu o config na volta seguinte
        self.assertLess(time.monotonic() - started, dgp.PAUSE_CHECK + 2)

    def test_stop_while_paused(self):
        self.write({"pause": {"until": None}})
        seen = {}

        def stop():
            seen["status"] = json.loads(self.status.read_text())
            dgp.STOPPING.set()
            dgp.WAKE.set()
        threading.Timer(0.3, stop).start()
        self.assertFalse(dgp.wait_pause())
        self.assertTrue(seen["status"]["paused"])
        self.assertIsNone(seen["status"]["paused_until"])


class StatusFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = pathlib.Path(self.dir.name) / "run" / "status.json"
        patch(self, dgp, status_path=lambda: self.path,
              _STATUS={"showing": {}, "stale": [], "paused": False, "paused_until": None,
                       "stopped": False})

    def test_showing_and_cleanup(self):
        game = {"large_text": "YouTube", "client_id": "42"}
        dgp.status_showing("youtube", game, {"details": "Lofi", "state": "Lofi Girl"})
        data = json.loads(self.path.read_text())
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["showing"]["youtube"]["name"], "YouTube")
        self.assertEqual(data["showing"]["youtube"]["client_id"], "42")
        self.assertEqual(oct(self.path.parent.stat().st_mode & 0o777), "0o700")
        dgp.status_showing("youtube", game)
        data = json.loads(self.path.read_text())
        self.assertEqual((data["showing"], data["stale"]), ({}, []))

    def test_a_session_that_could_not_clear_is_remembered(self):
        game = {"large_text": "YouTube", "client_id": "42"}
        dgp.status_showing("youtube", game, {"details": "x"})
        dgp.status_showing("youtube", game, stale=True)
        self.assertEqual(json.loads(self.path.read_text())["stale"], ["42"])
        dgp.status_showing("youtube", game, {"details": "x"})  # outra sessao
        dgp.status_showing("youtube", game)  # essa apagou
        self.assertEqual(json.loads(self.path.read_text())["stale"], [])

    def test_leftovers_to_clear_at_start(self):
        everything = ["1", "2", "3"]
        self.assertEqual(dgp.leftover_ids(everything), everything)  # boot, versao antiga
        self.path.parent.mkdir(parents=True, exist_ok=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.path.write_text(json.dumps({"pid": dead.pid, "stopped": True, "showing": {},
                                         "stale": []}))
        self.assertEqual(dgp.leftover_ids(everything), [])  # saiu limpo: nada a apagar
        self.path.write_text(json.dumps({"pid": dead.pid, "stopped": False, "stale": ["3"],
                                         "showing": {"a": {"client_id": "2"}, "b": "lixo"}}))
        self.assertEqual(dgp.leftover_ids(everything), ["2", "3"])  # caiu com isso no ar
        alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "discord-game-presence"])
        self.addCleanup(alive.wait)
        self.addCleanup(alive.kill)
        # right after fork the child still has the test runner's cmdline: wait for
        # the exec (lost this race once under load in the distro matrix)
        deadline = time.monotonic() + 5
        while not dgp.daemon_alive(alive.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.path.write_text(json.dumps({"pid": alive.pid, "showing": {"a": {"client_id": "2"}}}))
        self.assertEqual(dgp.leftover_ids(everything), [])  # outro daemon rodando
        self.path.write_text("{quebrado")
        self.assertEqual(dgp.leftover_ids(everything), everything)

    def test_threads_do_not_lose_updates(self):
        game = {"large_text": "x", "client_id": "1"}
        threads = [threading.Thread(target=dgp.status_showing,
                                    args=(f"app{i}", game, {"details": "d"})) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(json.loads(self.path.read_text())["showing"]), 20)


CATALOG = [
    {"id": "900000000000000001", "name": "Fake Quest", "icon_hash": "abc",
     "executables": [{"name": "fakequest.exe", "os": "win32", "is_launcher": False}]},
    {"id": "900000000000000002", "name": "Known Game",
     "executables": [{"name": "knowngame.exe", "os": "win32", "is_launcher": False}]},
    {"id": "900000000000000003", "name": "Wine Explorer",
     "executables": [{"name": "windows/explorer.exe", "os": "win32", "is_launcher": False}]},
]


class Suggestions(unittest.TestCase):
    """O mesmo caminho que a bandeja usa: --suggest num processo a parte."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cache = pathlib.Path(cls.dir.name) / "discord-game-presence"
        cache.mkdir()
        (cache / "detectable.json").write_text(json.dumps(CATALOG))
        games = pathlib.Path(cls.dir.name) / "games"
        games.mkdir()
        # processos com o argv[0] de um jogo nativo e de servicos do Wine
        cls.procs = [subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 60"])
                     for argv0 in (games / "FakeQuest.x86_64", games / "KnownGame.x86_64",
                                   "C:\\\\windows\\\\explorer.exe")]
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            proc.kill()
            proc.wait()
        cls.dir.cleanup()

    def run_helper(self, request):
        env = {**os.environ, "XDG_CACHE_HOME": self.dir.name}
        done = subprocess.run([sys.executable, "-I", str(DAEMON), "--suggest"],
                              input=json.dumps(request), capture_output=True, text=True,
                              env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_helper_process(self):
        found = self.run_helper({"known_ids": [], "known_keys": ["knowngame.x86_64"]})
        names = [item["name"] for item in found]
        self.assertIn("Fake Quest", names)
        self.assertNotIn("Known Game", names)  # ja esta no catalogo
        self.assertNotIn("Wine Explorer", names)  # servico do Wine, nao jogo
        quest = next(item for item in found if item["name"] == "Fake Quest")
        self.assertEqual((quest["process"], quest["icon"]), ("fakequest.x86_64", "abc"))
        found = self.run_helper({"known_ids": ["900000000000000001"], "known_keys": []})
        self.assertEqual([item["name"] for item in found], ["Known Game"])

    def test_wine_system_paths(self):
        self.assertEqual(dgp.process_paths(dgp.Proc(1, ["C:\\windows\\system32\\explorer.exe"])),
                         [])
        self.assertEqual(dgp.process_paths(dgp.Proc(1, ["Z:\\Games\\Jogo.exe"])),
                         ["Z:\\Games\\Jogo.exe"])

    def test_tray_uses_the_helper(self):
        try:
            tray = load("tray_options", ROOT / "discord-presence-tray.py")
        except (ImportError, ValueError) as error:
            self.skipTest(f"sem GTK/AppIndicator: {error}")
        os.environ["XDG_CACHE_HOME"] = self.dir.name
        self.addCleanup(os.environ.pop, "XDG_CACHE_HOME")
        found = tray.suggest_apps(dgp, set(), {"knowngame.x86_64"})
        self.assertEqual([item["name"] for item in found], ["Fake Quest"])

    def test_tray_reads_the_status_of_a_live_daemon(self):
        try:
            tray = load("tray_status", ROOT / "discord-presence-tray.py")
        except (ImportError, ValueError) as error:
            self.skipTest(f"sem GTK/AppIndicator: {error}")
        path = pathlib.Path(self.dir.name) / "status.json"
        fake = type("Daemon", (), {"status_path": staticmethod(lambda: path)})
        daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                   "discord-game-presence"])
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.kill)
        path.write_text(json.dumps({"pid": daemon.pid, "showing": {"youtube": {}}}))
        self.assertEqual(set(tray.daemon_status(fake)["showing"]), {"youtube"})
        path.write_text(json.dumps({"pid": os.getpid(), "showing": {}}))  # nao e o daemon
        self.assertIsNone(tray.daemon_status(fake))
        path.write_text("{quebrado")
        self.assertIsNone(tray.daemon_status(fake))

    def test_tray_reloads_a_live_daemon_instead_of_restarting(self):
        try:
            tray = load("tray_reload", ROOT / "discord-presence-tray.py")
        except (ImportError, ValueError) as error:
            self.skipTest(f"sem GTK/AppIndicator: {error}")
        path = pathlib.Path(self.dir.name) / "status.json"
        got = pathlib.Path(self.dir.name) / "got"
        fake = type("Daemon", (), {"status_path": staticmethod(lambda: path)})
        restarts = []
        patch(self, tray, restart_daemon=lambda: restarts.append(1),
              service_active=lambda: True)
        code = ("import signal, sys, time\n"
                "def hup(*_):\n"
                "    open(sys.argv[1], 'w').write('hup')\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGHUP, hup)\n"
                "print('pronto', flush=True)\n"
                "time.sleep(30)\n")
        daemon = subprocess.Popen([sys.executable, "-c", code, str(got), "discord-game-presence"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.kill)
        self.addCleanup(daemon.stdout.close)
        self.assertEqual(daemon.stdout.readline().strip(), "pronto")
        path.write_text(json.dumps({"pid": daemon.pid, "showing": {}, "reload": False}))
        self.assertFalse(tray.reload_daemon(fake))  # daemon antigo: reinicia
        self.assertEqual(restarts, [1])
        path.write_text(json.dumps({"pid": daemon.pid, "showing": {}, "reload": True}))
        self.assertTrue(tray.reload_daemon(fake))
        daemon.wait(5)
        self.assertEqual(got.read_text(), "hup")
        self.assertEqual(restarts, [1])  # sem reinicio


if __name__ == "__main__":
    unittest.main()

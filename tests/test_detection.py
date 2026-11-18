#!/usr/bin/env python3
"""Testes do daemon: python3 -m unittest discover -s tests

So biblioteca padrao. Nada aqui fala com o Discord de verdade, com o D-Bus
ou com a rede: processos, sockets e historicos sao montados na hora.
"""
import importlib.machinery
import importlib.util
import json
import pathlib
import socket
import sqlite3
import struct
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_daemon():
    loader = importlib.machinery.SourceFileLoader(
        "dgp", str(ROOT / "discord-game-presence.py"))
    spec = importlib.util.spec_from_loader("dgp", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dgp = load_daemon()
Proc = dgp.Proc


def matches(key, game, proc):
    return any(dgp.proc_matches(proc, r) for r in dgp.entry_rules(key, game))


class ProcessRules(unittest.TestCase):
    def test_wine_game_matches_by_windows_argv0(self):
        game = Proc(1, [r"S:\common\Task Bar Hero\TaskBarHero.exe"],
                    comm="TaskBarHero.exe", exe="/usr/bin/wine64-preloader")
        self.assertTrue(matches("TaskBarHero.exe", {}, game))

    def test_proton_wrapper_is_not_the_game(self):
        wrapper = Proc(2, ["python3", "/steam/Proton/proton", "waitforexitandrun",
                           "/games/Task Bar Hero/TaskBarHero.exe"], comm="python3")
        reaper = Proc(3, ["/steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=1",
                          "--", "/games/Task Bar Hero/TaskBarHero.exe"], comm="reaper")
        self.assertFalse(matches("TaskBarHero.exe", {}, wrapper))
        self.assertFalse(matches("TaskBarHero.exe", {}, reaper))

    def test_single_string_command_line(self):
        game = Proc(4, [r"C:\Games\Foo Bar\foo.exe -windowed -nosound"], comm="foo.exe")
        self.assertTrue(matches("foo.exe", {}, game))
        self.assertTrue(matches("foo", {}, game))

    def test_search_and_editors_do_not_count(self):
        grep = Proc(5, ["grep", "-r", "rs2client", "/home/u"])
        tail = Proc(6, ["tail", "-f", "/home/u/Jagex/rs2client.log"])
        real = Proc(7, ["/home/u/.local/share/bolt/rs2client"], comm="rs2client")
        self.assertFalse(matches("rs2client", {}, grep))
        self.assertFalse(matches("rs2client", {}, tail))
        self.assertTrue(matches("rs2client", {}, real))

    def test_legacy_cmdline_key_still_works_but_ignores_viewers(self):
        key = r"S:\common\Jogo"
        game = Proc(8, [r"S:\common\Jogo\bin\x.exe"])
        viewer = Proc(9, ["less", r"S:\common\Jogo\log.txt"])
        self.assertTrue(matches(key, {}, game))
        self.assertFalse(matches(key, {}, viewer))

    def test_left4dead2_native_proton_and_not_server(self):
        entry = dgp.GAMES["left4dead2"]
        native = Proc(10, ["/games/Left 4 Dead 2/hl2_linux", "-game", "left4dead2",
                           "-steam"], comm="hl2_linux")
        proton = Proc(11, [r"S:\common\Left 4 Dead 2\left4dead2.exe"], comm="left4dead2.exe")
        hl2 = Proc(12, ["/games/Half-Life 2/hl2_linux", "-game", "hl2"], comm="hl2_linux")
        server = Proc(13, ["/srv/l4d2/srcds_linux", "-game", "left4dead2"], comm="srcds_linux")
        editor = Proc(14, ["kate", "/games/Left 4 Dead 2/left4dead2/cfg/autoexec.cfg"])
        self.assertTrue(matches("left4dead2", entry, native))
        self.assertTrue(matches("left4dead2", entry, proton))
        self.assertFalse(matches("left4dead2", entry, hl2))
        self.assertFalse(matches("left4dead2", entry, server))
        self.assertFalse(matches("left4dead2", entry, editor))

    def test_old_tray_key_without_exe_suffix(self):
        game = Proc(15, [r"Z:\games\ELDEN RING\Game\eldenring.exe"], comm="eldenring.exe")
        self.assertTrue(matches("eldenring", {"client_id": "1"}, game))

    def test_interpreter_scripts(self):
        script = Proc(16, ["/bin/bash", "/games/Left 4 Dead 2/hl2.sh", "-game", "left4dead2"])
        self.assertIn("hl2.sh", script.names)
        self.assertIn("hl2", script.names)
        jar = Proc(17, ["java", "-Xmx2G", "-jar", "/games/Starbound.jar"])
        self.assertIn("starbound.jar", jar.names)

    def test_truncated_comm(self):
        proc = Proc(18, ["/usr/bin/wine64-preloader"], comm="averyveryverylo")
        self.assertTrue(matches("AVeryVeryVeryLongGame.exe", {}, proc))
        self.assertFalse(matches("averyshort.exe", {}, proc))

    def test_sober_counts_as_roblox(self):
        entry = dgp.GAMES["roblox-player:"]
        sober = Proc(19, ["/app/bin/sober"], comm="sober")
        link = Proc(20, ["/usr/bin/flatpak", "run", "org.vinegarhq.Sober",
                         "roblox-player:1+launchmode:play"])
        opener = Proc(21, ["xdg-open", "roblox-player:1+launchmode:play"])
        self.assertTrue(matches("roblox-player:", entry, sober))
        self.assertTrue(matches("roblox-player:", entry, link))
        self.assertFalse(matches("roblox-player:", entry, opener))

    def test_explicit_rules_in_config(self):
        game = {"exe": ["game.x86_64"], "args": "--profile=main"}
        hit = Proc(22, ["/g/game.x86_64", "--profile=main"])
        miss = Proc(23, ["/g/game.x86_64", "--profile=test"])
        self.assertTrue(matches("anything", game, hit))
        self.assertFalse(matches("anything", game, miss))

    def test_detect_and_start_time(self):
        procs = [Proc(30, ["/g/rs2client"], comm="rs2client"),
                 Proc(31, ["grep", "rs2client"])]
        procs[0]._start = 1000.0
        original = dgp.processes
        dgp.processes = lambda: procs
        try:
            hit = dgp.detect("rs2client", dgp.GAMES["rs2client"])
        finally:
            dgp.processes = original
        self.assertEqual([p.pid for p in hit], [30])
        self.assertEqual(dgp.hit_start(hit), 1000.0)
        self.assertIsNone(dgp.hit_start(True))  # bool nao e pid

    def test_real_proc_snapshot_has_this_process_tree(self):
        procs = dgp.processes()
        self.assertTrue(procs)
        self.assertTrue(all(p.argv and p.argv[0] for p in procs))
        start = dgp.proc_start(procs[0].pid)
        self.assertTrue(start is None or start > 1e9)


class YouTube(unittest.TestCase):
    def test_video_ids(self):
        vid = "dQw4w9WgXcQ"
        for url in (f"https://www.youtube.com/watch?v={vid}&t=10",
                    f"https://m.youtube.com/watch?v={vid}",
                    f"https://youtu.be/{vid}?si=x",
                    f"https://www.youtube.com/shorts/{vid}",
                    f"https://www.youtube.com/live/{vid}?feature=share",
                    f"https://www.youtube-nocookie.com/embed/{vid}",
                    f"https://music.youtube.com/watch?v={vid}&list=RD"):
            self.assertEqual(dgp.youtube_video_id(url), vid, url)
        for url in ("https://www.youtube.com/@canal", "https://example.com/watch?v=" + vid,
                    "", "nada"):
            self.assertIsNone(dgp.youtube_video_id(url), url)

    def test_services(self):
        self.assertEqual(dgp.media_service("https://music.youtube.com/watch?v=x"), "ytm")
        self.assertEqual(dgp.media_service("https://www.youtube.com/watch?v=x"), "youtube")
        self.assertEqual(dgp.media_service("https://youtu.be/x"), "youtube")
        self.assertIsNone(dgp.media_service("https://notyoutube.com/x"))
        self.assertIsNone(dgp.media_service("https://www.twitch.tv/x"))

    def test_page_title(self):
        self.assertTrue(dgp.page_title_is("(3) Video - YouTube", "Video"))
        self.assertTrue(dgp.page_title_is("Song | YouTube Music", "Song"))
        self.assertFalse(dgp.page_title_is("Best Video Ever - YouTube", "Video"))

    def make_history(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "Profile 2" / "History"
        path.parent.mkdir()
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,"
                   " visit_count INTEGER, last_visit_time INTEGER)")
        now = int((time.time() + dgp.WEBKIT_EPOCH) * 1_000_000)
        for url, title, age in rows:
            db.execute("INSERT INTO urls (url, title, last_visit_time) VALUES (?,?,?)",
                       (url, title, now - int(age * 1_000_000)))
        db.commit()
        db.close()
        dgp._HISTORY_CACHE.clear()
        return [path]

    def test_history_prefers_exact_title(self):
        files = self.make_history([
            ("https://www.youtube.com/watch?v=AAAAAAAAAAA", "Best Intro Ever - YouTube", 10),
            ("https://www.youtube.com/watch?v=BBBBBBBBBBB", "(2) Intro - YouTube", 300),
            ("https://example.com/intro", "Intro", 1),
        ])
        url = dgp.history_url("Intro", files)
        self.assertEqual(dgp.youtube_video_id(url), "BBBBBBBBBBB")

    def test_history_ignores_old_visits(self):
        files = self.make_history([
            ("https://www.youtube.com/watch?v=CCCCCCCCCCC", "Old - YouTube", 3 * 86400)])
        self.assertEqual(dgp.history_url("Old", files), "")

    def test_history_music(self):
        files = self.make_history([
            ("https://music.youtube.com/watch?v=DDDDDDDDDDD&list=RD", "Song | YouTube Music", 5)])
        url = dgp.history_url("Song", files)
        self.assertEqual(dgp.media_service(url), "ytm")


class Activity(unittest.TestCase):
    def test_sanitize(self):
        out = dgp.sanitize_activity({
            "type": 0, "details": "x" * 300, "state": "a",
            "timestamps": {"start": 200, "end": 100},
            "assets": {"large_image": "https://i/x.png", "large_text": "  "},
            "buttons": [{"label": "L" * 40, "url": "https://a"},
                        {"label": "bad", "url": "javascript:1"},
                        {"label": "ok", "url": "https://b"},
                        {"label": "extra", "url": "https://c"}],
            "party": {"id": "1", "size": [0, 10]},
        })
        self.assertEqual(len(out["details"]), 128)
        self.assertNotIn("state", out)
        self.assertEqual(out["timestamps"], {"start": 200})
        self.assertEqual(out["assets"], {"large_image": "https://i/x.png"})
        self.assertEqual([b["url"] for b in out["buttons"]], ["https://a", "https://b"])
        self.assertEqual(len(out["buttons"][0]["label"]), 32)
        self.assertNotIn("party", out)

    def test_build_uses_process_start(self):
        game = {"client_id": "1", "details": "Jogo", "large_text": "Jogo"}
        act = dgp.build_activity(game, ("Jogando", None), 1234.9)
        self.assertEqual(act["timestamps"], {"start": 1234})
        act = dgp.build_activity(game, ("em host", [45, 1000]), 1)
        self.assertEqual(act["party"]["size"], [45, 1000])


def frame(op, body):
    data = json.dumps(body).encode()
    return struct.pack("<II", op, len(data)) + data


class IPC(unittest.TestCase):
    def pair(self, script):
        """Socket do daemon ligado a um 'Discord' que segue o roteiro."""
        ours, theirs = socket.socketpair()
        ours.settimeout(3)
        received = []

        def server():
            for step in script:
                header = theirs.recv(8)
                if len(header) < 8:
                    break
                _op, length = struct.unpack("<II", header)
                received.append(json.loads(theirs.recv(length)))
                step(theirs)
        threading.Thread(target=server, daemon=True).start()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return ours, received

    def test_skips_dispatch_and_answers_ping(self):
        def step(s):
            s.sendall(frame(3, {"n": 1}))
            s.sendall(frame(1, {"cmd": "DISPATCH", "evt": "ACTIVITY_JOIN"}))
            s.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": None, "nonce": "1"}))
        sock, _ = self.pair([step])
        reply = dgp.send(sock, 1, {"cmd": "SET_ACTIVITY", "nonce": "1"})
        self.assertEqual(reply["cmd"], "SET_ACTIVITY")

    def test_error_is_raised(self):
        def step(s):
            s.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": "ERROR",
                                "data": {"code": 4000, "message": "state too short"}}))
        sock, _ = self.pair([step])
        with self.assertRaises(dgp.IPCError):
            dgp.send(sock, 1, {"cmd": "SET_ACTIVITY", "nonce": "1"})

    def test_closed_mid_frame_does_not_hang(self):
        def step(s):
            s.sendall(struct.pack("<II", 1, 500) + b'{"cmd":')
            s.shutdown(socket.SHUT_WR)
        sock, _ = self.pair([step])
        started = time.monotonic()
        with self.assertRaises(ConnectionError):
            dgp.send(sock, 1, {"cmd": "SET_ACTIVITY", "nonce": "1"})
        self.assertLess(time.monotonic() - started, 2)

    def test_close_frame(self):
        def step(s):
            s.sendall(frame(2, {"code": 4000, "message": "Invalid Client ID"}))
        sock, _ = self.pair([step])
        with self.assertRaises(ConnectionError):
            dgp.send(sock, 0, {"v": 1, "client_id": "0"})


class DetectableDB(unittest.TestCase):
    APPS = [
        {"id": "1", "name": "Alpha", "executables": [
            {"name": "game.exe", "os": "win32", "is_launcher": False},
            {"name": "alpha/bin/game.exe", "os": "win32", "is_launcher": False}]},
        {"id": "2", "name": "Beta", "executables": [
            {"name": "game.exe", "os": "win32", "is_launcher": False}]},
        {"id": "3", "name": "Gamma", "executables": [
            {"name": "gamma.exe", "os": "win32", "is_launcher": False},
            {"name": "gammalauncher.exe", "os": "win32", "is_launcher": True}]},
        {"id": "4", "name": "Delta", "executables": [
            {"name": ">delta_osx", "os": "darwin", "is_launcher": False},
            {"name": "delta.exe", "os": "win32", "is_launcher": False,
             "arguments": "-game delta"}]},
        {"id": "5", "name": "Tool", "executables": [
            {"name": "client.exe", "os": "win32", "is_launcher": False}]},
    ]

    def setUp(self):
        self.db = dgp.detectable_index(self.APPS + [
            {"id": "6", "name": "Unturned", "executables": [
                {"name": "unturned.exe", "os": "win32", "is_launcher": False}]},
            {"id": "7", "name": "Zomboid", "executables": [
                {"name": "projectzomboid64.exe", "os": "win32", "is_launcher": False}]},
        ])

    def match(self, path, args=""):
        hit = dgp.match_detectable([path], args, self.db)
        return hit[0]["name"] if hit else None

    def test_native_builds_of_windows_entries(self):
        self.assertEqual(self.match("/games/Unturned/Unturned.x86_64"), "Unturned")
        self.assertEqual(self.match("/games/pz/ProjectZomboid64"), "Zomboid")
        self.assertIsNone(self.match("/games/x/game.x86_64"))  # varios donos

    def test_shared_basename_needs_more_path(self):
        self.assertIsNone(self.match(r"Z:\Games\Beta\game.exe"))
        self.assertEqual(self.match(r"Z:\Games\Alpha\bin\game.exe"), "Alpha")

    def test_launcher_entries_are_skipped(self):
        self.assertEqual(self.match(r"C:\Gamma\gamma.exe"), "Gamma")
        self.assertIsNone(self.match(r"C:\Gamma\gammalauncher.exe"))

    def test_arguments_and_exact_names(self):
        self.assertIsNone(self.match(r"C:\Delta\delta.exe"))
        self.assertEqual(self.match(r"C:\Delta\delta.exe", "-game delta -x"), "Delta")
        self.assertIsNone(self.match("/Apps/delta_osx_helper"))

    def test_generic_names_alone_do_not_match(self):
        self.assertIsNone(self.match(r"C:\Tool\client.exe"))

    def test_64_bit_suffix(self):
        self.assertEqual(self.match(r"C:\Gamma\gamma64.exe"), "Gamma")


class Minecraft(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "PrismLauncher/instances/ATM9"
        self.game = self.root / "minecraft"
        (self.game / "logs").mkdir(parents=True)
        (self.root / "instance.cfg").write_text("[General]\nname=All the Mods 9\n")
        (self.game / "options.txt").write_text("")
        (self.game / "logs/latest.log").write_text(
            "[10:00:00] [Render thread/INFO]: Connecting to old.example.net, 25565\n"
            "[10:30:00] [Render thread/INFO]: Connecting to play.example.com, 25565\n")
        self.proc = Proc(40, ["/usr/lib/jvm/bin/java",
                              f"-Djava.library.path={self.root}/natives",
                              "org.prismlauncher.EntryPoint"], comm="java")
        self.saved = {k: getattr(dgp, k) for k in ("mc_proc", "mc_peers", "mc_ping",
                                                   "processes")}
        dgp.mc_proc = lambda: self.proc
        dgp.processes = lambda: [self.proc]
        dgp.mc_ping = lambda *a: [12, 100]
        self.peers = []
        dgp.mc_peers = lambda pid: self.peers

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(dgp, k, v)

    def test_game_dir_and_instance_name(self):
        self.assertEqual(dgp.mc_game_dir(self.proc), self.game)
        self.assertEqual(dgp.mc_instance(40), "All the Mods 9")

    def test_server_name_from_log(self):
        self.peers = [("203.0.113.7", "25565")]
        self.assertEqual(dgp.mc_state(), ("em play.example.com", [12, 100]))

    def test_srv_port_still_uses_logged_name(self):
        self.peers = [("203.0.113.7", "25577"), ("1.2.3.4", "443")]
        self.assertEqual(dgp.mc_state()[0], "em play.example.com")

    def test_singleplayer_when_not_connected(self):
        self.peers = []
        self.assertEqual(dgp.mc_state(), ("Singleplayer — All the Mods 9", None))

    def test_java_process_detection(self):
        dgp.mc_proc = self.saved["mc_proc"]
        other = Proc(41, ["java", "-jar", "/usr/share/PrismLauncher/JavaCheck.jar"], comm="java")
        tail = Proc(42, ["tail", "-f", f"{self.game}/logs/latest.log"])
        dgp.processes = lambda: [other, tail, self.proc]
        self.assertIs(dgp.mc_proc(), self.proc)
        dgp.processes = lambda: [other, tail]
        self.assertIsNone(dgp.mc_proc())


# ---------------------------------------------------------------- Chrome
def snss(*commands):
    """Arquivo de sessao SNSS v3 montado na mao, no formato do Chromium."""
    data = struct.pack("<ii", 0x53534E53, 3)
    for command, body in commands:
        data += struct.pack("<HB", len(body) + 1, command) + body
    return data


def pickle_str(text, wide=False):
    raw = text.encode("utf-16-le" if wide else "utf-8")
    count = len(raw) // 2 if wide else len(raw)
    return struct.pack("<i", count) + raw + b"\0" * (-len(raw) % 4)


def navigation(tab, index, url, title):
    body = struct.pack("<ii", tab, index) + pickle_str(url) + pickle_str(title, True)
    body += pickle_str("") + struct.pack("<ii", 0, 0)  # page state, transicao, tipo
    return 6, struct.pack("<I", len(body)) + body


def pair(command, a, b):
    return command, struct.pack("<ii", a, b)


class ChromeSession(unittest.TestCase):
    def test_tabs_selected_closed_and_pruned(self):
        data = snss(
            pair(0, 1, 10), pair(2, 10, 0),
            navigation(10, 0, "https://www.youtube.com/watch?v=AAAAAAAAAAA", "Vídeo 🎄 - YouTube"),
            pair(7, 10, 0),
            pair(0, 1, 11), pair(2, 11, 1),
            navigation(11, 0, "https://example.com/", "Outro"),
            navigation(11, 1, "https://music.youtube.com/watch?v=BBBBBBBBBBB", "Faixa | YouTube Music"),
            pair(7, 11, 1),
            pair(11, 11, 1),              # a primeira navegacao saiu: a atual vira 0
            pair(0, 1, 12), navigation(12, 0, "https://www.youtube.com/", "YouTube"),
            (16, struct.pack("<iiq", 12, 0, 0)),  # aba 12 fechada
            pair(8, 1, 1),                # selecionada: a da posicao 1
        ) + struct.pack("<HB", 50, 6) + b"pela metade"
        tabs = dgp.parse_session(data, "perfil")
        self.assertEqual([(t.url[-11:], t.title, t.selected) for t in tabs], [
            ("AAAAAAAAAAA", "Vídeo 🎄 - YouTube", False),
            ("BBBBBBBBBBB", "Faixa | YouTube Music", True)])

    def test_title_update_and_closed_window(self):
        data = snss(pair(0, 1, 10), navigation(10, 0, "https://www.youtube.com/", "YouTube"),
                    navigation(10, 0, "https://www.youtube.com/", "(2) YouTube"),
                    pair(0, 2, 20), navigation(20, 0, "https://www.youtube.com/", "x"),
                    (17, struct.pack("<iiq", 2, 0, 0)))
        tabs = dgp.parse_session(data)
        self.assertEqual([t.title for t in tabs], ["(2) YouTube"])

    def test_other_versions_are_ignored(self):
        self.assertEqual(dgp.parse_session(struct.pack("<ii", 0x53534E53, 5) + b"x" * 20), [])
        self.assertEqual(dgp.parse_session(b"lixo"), [])


Tab = dgp.Tab
WATCH = "https://www.youtube.com/watch?v="
SONG = "https://music.youtube.com/watch?v="


class ChromePlayer(unittest.TestCase):
    """Qual aba esta tocando, sem rede: o oEmbed vem de um dicionario."""

    OEMBED = {
        "AAAAAAAAAAA": {"title": "Video A", "author_name": "Canal A",
                        "author_url": "https://www.youtube.com/@a"},
        "SSSSSSSSSSS": {"title": "Rick Astley - Never Gonna Give You Up (Official Video)",
                        "author_name": "Rick Astley"},
        "TTTTTTTTTTT": {"title": "Rick Astley - Together Forever (Official Video)",
                        "author_name": "Rick Astley"},
    }

    def setUp(self):
        saved = {name: getattr(dgp, name) for name in
                 ("youtube_oembed", "history_url", "browser_windows", "live_video_id")}
        self.addCleanup(lambda: [setattr(dgp, k, v) for k, v in saved.items()])
        dgp.youtube_oembed = lambda video: self.OEMBED.get(video, {})
        dgp.history_url = lambda title, files=None: ""
        dgp.browser_windows = lambda: []
        dgp.live_video_id = lambda url: None
        dgp._TRACK_TAB.clear()
        dgp._MISMATCH.clear()

    def play(self, title, artist, tabs, track="T1", windows=None, status="Playing"):
        props = {"PlaybackStatus": status, "Metadata": {
            "xesam:title": title, "xesam:artist": [artist], "mpris:trackid": track}}
        return dgp.resolve_player("p", "chrome", props, tabs, windows)

    def age(self, seconds):
        title, _since = dgp._MISMATCH["p"]
        dgp._MISMATCH["p"] = (title, time.monotonic() - seconds)

    def test_tab_with_the_same_title(self):
        tabs = [Tab(("x", 1), 1, 0, False, WATCH + "AAAAAAAAAAA", "(3) Video A - YouTube")]
        media = self.play("Video A", "Canal A", tabs)
        self.assertEqual((media["service"], media["video"], media["ad"]),
                         ("youtube", "AAAAAAAAAAA", False))

    def test_ad_on_a_known_tab(self):
        tabs = [Tab(("x", 1), 1, 0, True, SONG + "SSSSSSSSSSS", "Never Gonna Give You Up | YouTube Music")]
        self.assertFalse(self.play("Never Gonna Give You Up", "Rick Astley", tabs)["ad"])
        media = self.play("Black Friday", "Loja Brasil", tabs)
        self.assertTrue(media["pending"])  # pode ser a aba atrasada: espera
        self.age(dgp.AD_CONFIRM + 1)
        media = self.play("Black Friday", "Loja Brasil", tabs)
        self.assertEqual((media["service"], media["ad"]), ("ytm", True))

    def test_next_track_with_stale_tab_title(self):
        old = [Tab(("x", 1), 1, 0, True, SONG + "SSSSSSSSSSS", "Never Gonna Give You Up | YouTube Music")]
        self.play("Never Gonna Give You Up", "Rick Astley", old)
        # o Chrome gravou a URL nova com o titulo da musica anterior
        new = [Tab(("x", 1), 1, 0, True, SONG + "TTTTTTTTTTT&list=RD", old[0].title)]
        media = self.play("Together Forever (2022 Remaster)", "Rick Astley", new)
        self.assertEqual((media["video"], media["ad"], media["pending"]),
                         ("TTTTTTTTTTT", False, False))

    def test_same_artist_next_track_waits_for_the_new_url(self):
        tabs = [Tab(("x", 1), 1, 0, True, SONG + "SSSSSSSSSSS", "Never Gonna Give You Up | YouTube Music")]
        self.play("Never Gonna Give You Up", "Rick Astley", tabs)
        # faixa nova do mesmo artista antes do Chrome gravar a URL nova
        media = self.play("Together Forever (2022 Remaster)", "Rick Astley", tabs)
        self.assertTrue(media["pending"])
        new = [Tab(("x", 1), 1, 0, True, SONG + "TTTTTTTTTTT", tabs[0].title)]
        media = self.play("Together Forever (2022 Remaster)", "Rick Astley", new)
        self.assertEqual((media["video"], media["pending"]), ("TTTTTTTTTTT", False))

    def test_long_music_video_ad_is_still_an_ad(self):
        tabs = [Tab(("x", 1), 1, 0, True, SONG + "SSSSSSSSSSS", "Never Gonna Give You Up | YouTube Music")]
        self.play("Never Gonna Give You Up", "Rick Astley", tabs)
        self.play("Carona Pra Marte", "Israel Novaes", tabs)
        self.age(180)
        self.assertTrue(self.play("Carona Pra Marte", "Israel Novaes", tabs)["ad"])

    def test_ad_before_the_first_video(self):
        tabs = [Tab(("x", 1), 1, 0, True, WATCH + "AAAAAAAAAAA", "Video A - YouTube")]
        windows = [("Video A - YouTube", False)]
        self.assertTrue(self.play("Presenteie", "Marca", tabs, windows=windows)["pending"])
        self.age(dgp.AD_CONFIRM + 1)
        media = self.play("Presenteie", "Marca", tabs, windows=windows)
        self.assertEqual((media["service"], media["ad"]), ("youtube", True))

    def test_no_blame_without_a_single_visible_candidate(self):
        tabs = [Tab(("x", 1), 1, 0, True, WATCH + "AAAAAAAAAAA", "Video A - YouTube")]
        minimized = [("Video A - YouTube", True)]
        self.assertIsNone(self.play("Outro site", "Alguem", tabs, windows=minimized)["service"])
        two = tabs + [Tab(("x", 2), 2, 0, True, WATCH + "SSSSSSSSSSS", "B - YouTube")]
        both = [("Video A - YouTube", False), ("B - YouTube", False)]
        self.assertIsNone(self.play("Outro site", "Alguem", two, windows=both)["service"])

    def test_other_browsers_and_firefox(self):
        props = {"PlaybackStatus": "Playing", "Metadata": {
            "xesam:title": "x", "xesam:url": WATCH + "AAAAAAAAAAA"}}
        media = dgp.resolve_player("f", "firefox", props, [])
        self.assertEqual((media["service"], media["source"]), ("youtube", "xesam:url"))
        props["Metadata"].pop("xesam:url")
        self.assertIsNone(dgp.resolve_player("v", "vlc", props, [])["service"])

    def test_title_fits(self):
        fits = dgp.title_fits
        self.assertTrue(fits("Video A", "Canal A", "AAAAAAAAAAA", "youtube"))
        self.assertFalse(fits("Video", "Canal A", "AAAAAAAAAAA", "youtube") is None)
        self.assertTrue(fits("Never Gonna Give You Up", "Rick Astley", "SSSSSSSSSSS", "ytm"))
        self.assertFalse(fits("Black Friday", "Loja", "SSSSSSSSSSS", "ytm"))
        self.assertIsNone(fits("x", "y", "ZZZZZZZZZZZ", "ytm"))


class ChromePresence(unittest.TestCase):
    def setUp(self):
        saved = {name: getattr(dgp, name) for name in
                 ("youtube_oembed", "video_thumbnail", "mpris_position")}
        self.addCleanup(lambda: [setattr(dgp, k, v) for k, v in saved.items()])
        dgp.youtube_oembed = lambda video: {"title": "Lofi", "author_name": "Lofi Girl",
                                            "author_url": "https://www.youtube.com/@LofiGirl"}
        dgp.video_thumbnail = lambda video: f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"
        dgp.mpris_position = lambda player, tries=3: 10_000_000

    def media(self, url, length, status="Playing", ad=False):
        return {"player": "p", "url": url, "video": dgp.youtube_video_id(url) or "LLLLLLLLLLL",
                "status": status, "ad": ad, "pending": False, "tab": None,
                "meta": {"xesam:title": "Lofi", "xesam:artist": ["Lofi Girl"],
                         "mpris:length": length}}

    def test_live_has_no_end(self):
        text, extra = dgp.video_state(self.media("https://www.youtube.com/@LofiGirl/live",
                                                 9223372036854775807))
        self.assertIn("Ao vivo", text)
        self.assertNotIn("end", extra["timestamps"])
        self.assertAlmostEqual(extra["timestamps"]["start"], time.time() - 10, delta=2)
        self.assertIsNone(dgp.media_timestamps("x", 1, 9223372036854775807))

    def test_video_card_links_and_bar(self):
        text, extra = dgp.video_state(self.media(WATCH + "AAAAAAAAAAA", 60_000_000))
        game = dgp.GAMES["youtube"]
        act = dgp.build_activity(game, (text, extra), time.time())
        self.assertEqual((act["type"], act["details"], act["state"]), (3, "Lofi", "Lofi Girl"))
        self.assertEqual(act["details_url"], WATCH + "AAAAAAAAAAA")
        self.assertEqual(act["state_url"], "https://www.youtube.com/@LofiGirl")
        self.assertEqual(act["status_display_type"], 2)
        self.assertEqual(act["timestamps"]["end"] - act["timestamps"]["start"], 60)
        self.assertEqual(act["assets"]["small_image"], dgp.YT_ICON)
        self.assertEqual([b["label"] for b in act["buttons"]], ["Assistir no YouTube", "Ver o canal"])

    def test_paused_and_ad(self):
        text, extra = dgp.video_state(self.media(WATCH + "AAAAAAAAAAA", 60_000_000, "Paused"))
        self.assertEqual(text, "Lofi Girl · pausado em 0:10")
        self.assertIsNone(extra["timestamps"])
        text, extra = dgp.video_state(self.media(WATCH + "AAAAAAAAAAA", 15_000_000, ad=True))
        self.assertEqual((text, extra["details"]), ("Anúncio", "Lofi"))

    def test_shorts(self):
        text, extra = dgp.video_state(self.media("https://www.youtube.com/shorts/AAAAAAAAAAA",
                                                 30_000_000))
        self.assertEqual(text, "Shorts · Lofi Girl")
        self.assertEqual(extra["details_url"], "https://www.youtube.com/shorts/AAAAAAAAAAA")

    def test_browsing_pages(self):
        def page(url, title="YouTube"):
            return Tab(("x", 1), 1, 0, True, url, title)
        text = dgp.browse_text
        self.assertEqual(text("youtube", page("https://www.youtube.com/"))[0], "Na página inicial")
        search = page("https://www.youtube.com/results?search_query=segredo")
        self.assertEqual(text("youtube", search)[0], "Pesquisando")
        self.assertEqual(text("youtube", search, show_search=True)[0], "Pesquisando segredo")
        channel = page("https://www.youtube.com/@BlenderOfficial")
        self.assertEqual(text("youtube", channel, "Blender - YouTube")[0], "No canal Blender")
        self.assertEqual(text("youtube", channel)[0], "Num canal")  # titulo velho da aba
        self.assertEqual(text("ytm", page("https://music.youtube.com/library"))[0], "Na biblioteca")
        self.assertEqual(dgp.caption_service("Faixa | YouTube Music"), "ytm")
        self.assertEqual(dgp.caption_service("(2) Video - YouTube"), "youtube")
        self.assertIsNone(dgp.caption_service("Agents"))

    def test_new_fields_sanitized_and_legacy(self):
        out = dgp.sanitize_activity({
            "type": 4, "details": "d", "details_url": "https://ok", "state": "estado",
            "state_url": "javascript:x", "status_display_type": 7,
            "assets": {"large_image": "https://i/x.png", "large_url": "https://l",
                       "small_image": "https://i/s.png", "small_text": "S!", "small_url": "ftp://x"}})
        self.assertEqual(out["type"], 0)
        self.assertNotIn("details_url", out)  # sem details (1 caractere) nao ha link
        self.assertNotIn("state_url", out)
        self.assertNotIn("status_display_type", out)
        self.assertEqual(out["assets"], {"large_image": "https://i/x.png", "large_url": "https://l",
                                         "small_image": "https://i/s.png", "small_text": "S!"})
        rich = dgp.sanitize_activity({"type": 2, "details": "Faixa", "details_url": "https://a",
                                      "status_display_type": 1,
                                      "assets": {"large_image": "x", "large_url": "https://b"}})
        self.assertEqual(dgp.legacy_activity(rich),
                         {"type": 2, "details": "Faixa", "assets": {"large_image": "x"}})


if __name__ == "__main__":
    unittest.main()

"""Sites novos no navegador (Twitch, Netflix, Crunchyroll, Spotify) e o CS2.

Nada aqui toca a rede, o Discord ou o navegador de verdade: os players MPRIS,
as abas e as janelas sao montados a mao, e o servidor do Game State
Integration recebe POSTs feitos pelo proprio teste em 127.0.0.1.
"""
import http.client
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dgp = load("dgp_sites", ROOT / "discord-game-presence.py")


def tab(url, title, selected=True, key=("p", 1)):
    return dgp.Tab(key, 1, 0, selected, url, title)


def player(title, artist="", status="Playing", process="chrome", url="", name="p1", **meta):
    data = {"xesam:title": title, "xesam:artist": [artist] if artist else []}
    if url:
        data["xesam:url"] = url
    data.update(meta)
    return {"player": name, "process": process, "meta": data, "status": status,
            "url": url, "service": None}


class SiteNames(unittest.TestCase):
    def test_site_of(self):
        self.assertEqual(dgp.site_of("https://www.twitch.tv/gaules"), "twitch")
        self.assertEqual(dgp.site_of("https://www.netflix.com/watch/8113"), "netflix")
        self.assertEqual(dgp.site_of("https://www.crunchyroll.com/watch/G4/x"), "crunchyroll")
        self.assertEqual(dgp.site_of("https://open.spotify.com/album/1"), "spotify")
        self.assertIsNone(dgp.site_of("https://www.youtube.com/watch?v=x"))
        self.assertIsNone(dgp.site_of("https://nottwitch.tv.evil.com/"))
        self.assertIsNone(dgp.site_of("https://spotify.com.evil.net/"))

    def test_captions_and_titles(self):
        self.assertEqual(dgp.site_caption("gaules - Twitch"), "twitch")
        self.assertEqual(dgp.site_caption("Netflix"), "netflix")
        self.assertEqual(dgp.site_caption("Spotify – Web Player"), "spotify")
        self.assertIsNone(dgp.site_caption("Some video - YouTube"))
        self.assertEqual(dgp.site_title("crunchyroll", "Watch Frieren - Crunchyroll"), "Frieren")
        self.assertEqual(dgp.site_title("twitch", "(2) gaules - Twitch"), "gaules")
        self.assertEqual(dgp.site_title("netflix", "Netflix"), "")

    def test_player_pages(self):
        self.assertTrue(dgp.site_player_page("twitch", "https://www.twitch.tv/gaules"))
        self.assertFalse(dgp.site_player_page("twitch", "https://www.twitch.tv/directory"))
        self.assertTrue(dgp.site_player_page("netflix", "https://www.netflix.com/watch/1"))
        self.assertFalse(dgp.site_player_page("netflix", "https://www.netflix.com/browse"))
        self.assertEqual(dgp.twitch_channel("https://www.twitch.tv/gaules/"), "gaules")
        self.assertIsNone(dgp.twitch_channel("https://www.twitch.tv/directory/all"))


class SiteMedia(unittest.TestCase):
    def test_title_inside_tab_title(self):
        tabs = [tab("https://open.spotify.com/", "Song A • Band"),
                tab("https://www.twitch.tv/gaules", "gaules - Twitch", key=("p", 2))]
        found = [player("Song A", "Band")]
        media = dgp.site_media("spotify", found, tabs)
        self.assertEqual(media["tab"].url, "https://open.spotify.com/")
        self.assertIsNone(dgp.site_media("twitch", found, tabs))

    def test_spotify_stale_title_single_tab(self):
        # o Chrome nao regrava o titulo quando so a faixa muda
        tabs = [tab("https://open.spotify.com/", "Song A • Band")]
        media = dgp.site_media("spotify", [player("Song B", "Band")], tabs)
        self.assertIsNotNone(media)

    def test_sound_of_another_tab_is_not_ours(self):
        tabs = [tab("https://www.netflix.com/watch/1", "Netflix"),
                tab("https://example.com/v", "Cat video", key=("p", 2))]
        self.assertIsNone(dgp.site_media("netflix", [player("Cat video")], tabs))

    def test_single_player_page_takes_unknown_title(self):
        tabs = [tab("https://www.netflix.com/watch/1", "Netflix")]
        media = dgp.site_media("netflix", [player("Stranger Things")], tabs)
        self.assertEqual(media["tab"].url, "https://www.netflix.com/watch/1")

    def test_youtube_player_is_skipped(self):
        tabs = [tab("https://www.twitch.tv/gaules", "gaules - Twitch")]
        yt = player("gaules")
        yt["service"] = "youtube"
        self.assertIsNone(dgp.site_media("twitch", [yt], tabs))

    def test_firefox_url(self):
        media = dgp.site_media("twitch", [player("Live!", process="firefox",
                                                 url="https://www.twitch.tv/gaules")], [])
        self.assertEqual(media["url"], "https://www.twitch.tv/gaules")

    def test_playing_beats_paused(self):
        tabs = [tab("https://open.spotify.com/", "A • X"),
                tab("https://open.spotify.com/", "B • Y", key=("p", 2))]
        found = [player("A", "X", status="Paused", name="p1"),
                 player("B", "Y", name="p2")]
        self.assertEqual(dgp.site_media("spotify", found, tabs)["player"], "p2")


class SiteStates(unittest.TestCase):
    def setUp(self):
        for name in ("mpris_position", "anchored_position", "track_length", "ytm_art"):
            patcher = mock.patch.object(dgp, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_twitch(self):
        media = {**player("Ranked hoje", "gaules"), "url": "https://www.twitch.tv/gaules",
                 "tab": tab("https://www.twitch.tv/gaules", "gaules - Twitch")}
        state, extra = dgp.site_media_state("twitch", media)
        self.assertEqual(extra["details"], "gaules")
        self.assertEqual(state, "Ranked hoje")
        self.assertEqual(extra["details_url"], "https://www.twitch.tv/gaules")

    def test_spotify_cover_and_artist(self):
        media = {**player("Song", "Band", **{"mpris:artUrl": "https://i.scdn.co/image/ab"}),
                 "tab": None}
        state, extra = dgp.site_media_state("spotify", media)
        self.assertEqual((state, extra["details"]), ("Band", "Song"))
        self.assertEqual(extra["large_image"], "https://i.scdn.co/image/ab")

    def test_spotify_local_art_is_not_sent(self):
        media = {**player("Song", "Band", **{"mpris:artUrl": "file:///tmp/x.png"}), "tab": None}
        _state, extra = dgp.site_media_state("spotify", media)
        self.assertNotIn("large_image", extra)

    def test_netflix_paused(self):
        media = {**player("Netflix", status="Paused"),
                 "tab": tab("https://www.netflix.com/watch/1", "Netflix")}
        state, extra = dgp.site_media_state("netflix", media)
        self.assertEqual(extra["details"], "Netflix")
        self.assertTrue(state.startswith("Pausado"))

    def test_browse(self):
        _s, extra = dgp.site_browse_state("twitch", tab("https://www.twitch.tv/gaules", "x"), 100)
        self.assertEqual(extra["details"], "No canal gaules")

    def test_entries_need_an_id(self):
        for key in ("twitch", "netflix", "crunchyroll", "spotify"):
            self.assertTrue(dgp.GAMES[key]["needs_id"])
            self.assertFalse(dgp.GAMES[key]["client_id"])
        self.assertNotIn("twitch", [k for k, _g in dgp.active_games()])


def gsi(**parts):
    payload = {"provider": {"name": "Counter-Strike: Global Offensive", "appid": 730,
                            "steamid": "765"},
               "auth": {"token": "t" * 32}}
    payload.update(parts)
    return payload


class CS2(unittest.TestCase):
    def setUp(self):
        dgp._CS2.update(data=None, stamp=-1e9, warned=True)

    def feed(self, payload):
        self.assertTrue(dgp.cs2_accept(payload, "t" * 32))

    def test_wrong_token_is_ignored(self):
        self.assertFalse(dgp.cs2_accept({**gsi(), "auth": {"token": "x"}}, "t" * 32))
        self.assertFalse(dgp.cs2_accept(gsi(), ""))
        self.assertIsNone(dgp.cs2_data())

    def test_menu(self):
        self.feed(gsi(player={"steamid": "765", "activity": "menu"}))
        with mock.patch.object(dgp, "cs2_server"):
            self.assertEqual(dgp.cs2_state(), ("No menu", None))
        self.assertEqual(dgp.cs2_details(), "Counter-Strike 2")

    def test_match_from_my_side(self):
        self.feed(gsi(map={"mode": "competitive", "name": "de_mirage", "phase": "live",
                           "team_ct": {"score": 5}, "team_t": {"score": 7}},
                      player={"steamid": "765", "team": "T",
                              "match_stats": {"kills": 12, "assists": 3, "deaths": 4}}))
        with mock.patch.object(dgp, "cs2_server"):
            self.assertEqual(dgp.cs2_state(), ("7 x 5 · 12/3/4", None))
        self.assertEqual(dgp.cs2_details(), "Competitivo · Mirage")

    def test_spectating_someone_else(self):
        self.feed(gsi(map={"mode": "casual", "name": "workshop/123/aim_map",
                           "phase": "live", "team_ct": {"score": 1}, "team_t": {"score": 2}},
                      player={"steamid": "999", "team": "CT",
                              "match_stats": {"kills": 30, "assists": 0, "deaths": 0}}))
        with mock.patch.object(dgp, "cs2_server"):
            self.assertEqual(dgp.cs2_state(), ("CT 1 x 2 T · assistindo", None))
        self.assertEqual(dgp.cs2_details(), "Casual · Map")

    def test_warmup_and_stale(self):
        self.feed(gsi(map={"mode": "competitive", "name": "de_nuke", "phase": "warmup",
                           "team_ct": {"score": 0}, "team_t": {"score": 0}},
                      player={"steamid": "765", "team": "CT",
                              "match_stats": {"kills": 0, "assists": 0, "deaths": 0}}))
        with mock.patch.object(dgp, "cs2_server"):
            self.assertEqual(dgp.cs2_state(), ("Aquecimento", None))
            dgp._CS2["stamp"] -= dgp.CS2_FRESH + 1
            with mock.patch.object(dgp, "cs2_install_cfg", return_value=False):
                self.assertEqual(dgp.cs2_state(), ("Jogando", None))

    def test_cfg_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "game/csgo/cfg"
            cfg.mkdir(parents=True)
            env = {"XDG_DATA_HOME": str(pathlib.Path(tmp) / "data")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(dgp, "cs2_cfg_dirs", return_value=[cfg]):
                self.assertTrue(dgp.cs2_install_cfg())
                self.assertFalse(dgp.cs2_install_cfg())  # igual: nao regrava
                token = dgp.cs2_token()
            text = (cfg / dgp.CS2_CFG).read_bytes()
            self.assertFalse(text.startswith(b"\xef\xbb\xbf"))
            self.assertIn(f'"token" "{token}"'.encode(), text)
            self.assertIn(b'"uri" "http://127.0.0.1:', text)
            mode = (pathlib.Path(tmp) / "data/discord-game-presence/cs2-token").stat().st_mode
            self.assertEqual(mode & 0o777, 0o600)

    def test_http_server(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}), \
                mock.patch.object(dgp, "CS2_PORT", 0):
            dgp._CS2["server"] = None
            server = dgp.cs2_server()
            self.addCleanup(server.shutdown)
            port = server.server_address[1]
            token = dgp.cs2_token()

            def post(body):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/", body=json.dumps(body))
                status = conn.getresponse().status
                conn.close()
                return status

            self.assertEqual(post({**gsi(), "auth": {"token": "nope"}}), 403)
            self.assertIsNone(dgp.cs2_data())
            self.assertEqual(post({**gsi(map={"name": "de_dust2"}), "auth": {"token": token}}), 200)
            self.assertEqual(dgp.cs2_data()["map"]["name"], "de_dust2")
            self.assertIs(dgp.cs2_server(), server)  # um so

    def test_entry(self):
        game = dgp.GAMES["cs2"]
        self.assertEqual(game["client_id"], "1158877933042143272")
        proc = dgp.Proc(1, ["/x/game/bin/linuxsteamrt64/cs2", "-steam"], "cs2", "")
        rules = dgp.entry_rules("cs2", game)
        self.assertTrue(any(dgp.proc_matches(proc, r) for r in rules))


if __name__ == "__main__":
    unittest.main()

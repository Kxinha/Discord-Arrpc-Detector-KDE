"""Atualizacao pela bandeja: contra um GitHub falso em 127.0.0.1, sem rede.

Nada aqui toca a instalacao de verdade: HOME, cache e PATH apontam para uma
pasta temporaria, e systemctl, pkexec e pacman sao scripts que so anotam como
foram chamados.
"""
import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dgp = load("dgp_update", "discord-game-presence.py")
DAEMON_SOURCE = (ROOT / "discord-game-presence.py").read_text()
TRAY_SOURCE = (ROOT / "discord-presence-tray.py").read_text()


class FakeGitHub:
    """Serve /api (a release) e /files/<nome>, como o GitHub faria."""

    def __init__(self):
        self.files, self.release = {}, {}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api":
                    body = json.dumps(owner.release).encode()
                elif self.path.startswith("/files/") and self.path[7:] in owner.files:
                    body = owner.files[self.path[7:]]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, version, files, digest=True, sums=False, prerelease=False, tamper=()):
        self.files = dict(files)
        assets = []
        for name, data in files.items():
            asset = {"name": name, "browser_download_url": f"{self.base}/files/{name}"}
            if digest:
                real = hashlib.sha256(data).hexdigest()
                asset["digest"] = "sha256:" + ("0" * 64 if name in tamper else real)
            assets.append(asset)
        if sums:
            text = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items())
            self.files["SHA256SUMS"] = text.encode()
            assets.append({"name": "SHA256SUMS",
                           "browser_download_url": f"{self.base}/files/SHA256SUMS"})
        self.release = {"tag_name": f"v{version}", "name": f"v{version} teste",
                        "html_url": f"{self.base}/page", "draft": False,
                        "prerelease": prerelease, "body": "**Novo**: teste", "assets": assets}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def script_release(version, daemon=None, tray=None):
    daemon = daemon or re.sub(r'^VERSION = "[^"]+"$', f'VERSION = "{version}"',
                              DAEMON_SOURCE, count=1, flags=re.M)
    return {
        "discord-game-presence.py": daemon.encode(),
        "discord-presence-tray.py": (tray or TRAY_SOURCE).encode(),
        "discord-game-presence.service": b"[Service]\nExecStart=%h/.local/bin/discord-game-presence.py\n",
        "discord-game-presence-tray.service": b"[Service]\nExecStart=%h/.local/bin/discord-presence-tray.py\n",
    }


class Updates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub()

    @classmethod
    def tearDownClass(cls):
        cls.github.close()

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / ".local/bin").mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.log"
        for tool in ("systemctl", "pacman"):
            self.stub(tool, 0)
        saved_env = {k: os.environ.get(k) for k in ("HOME", "PATH", "APPIMAGE", "APPDIR")}
        saved = {k: getattr(dgp, k) for k in ("RELEASES_API", "UPDATE_DIR")}

        def restore():
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for key, value in saved.items():
                setattr(dgp, key, value)
        self.addCleanup(restore)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        os.environ.pop("APPIMAGE", None)
        os.environ.pop("APPDIR", None)
        dgp.RELEASES_API = self.github.base + "/api"
        dgp.UPDATE_DIR = self.tmp / "cache"

    def stub(self, tool, code, check_file=False):
        """Um executavel falso que anota os argumentos (e se o arquivo existia)."""
        script = self.bin / tool
        check = 'for a in "$@"; do [ -f "$a" ] && echo "existe $a" >> "$LOG"; done\n' if check_file else ""
        script.write_text(f'#!/bin/sh\nLOG="{self.calls}"\necho "{tool} $*" >> "$LOG"\n{check}exit {code}\n')
        script.chmod(0o755)

    def calls_text(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def installed(self, name):
        return (self.home / dgp.SCRIPT_FILES[name]).read_bytes()

    def old_install(self):
        for name in dgp.SCRIPT_REQUIRED:
            path = self.home / dgp.SCRIPT_FILES[name]
            path.write_text("# versao antiga\n")

    # ------------------------------------------------------------------
    def test_versions(self):
        self.assertEqual(dgp.version_tuple("v1.10.2"), (1, 10, 2))
        self.assertEqual(dgp.version_tuple("1.4"), (1, 4, 0))
        self.assertIsNone(dgp.version_tuple("v1.4.0-beta"))
        self.assertTrue(dgp.is_newer({"version": "1.10.0"}, "1.9.9"))
        self.assertFalse(dgp.is_newer({"version": "1.3.0"}, "1.3.0"))
        self.assertFalse(dgp.is_newer(None, "1.3.0"))

    def test_latest_release_and_prerelease(self):
        self.github.publish("9.0.0", {"a.txt": b"x"})
        release = dgp.latest_release()
        self.assertEqual((release["version"], release["assets"]["a.txt"]["sha256"]),
                         ("9.0.0", hashlib.sha256(b"x").hexdigest()))
        self.github.release["assets"][0]["state"] = "new"  # ainda subindo
        self.assertEqual(dgp.latest_release()["assets"], {})
        self.github.publish("9.0.1", {"a.txt": b"x"}, prerelease=True)
        self.assertIsNone(dgp.latest_release())

    def test_install_kind(self):
        target = self.home / ".local/bin/discord-presence-tray.py"
        target.write_text("")
        self.assertEqual(dgp.install_kind(target), "script")
        self.assertEqual(dgp.install_kind(ROOT / "discord-presence-tray.py"), "manual")
        clone = self.tmp / "clone.py"  # link na .local/bin para um clone do git
        clone.write_text("")
        link = self.home / ".local/bin/link.py"
        link.symlink_to(clone)
        self.assertEqual(dgp.install_kind(link), "manual")
        os.environ.update(APPIMAGE=str(self.tmp / "x.AppImage"), APPDIR=str(self.tmp / "mnt"))
        self.assertEqual(dgp.install_kind(self.tmp / "mnt/usr/bin/discord-presence-tray"),
                         "appimage")

    def test_script_update_replaces_files_and_keeps_backup(self):
        self.old_install()
        files = script_release("9.1.0")
        self.github.publish("9.1.0", files)
        release = dgp.latest_release()
        self.assertEqual(dgp.install_update(release, "script"), "script")
        for name, data in files.items():
            self.assertEqual(self.installed(name), data)
        self.assertTrue(os.access(self.home / ".local/bin/discord-game-presence.py", os.X_OK))
        backup = self.home / ".local/share/discord-game-presence/backup" / dgp.VERSION
        self.assertEqual((backup / "discord-game-presence.py").read_text(), "# versao antiga\n")
        self.assertIn("systemctl --user daemon-reload", self.calls_text())
        self.assertFalse(any((dgp.UPDATE_DIR).glob("9.1.0*")), "sobrou a pasta de download")

    def test_bad_checksum_changes_nothing(self):
        self.old_install()
        self.github.publish("9.2.0", script_release("9.2.0"),
                            tamper={"discord-presence-tray.py"})
        with self.assertRaisesRegex(dgp.UpdateError, "sha256"):
            dgp.install_update(dgp.latest_release(), "script")
        self.assertEqual(self.installed("discord-game-presence.py"), b"# versao antiga\n")

    def test_sha256sums_when_github_has_no_digest(self):
        self.old_install()
        files = script_release("9.3.0")
        self.github.publish("9.3.0", files, digest=False, sums=True)
        dgp.install_update(dgp.latest_release(), "script")
        self.assertEqual(self.installed("discord-presence-tray.py"), files["discord-presence-tray.py"])
        self.github.publish("9.3.1", script_release("9.3.1"), digest=False)
        with self.assertRaisesRegex(dgp.UpdateError, "no published sha256"):
            dgp.install_update(dgp.latest_release(), "script")

    def test_wrong_version_or_broken_file_is_refused(self):
        self.old_install()
        self.github.publish("9.4.0", script_release("9.4.0", daemon=DAEMON_SOURCE))
        with self.assertRaisesRegex(dgp.UpdateError, "not version 9.4.0"):
            dgp.install_update(dgp.latest_release(), "script")
        self.github.publish("9.4.1", script_release("9.4.1", tray="def quebrado(:\n"))
        with self.assertRaisesRegex(dgp.UpdateError, "does not compile"):
            dgp.install_update(dgp.latest_release(), "script")
        self.assertEqual(self.installed("discord-presence-tray.py"), b"# versao antiga\n")

    def test_missing_files(self):
        self.github.publish("9.5.0", {"discord-game-presence.py": b"x"})
        with self.assertRaisesRegex(dgp.UpdateError, "discord-presence-tray.py"):
            dgp.install_update(dgp.latest_release(), "script")
        with self.assertRaisesRegex(dgp.UpdateError, "release page"):
            dgp.install_update(dgp.latest_release(), "manual")
        with self.assertRaisesRegex(dgp.UpdateError, "no pacman package"):
            dgp.install_update(dgp.latest_release(), "pacman")

    def test_pacman_update_goes_through_pkexec(self):
        package = "discord-game-presence-9.6.0-1-any.pkg.tar.zst"
        self.github.publish("9.6.0", {package: b"pacote", "discord-game-presence_9.6.0-1_all.deb": b"d"})
        self.stub("pkexec", 0, check_file=True)
        dgp.install_update(dgp.latest_release(), "pacman")
        log = self.calls_text()
        self.assertRegex(log, rf"pkexec pacman -U --noconfirm \S+/{re.escape(package)}")
        self.assertIn(f"existe {dgp.UPDATE_DIR}", log)  # o pacote ja estava baixado

    def test_cancelled_password_dialog(self):
        package = "discord-game-presence-9.7.0-1-any.pkg.tar.zst"
        self.github.publish("9.7.0", {package: b"pacote"})
        self.stub("pkexec", 126)
        with self.assertRaisesRegex(dgp.UpdateError, "cancelled"):
            dgp.install_update(dgp.latest_release(), "pacman")

    def test_appimage_is_replaced(self):
        image = self.tmp / "DiscordPresence.AppImage"
        image.write_bytes(b"antigo")
        os.environ.update(APPIMAGE=str(image), APPDIR=str(self.tmp / "mnt"))
        self.github.publish("9.8.0", {"DiscordPresence-9.8.0-x86_64.AppImage": b"novo"})
        dgp.install_update(dgp.latest_release(), "appimage")
        self.assertEqual(image.read_bytes(), b"novo")
        self.assertTrue(os.access(image, os.X_OK))


class ReleaseSummary(unittest.TestCase):
    def test_first_line_without_markdown(self):
        try:
            tray = load("tray_update", "discord-presence-tray.py")
        except (ImportError, ValueError) as error:
            self.skipTest(f"sem GTK/AppIndicator: {error}")
        self.assertEqual(tray.release_summary("\n## Novidades\n"), "Novidades")
        self.assertEqual(tray.release_summary("**Correção**: a [presença](http://x) do `show_search`"),
                         "Correção: a presença do show_search")
        self.assertTrue(tray.release_summary("x" * 500).endswith("…"))


if __name__ == "__main__":
    unittest.main()

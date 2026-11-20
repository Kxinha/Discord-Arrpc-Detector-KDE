"""A conexao com o Discord morre sem o daemon escrever nada, e a presenca volta.

O daemon so escrevia quando a atividade mudava, entao uma conexao fechada
pelo outro lado passava despercebida: o Vesktop 1.6.9 fecha todas as conexoes
de RPC quando a janela recarrega ("Renderer reloaded"), e ao reiniciar o
socket antigo simplesmente acaba. Com o jogo parado no mesmo estado, a
presenca sumia do perfil ate alguem reiniciar o daemon.

Tudo aqui e um Discord falso num socket Unix dentro de uma pasta temporaria.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import socket
import struct
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dgp = load("dgp_link", ROOT / "discord-game-presence.py")


def frame(op, body):
    data = json.dumps(body).encode()
    return struct.pack("<II", op, len(data)) + data


def patch(test, module, **values):
    saved = {name: getattr(module, name) for name in values}
    test.addCleanup(lambda: [setattr(module, k, v) for k, v in saved.items()])
    for name, value in values.items():
        setattr(module, name, value)


class CheckLink(unittest.TestCase):
    def setUp(self):
        self.ours, self.theirs = socket.socketpair()
        self.ours.settimeout(2)
        self.theirs.settimeout(2)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.theirs.close)

    def test_quiet_connection_is_fine(self):
        self.assertIsNone(dgp.check_link(self.ours))

    def test_ping_gets_pong_and_events_are_ignored(self):
        self.theirs.sendall(frame(1, {"cmd": "DISPATCH", "evt": "READY"}))
        self.theirs.sendall(frame(4, {"nonce": "x"}))
        self.theirs.sendall(frame(3, {"nonce": "p"}))
        time.sleep(0.05)
        dgp.check_link(self.ours)
        op, length = struct.unpack("<II", self.theirs.recv(8))
        self.assertEqual((op, json.loads(self.theirs.recv(length))), (4, {"nonce": "p"}))

    def test_close_frame_ends_the_session(self):
        self.theirs.sendall(frame(2, {"code": 1000, "message": "Renderer reloaded"}))
        time.sleep(0.05)
        with self.assertRaisesRegex(ConnectionError, "Renderer reloaded"):
            dgp.check_link(self.ours)

    def test_other_side_gone(self):
        self.theirs.close()
        with self.assertRaises(ConnectionError):
            dgp.check_link(self.ours)

    def test_socket_replaced_by_a_new_discord(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "discord-ipc-0"
            path.write_text("")
            link = {"path": str(path), "ino": os.stat(path).st_ino}
            dgp.check_link(self.ours, link)
            path.unlink()
            dgp.check_link(self.ours, link)  # sumiu, mas a conexao vive: segue
            path.write_text("novo")
            if os.stat(path).st_ino != link["ino"]:
                with self.assertRaisesRegex(ConnectionError, "reiniciou"):
                    dgp.check_link(self.ours, link)

    def test_send_skips_a_pong_before_the_answer(self):
        def server():
            header = self.theirs.recv(8)
            self.theirs.recv(struct.unpack("<II", header)[1])
            self.theirs.sendall(frame(4, {"nonce": "velho"}))
            self.theirs.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": "ERROR",
                                          "data": {"message": "recusado"}}))
        threading.Thread(target=server, daemon=True).start()
        with self.assertRaises(dgp.IPCError):  # o PONG nao virou a resposta
            dgp.send(self.ours, 1, {"cmd": "SET_ACTIVITY", "nonce": "1"})


class FakeDiscord:
    """Servidor de IPC minimo: handshake, SET_ACTIVITY e o que o teste mandar."""

    def __init__(self, path):
        self.path = path
        self.events = []  # (hora, conexao, atividade)
        self.handshakes = 0
        self.refuse = 0  # quantas SET_ACTIVITY recusar com erro passageiro
        self.conns = []
        self.lock = threading.Lock()
        self.server = self.listen()

    def listen(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(8)
        threading.Thread(target=self.accept, args=(server,), daemon=True).start()
        return server

    def accept(self, server):
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with self.lock:
                self.conns.append(conn)
                number = len(self.conns)
            threading.Thread(target=self.serve, args=(conn, number), daemon=True).start()

    def serve(self, conn, number):
        try:
            while True:
                header = conn.recv(8)
                if len(header) < 8:
                    return
                op, length = struct.unpack("<II", header)
                body = json.loads(conn.recv(length)) if length else {}
                if op == 0:
                    with self.lock:
                        self.handshakes += 1
                    conn.sendall(frame(1, {"cmd": "DISPATCH", "evt": "READY", "data": {}}))
                elif op == 1:
                    activity = body["args"]["activity"]
                    with self.lock:
                        refuse = bool(activity) and self.refuse > 0
                        self.refuse -= refuse
                        if not refuse:
                            self.events.append((time.time(), number, activity))
                    if refuse:
                        conn.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": "ERROR",
                                               "nonce": body.get("nonce"),
                                               "data": {"message": "Request has been terminated"}}))
                    else:
                        conn.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": None,
                                               "nonce": body.get("nonce"), "data": activity}))
        except OSError:
            return

    def close_all(self, message):
        """O que o Vesktop faz quando a janela recarrega."""
        with self.lock:
            conns, self.conns = self.conns, []
        for conn in conns:
            try:
                conn.sendall(frame(2, {"code": 1000, "message": message}))
                conn.close()
            except OSError:
                pass

    def restart(self):
        """Vesktop novo no mesmo caminho; a conexao velha fica pendurada."""
        self.server.close()
        os.unlink(self.path)
        self.server = self.listen()

    def shown(self, after=0, conn=None):
        with self.lock:
            return [e for e in self.events if e[2] and e[0] > after
                    and (conn is None or e[1] == conn)]

    def wait(self, test, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if test():
                return True
            time.sleep(0.05)
        return False


class Sessions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.environ["XDG_RUNTIME_DIR"] = self.dir.name
        self.addCleanup(os.environ.pop, "XDG_RUNTIME_DIR")
        self.discord = FakeDiscord(os.path.join(self.dir.name, "discord-ipc-0"))
        self.addCleanup(lambda: self.discord.server.close())
        patch(self, dgp, ALIVE_CHECK=0.1, DETECT_POLL=0.2, REFUSED_RETRY=0.5,
              REASSERT=60, detect=lambda key, game: True, hit_start=lambda hit: 1000.0,
              log=lambda *_: None)
        self.game = {"client_id": "123", "details": "Jogo Falso", "state": "Jogando",
                     "large_text": "Jogo Falso", "poll": 0.3}
        dgp.STOPPING.clear()
        self.thread = threading.Thread(target=dgp.watch, args=("jogo", self.game), daemon=True)

    def tearDown(self):
        dgp.STOPPING.set()
        self.thread.join(5)
        dgp.STOPPING.clear()

    def start(self):
        self.thread.start()
        self.assertTrue(self.discord.wait(lambda: self.discord.shown(), 5), "nao publicou")

    def test_comes_back_after_the_window_reloads(self):
        self.start()
        mark = time.time()
        self.discord.close_all("Renderer reloaded")
        self.assertTrue(self.discord.wait(lambda: self.discord.shown(mark), 3),
                        "nao voltou depois do Renderer reloaded")
        self.assertEqual(self.discord.handshakes, 2)

    def test_comes_back_after_discord_restarts(self):
        self.start()
        mark = time.time()
        self.discord.restart()
        self.assertTrue(self.discord.wait(lambda: self.discord.shown(mark), 3),
                        "nao foi para o Discord novo")
        self.assertEqual(self.discord.shown(mark)[0][1], 2)  # conexao nova

    def test_a_passing_refusal_is_tried_again(self):
        self.discord.refuse = 1
        self.thread.start()
        self.assertTrue(self.discord.wait(lambda: self.discord.shown(), 3),
                        "desistiu depois de uma recusa passageira")

    def test_same_activity_is_sent_again_now_and_then(self):
        dgp.REASSERT = 0.5
        self.start()
        first = self.discord.shown()[0]
        self.assertTrue(self.discord.wait(lambda: len(self.discord.shown()) >= 2, 3))
        again = self.discord.shown()[1]
        self.assertEqual(again[2], first[2])  # a mesma, com o mesmo tempo
        self.assertEqual(again[1], first[1])  # na mesma conexao
        self.assertEqual(self.discord.handshakes, 1)


if __name__ == "__main__":
    unittest.main()

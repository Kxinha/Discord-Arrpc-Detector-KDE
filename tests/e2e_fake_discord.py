#!/usr/bin/env python3
"""Ponta a ponta com um Discord falso.

Roda o daemon de verdade contra um socket IPC de mentira e confere o que ele
publica: um jogo "Wine" falso (argv[0] no formato Windows), iscas que so
carregam o nome do jogo nos argumentos, um socket velho na frente do bom, o
tempo de jogo contado desde que o processo nasceu, um Discord antigo que
recusa os campos de 2025 (status_display_type, links), e a presenca apagada
quando o jogo fecha e quando o daemon e encerrado. Depois, o arquivo de estado
que a bandeja le, uma pausa que segura a presenca ate acabar, o nivel
"minimal", que publica so o nome do app, e o SIGHUP da bandeja, que troca
opcoes sem reconectar e encerra uma pausa na hora.

Feito para rodar isolado, sem ver o Discord de verdade nem a rede:

    bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid --unshare-net \\
          --tmpfs /run/user/$(id -u) --tmpfs /tmp --die-with-parent \\
          python3 tests/e2e_fake_discord.py
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DAEMON = ROOT / "discord-game-presence.py"
CLIENT = "111111111111111111"
NEW_FIELDS = ("status_display_type", "details_url", "state_url")
EXE = r"Z:\Games\Jogo Falso\fakegame.exe"


def load_daemon():
    loader = importlib.machinery.SourceFileLoader("dgp", str(DAEMON))
    spec = importlib.util.spec_from_loader("dgp", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeDiscord:
    def __init__(self, path):
        self.events = []  # (hora, client_id, activity)
        self.refused = []  # atividades com campos que este Discord nao conhece
        self.handshakes = []  # (hora, client_id): cada conexao nova
        self.conns = []
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(16)
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            conn, _ = self.server.accept()
            with self.lock:
                self.conns.append(conn)
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def close_all(self, message):
        """O Vesktop 1.6.9 fecha todas as conexoes quando a janela recarrega."""
        with self.lock:
            conns, self.conns = self.conns, []
        for conn in conns:
            try:
                self.reply(conn, 2, {"code": 1000, "message": message})
                conn.close()
            except OSError:
                pass

    @staticmethod
    def recv(conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def reply(self, conn, op, body):
        data = json.dumps(body).encode()
        conn.sendall(struct.pack("<II", op, len(data)) + data)

    def serve(self, conn):
        client = None
        try:
            while True:
                op, length = struct.unpack("<II", self.recv(conn, 8))
                body = json.loads(self.recv(conn, length))
                if op == 0:
                    client = body["client_id"]
                    with self.lock:
                        self.handshakes.append((time.time(), client))
                    self.reply(conn, 1, {"cmd": "DISPATCH", "evt": "READY", "nonce": None,
                                         "data": {"v": 1}})
                elif op == 1:
                    # um evento solto antes da resposta, como o Discord faz as vezes
                    self.reply(conn, 1, {"cmd": "DISPATCH", "evt": "ACTIVITY_JOIN",
                                         "nonce": None, "data": {}})
                    activity = body["args"]["activity"]
                    if activity and any(k in activity for k in NEW_FIELDS):
                        with self.lock:
                            self.refused.append(activity)
                        self.reply(conn, 1, {"cmd": body["cmd"], "evt": "ERROR",
                                             "nonce": body.get("nonce"),
                                             "data": {"code": 4000,
                                                      "message": "child \"activity\" fails"}})
                        continue
                    with self.lock:
                        self.events.append((time.time(), client, activity))
                    self.reply(conn, 1, {"cmd": body["cmd"], "evt": None,
                                         "nonce": body.get("nonce"), "data": activity})
        except (ConnectionError, OSError, ValueError):
            conn.close()

    def wait(self, test, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for event in self.events:
                    if test(event):
                        return event
            time.sleep(0.2)
        return None


def spawn(argv0, *args):
    """Processo cujo argv[0] e o que pedirmos (exec -a do bash)."""
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
    return subprocess.Popen(["bash", "-c", f"exec -a '{argv0}' {quoted}"])


def main():
    if os.path.exists(f"/run/user/{os.getuid()}/bus"):
        sys.exit("rode dentro do bwrap (veja o docstring): o D-Bus real esta visivel")
    tmp = pathlib.Path(tempfile.mkdtemp())
    run, config = tmp / "run", tmp / "config"
    run.mkdir()
    config.mkdir()

    # socket velho na frente: arquivo existe, ninguem escuta
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(run / "discord-ipc-0"))
    stale.close()
    discord = FakeDiscord(str(run / "discord-ipc-1"))

    games = {key: {"enabled": False} for key in load_daemon().GAMES}
    games["fakegame.exe"] = {"client_id": CLIENT, "details": "Jogo Falso",
                             "state": "Jogando", "large_text": "Jogo Falso",
                             "status_display": "details"}
    (config / "discord-game-presence.json").write_text(json.dumps({"games": games}))

    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(f"[{'ok' if ok else 'FALHOU'}] {label}{(': ' + detail) if detail else ''}",
              flush=True)

    # iscas: o nome do jogo aparece, mas nenhuma delas e o jogo
    decoys = [
        spawn("tail", sys.executable, "-c", "import time; time.sleep(120)", EXE),
        spawn("python3", sys.executable, "-c", "import time; time.sleep(120)",
              "waitforexitandrun", EXE),
    ]
    game_started = time.time()
    game = spawn(EXE, "sleep", "120")
    time.sleep(3)  # o jogo ja estava aberto quando o daemon sobe

    status = run / "discord-game-presence" / "status.json"
    env = dict(os.environ, XDG_RUNTIME_DIR=str(run), XDG_CONFIG_HOME=str(config),
               PYTHONUNBUFFERED="1")
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    daemon_started = time.time()
    daemon = subprocess.Popen([sys.executable, "-I", str(DAEMON)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        shown = discord.wait(lambda e: e[1] == CLIENT and e[2], 20)
        check("publicou a presenca do jogo, pulando o socket velho", bool(shown))
        if shown:
            start = shown[2]["timestamps"]["start"]
            check("tempo conta desde que o jogo abriu, nao desde o daemon",
                  abs(start - game_started) <= 2 and start < daemon_started - 1,
                  f"start={start - game_started:+.1f}s do jogo, "
                  f"{start - daemon_started:+.1f}s do daemon")
            check("texto publicado", shown[2].get("details") == "Jogo Falso"
                  and shown[2].get("state") == "Jogando")
            try:
                showing = json.loads(status.read_text())["showing"]
            except (OSError, ValueError, KeyError):
                showing = {}
            check("arquivo de estado diz o que esta publicado",
                  showing.get("fakegame.exe", {}).get("name") == "Jogo Falso", str(showing))
            check("Discord antigo recusou os campos novos e saiu a versao sem eles",
                  bool(discord.refused) and not any(k in shown[2] for k in NEW_FIELDS),
                  f"{len(discord.refused)} recusa(s)")

        killed = time.time()
        game.kill()
        game.wait()
        closed = discord.wait(lambda e: e[1] == CLIENT and e[2] is None
                              and e[0] > killed, 15)
        delay = closed[0] - killed if closed else None
        check("apagou a presenca em ate 6 s depois do jogo fechar (iscas abertas)",
              bool(closed) and delay <= 6, f"{delay:.1f}s" if closed else "nao apagou")

        later = time.time()
        game = spawn(EXE, "sleep", "120")
        again = discord.wait(lambda e: e[1] == CLIENT and e[2] and e[0] > later, 15)
        check("detectou o jogo de novo ao reabrir", bool(again))
        if again:
            check("novo tempo para a nova sessao",
                  again[2]["timestamps"]["start"] >= int(later) - 1)
            check("sessao nova tenta os campos novos uma vez so",
                  len(discord.refused) <= 2, f"{len(discord.refused)} recusa(s)")

        published = [e for e in discord.events if e[2]]
        check("nada alem do jogo foi publicado",
              all(e[1] == CLIENT for e in published), f"{len(published)} publicacoes")

        mark = time.time()
        daemon.send_signal(signal.SIGTERM)
        code = daemon.wait(timeout=10)
        cleared = discord.wait(lambda e: e[1] == CLIENT and e[2] is None and e[0] >= mark, 2)
        check("SIGTERM apaga a presenca e sai com 0", bool(cleared) and code == 0,
              f"saida {code}")
        try:
            final = json.loads(status.read_text())
        except (OSError, ValueError):
            final = {}
        check("SIGTERM deixa o estado vazio e marcado como parado",
              final.get("stopped") is True and final.get("showing") == {}, str(final))
        logs = [daemon.stdout.read()]

        # pausa de 4 s, pedida pela bandeja, com o jogo aberto; e so o nome do app
        games["fakegame.exe"]["detail"] = "minimal"
        (config / "discord-game-presence.json").write_text(json.dumps(
            {"games": games, "pause": {"until": time.time() + 4}}))
        mark = time.time()
        daemon = subprocess.Popen([sys.executable, "-I", str(DAEMON)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        early = discord.wait(lambda e: e[1] == CLIENT and e[2] and e[0] > mark, 3)
        check("nada publicado durante a pausa", early is None)
        with discord.lock:
            clears = [e for e in discord.events if e[0] > mark and e[2] is None]
        check("depois de sair limpo, o novo daemon nao reconecta so para apagar",
              not clears, f"{len(clears)} conexao(oes) de limpeza")
        try:
            paused = json.loads(status.read_text()).get("paused")
        except (OSError, ValueError):
            paused = None
        check("arquivo de estado mostra a pausa", paused is True, str(paused))
        late = discord.wait(lambda e: e[1] == CLIENT and e[2] and e[0] > mark, 12)
        check("publicou sozinho quando a pausa acabou",
              bool(late) and late[0] - mark >= 3.5,
              f"{late[0] - mark:.1f}s depois" if late else "nao publicou")
        if late:
            check('"minimal" publica so o nome do app',
                  late[2].get("details") == "Jogo Falso" and "state" not in late[2]
                  and late[2]["timestamps"].get("start", 0) <= late[0], str(late[2]))

        # opcoes trocadas pela bandeja: SIGHUP, sem reiniciar nem reconectar
        games["fakegame.exe"].pop("detail")
        (config / "discord-game-presence.json").write_text(json.dumps({"games": games}))
        mark = time.time()
        daemon.send_signal(signal.SIGHUP)
        full = discord.wait(lambda e: e[1] == CLIENT and e[2] and e[0] > mark
                            and e[2].get("state") == "Jogando", 6)
        with discord.lock:
            new = [h for h in discord.handshakes if h[0] > mark]
        check("SIGHUP aplica as opcoes na mesma conexao",
              bool(full) and not new and daemon.poll() is None,
              (f"{full[0] - mark:.1f}s, " if full else "nao republicou, ")
              + f"{len(new)} conexao(oes) nova(s)")

        # a janela do Vesktop recarrega: todas as conexoes caem, o jogo segue igual
        mark = time.time()
        discord.close_all("Renderer reloaded")
        back = discord.wait(lambda e: e[1] == CLIENT and e[2] and e[0] > mark, 12)
        check("volta sozinho quando o Discord fecha a conexao (Renderer reloaded)",
              bool(back), f"{back[0] - mark:.1f}s" if back else "nao voltou")
        daemon.send_signal(signal.SIGTERM)
        logs.append(daemon.stdout.read())
        daemon.wait(timeout=10)

        # pausa "ate eu retomar": a bandeja tira a pausa e acorda o daemon
        (config / "discord-game-presence.json").write_text(json.dumps(
            {"games": games, "pause": {"until": None}}))
        daemon = subprocess.Popen([sys.executable, "-I", str(DAEMON)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(1.5)
        (config / "discord-game-presence.json").write_text(json.dumps({"games": games}))
        mark = time.time()
        daemon.send_signal(signal.SIGHUP)
        back = discord.wait(lambda e: e[1] == CLIENT and e[2] and e[0] > mark, 10)
        check("retomar com SIGHUP publica na hora, sem esperar a conferencia",
              bool(back) and back[0] - mark < 3,
              f"{back[0] - mark:.1f}s" if back else "nao publicou")
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=10)
    finally:
        if daemon.poll() is None:
            daemon.kill()
        for proc in decoys + [game]:
            proc.kill()
        out = daemon.stdout.read() if daemon.stdout else ""
        print("\n--- log do daemon\n" + "\n".join(locals().get("logs", []) + [out]).strip())
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

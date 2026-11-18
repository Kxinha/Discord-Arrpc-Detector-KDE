#!/usr/bin/env python3
"""Rich Presence de jogos que o Discord nao detecta no Linux.

A base de jogos detectaveis do Discord so lista executaveis win32/darwin (ou
nenhum), entao nem o cliente nativo do RuneScape nem jogos rodando sob Proton
sao reconhecidos. Este daemon procura os processos, fala o protocolo IPC do
Discord direto e publica a presenca enquanto o jogo estiver aberto.

    discord-game-presence            roda o daemon
    discord-game-presence --scan     mostra o que seria detectado agora, e por que
    discord-game-presence --version
    discord-game-presence --check-update   diz se saiu versao nova
    discord-game-presence --update         instala a versao nova (sha256 conferido)
"""
import fcntl
import glob
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

VERSION = "1.3.0"

PRISM = pathlib.Path.home() / ".local/share/PrismLauncher/instances"


def log(message):
    """Uma linha no journal: journalctl --user -u discord-game-presence."""
    print(message, flush=True)


# ------------------------------------------------------------- processos
# Uma foto de /proc a cada dois segundos, dividida por todas as threads. Cada
# processo vira um Proc com os nomes pelos quais ele pode ser reconhecido: o
# programa que esta rodando (argv[0], ja no formato Windows quando e um jogo
# sob Wine), o comm do kernel, o executavel real e, para interpretadores, o
# script que eles rodam. Casar por esses nomes, e nao por um trecho qualquer da
# linha de comando, e o que separa o jogo de quem so fala dele: um grep, um
# editor aberto no autoexec.cfg, um tail no log, os wrappers do Proton.

SHELL_NAMES = {"bash", "sh", "zsh", "dash", "fish", "ksh", "csh", "tcsh"}

# Programas que carregam o caminho de um jogo nos argumentos sem ser o jogo.
# So valem para as regras por trecho de linha de comando ("cmdline").
VIEWERS = {
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "fd", "find", "locate",
    "tail", "head", "less", "more", "cat", "bat", "tee", "sed", "awk",
    "vi", "vim", "nvim", "nano", "micro", "emacs", "kate", "kwrite", "gedit",
    "gnome-text-editor", "mousepad", "code", "codium", "journalctl", "pgrep",
    "pkill", "pidof", "ps", "top", "htop", "btop", "watch", "strace",
    "ltrace", "gdb", "xdg-open", "kioclient", "kioclient5", "kfmclient",
    "dolphin", "nautilus", "thunar", "konsole", "git", "rsync", "cp", "mv",
    "ln", "rm", "du", "ls", "stat", "file", "7z", "tar", "unzip",
}

# Para estes o programa de verdade e o script que eles carregam.
INTERPRETERS = {"sh", "bash", "dash", "zsh", "python", "python2", "python3",
                "java", "javaw", "mono", "node", "perl", "ruby", "lua",
                "luajit", "love", "wine", "wine64"}

# Sufixos que nao mudam o que o programa e: "Jogo.x86_64" e "jogo.exe"
# tambem atendem por "jogo".
NAME_SUFFIXES = (".exe", ".x86_64", ".x86", ".sh")

SNAPSHOT_TTL = 2
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _boot_time():
    try:
        for line in pathlib.Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


BOOT_TIME = _boot_time()


def basename(path):
    """Ultimo pedaco de um caminho Unix ou Windows, em minusculas."""
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1].lower()


def interpreter_target(prog, argv):
    """O script que um interpretador esta rodando, se der para saber."""
    if prog in ("java", "javaw"):
        if "-jar" in argv[1:-1]:
            return argv[argv.index("-jar") + 1]
        return None  # classe principal nao diz nada sobre o jogo
    for arg in argv[1:]:
        if not arg.startswith("-"):
            return arg
    return None


def proc_names(argv, comm, exe):
    """Todos os nomes pelos quais um processo pode ser reconhecido."""
    first = basename(argv[0])
    prog = re.sub(r"[\d.]+$", "", first) if first.startswith("python") else first
    candidates = [first, comm.lower()]
    # Alguns programas reescrevem a linha de comando como uma string so, com
    # espacos ("C:\Jogo\jogo.exe -arg"): o executavel e o que termina em .exe
    if " " in first or (len(argv) == 1 and " " in argv[0]):
        # find(), nao regex: a linha de um renderer do Chrome tem milhares de
        # caracteres, e uma busca com retrocesso custava 20 ms por foto
        low = argv[0].lower()
        end = low.find(".exe") + 4
        if end >= 4 and (end == len(low) or low[end].isspace()):
            candidates.append(basename(argv[0][:end]))
        else:
            candidates.append(basename(argv[0].split(" ")[0]))
    if exe:
        candidates.append(basename(exe.removesuffix(" (deleted)")))
    if prog in INTERPRETERS:
        script = interpreter_target(prog, argv)
        if script:
            candidates.append(basename(script))
    names = set()
    for name in filter(None, candidates):
        names.add(name)
        for suffix in NAME_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                names.add(name[:-len(suffix)])
    return prog, names


def proc_start(pid):
    """Hora (epoch) em que o processo nasceu, ou None."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None
    return BOOT_TIME + ticks / CLK_TCK if BOOT_TIME else None


class Proc:
    __slots__ = ("pid", "argv", "cmdline", "comm", "exe", "prog", "names",
                 "argstr", "_start")

    def __init__(self, pid, argv, comm="", exe=""):
        self.pid = pid
        self.argv = argv
        self.cmdline = " ".join(argv).strip()
        self.comm = comm
        self.exe = exe
        self.prog, self.names = proc_names(argv, comm, exe)
        self.argstr = (" ".join(argv[1:]) if len(argv) > 1
                       else argv[0].partition(" ")[2])
        self._start = False

    @property
    def start(self):
        if self._start is False:
            self._start = proc_start(self.pid)
        return self._start

    def __repr__(self):
        return f"Proc({self.pid}, {self.argv[0]!r})"


def is_shell_oneliner(argv):
    """`bash -c ...`: o padrao procurado aparece na linha de quem procura."""
    return (basename(argv[0]) in SHELL_NAMES
            and any(a.startswith("-") and "c" in a for a in argv[1:2]))


_SNAP = {"stamp": -1e9, "procs": []}
_SNAP_LOCK = threading.Lock()


def processes():
    """Os processos abertos agora (cache de dois segundos, entre threads)."""
    with _SNAP_LOCK:
        now = time.monotonic()
        if now - _SNAP["stamp"] < SNAPSHOT_TTL:
            return _SNAP["procs"]
        procs, mine = [], os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == mine:
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    raw = f.read()
                with open(f"/proc/{entry}/comm", errors="replace") as f:
                    comm = f.read().strip()
            except OSError:
                continue  # o processo morreu no meio da leitura
            argv = raw.decode("utf-8", "replace").split("\0")
            while argv and not argv[-1]:
                argv.pop()
            if not argv or not argv[0]:
                continue  # thread do kernel, ou zumbi
            if is_shell_oneliner(argv):
                continue
            try:
                exe = os.readlink(f"/proc/{entry}/exe")
            except OSError:
                exe = ""
            procs.append(Proc(int(entry), argv, comm, exe))
        _SNAP.update(stamp=now, procs=procs)
        return procs


def process_cmdlines():
    """Linhas de comando dos processos (compatibilidade com a versao 1.1)."""
    return [p.cmdline for p in processes()]


def _as_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else [v for v in value if v]


def entry_rules(key, game):
    """Como reconhecer o processo de uma entrada.

    Uma entrada pode dizer explicitamente o que procurar, com uma regra ou uma
    lista delas em "match" (ou os mesmos campos soltos na entrada):
      "exe":     nome do executavel (ou lista), comparado inteiro: argv[0],
                 comm, /proc/<pid>/exe ou o script de um interpretador;
      "args":    trecho(s) que tambem precisam estar nos argumentos;
      "cmdline": trecho da linha de comando, o jeito antigo.
    Sem nada disso, a chave decide: um nome simples ("rs2client",
    "jogo.exe") vira "exe"; um caminho ou trecho com "/", "\\", ":" ou espaco
    continua valendo como trecho de linha de comando.
    """
    match = game.get("match")
    if match is None:
        fields = {k: game[k] for k in ("exe", "args", "cmdline") if k in game}
        if fields:
            match = fields
        elif re.search(r"[/\\:\s]", key):
            match = {"cmdline": key}
        else:
            match = {"exe": key}
    if isinstance(match, dict):
        match = [match]
    rules = []
    for rule in match or []:
        if not isinstance(rule, dict):
            continue
        rules.append({"exe": [e.lower() for e in _as_list(rule.get("exe"))],
                      "args": _as_list(rule.get("args")),
                      "cmdline": rule.get("cmdline") or None})
    return rules


def name_matches(proc, name):
    if name in proc.names or name.removesuffix(".exe") in proc.names:
        return True
    # o kernel corta o comm em 15 caracteres; Wine poe ali o nome do .exe
    return len(name) >= 15 and proc.comm.lower() == name[:15]


def proc_matches(proc, rule):
    if not rule["exe"] and not rule["cmdline"]:
        return False
    if rule["exe"] and not any(name_matches(proc, e) for e in rule["exe"]):
        return False
    if rule["cmdline"] and (proc.prog in VIEWERS
                            or rule["cmdline"] not in proc.cmdline):
        return False
    return all(a in proc.argstr for a in rule["args"])


def detect(key, game):
    """O que prova que o app esta aberto: lista de Proc, ou o que a funcao
    "running" da entrada devolver. Vazio/None/False quando nao esta."""
    check = game.get("running")
    if callable(check):
        return check()
    if isinstance(check, str):
        key = check
    rules = entry_rules(key, game)
    return [p for p in processes() if any(proc_matches(p, r) for r in rules)]


def is_running(key, game):
    return bool(detect(key, game))


def running(proc):
    """Compatibilidade: um trecho/nome de processo, ou uma funcao."""
    if callable(proc):
        return proc()
    return bool(detect(proc, {}))


def hit_start(hit):
    """Quando o app abriu, a partir do que detect() devolveu."""
    items = hit if isinstance(hit, (list, tuple)) else [hit]
    starts = []
    for item in items:
        if isinstance(item, Proc):
            starts.append(item.start)
        elif isinstance(item, int) and not isinstance(item, bool):
            starts.append(proc_start(item))
    starts = [s for s in starts if s]
    return min(starts) if starts else None


# ------------------------------------------------------- base do Discord
# Mesmas regras do arRPC (src/process/index.js) para casar um processo com a
# base de jogos detectaveis, com duas protecoes a mais: um nome que pertence a
# varios jogos ("game.exe", "client.exe") so vale se um pedaco maior do caminho
# desempatar, e entre varios acertos ganha o mais especifico.

GENERIC_NAMES = {
    "main", "game", "play", "start", "run", "home", "launcher", "launch",
    "update", "updater", "setup", "install", "installer", "client", "server",
    "player", "data", "test", "tools", "tool", "core", "base", "shell",
    "python", "python3", "bash", "node", "java", "electron", "wine", "wine64",
    "steam", "steamwebhelper", "containers", "default", "system", "desktop",
}


def path_variants(path):
    """Sufixos do caminho como o arRPC compara: "a/b/c.exe" da "c.exe",
    "b/c.exe" e "a/b/c.exe", mais as versoes sem "64"."""
    parts = [p for p in path.lower().replace("\\", "/").split("/") if p]
    out = ["/".join(parts[-i:]) for i in range(1, len(parts) + 1)]
    for p in list(out):
        for old in ("64", ".x64", "x64", "_64"):
            out.append(p.replace(old, "", 1))
    return list(dict.fromkeys(out))


def native_stems(name):
    """Nome sem extensao e sem marca de 64 bits: "Jogo.x86_64" -> "jogo"."""
    stem = name.lower()
    for suffix in (".exe", ".x86_64", ".x86", ".sh", ".bin", ".app"):
        stem = stem.removesuffix(suffix)
    stems = {stem}
    for mark in ("_x64", "-x64", "x64", "_64", "-64", "64"):
        if stem.endswith(mark) and len(stem) > len(mark):
            stems.add(stem[:-len(mark)])
    return stems


def detectable_index(apps):
    """Indices da base: por nome cadastrado, quantos apps usam cada nome de
    executavel, e por "raiz" do nome, para jogos nativos."""
    index, owners, stems = {}, {}, {}
    for app in apps:
        for x in app.get("executables") or []:
            name = (x.get("name") or "").lower()
            if not name or x.get("is_launcher"):
                continue
            index.setdefault(name.lstrip(">"), []).append((app, x))
            base_name = name.lstrip(">").split("/")[-1]
            owners.setdefault(base_name, set()).add(app["id"])
            if x.get("os") == "win32" and not x.get("arguments"):
                for stem in native_stems(base_name):
                    stems.setdefault(stem, {})[app["id"]] = (app, x)
    return {"index": index, "owners": owners, "stems": stems}


def match_detectable(paths, args, db, owners=None):
    """Melhor app da base para um processo: (app, nome casado) ou None.

    paths: caminhos do processo (argv[0] e o executavel real); args: o resto
    da linha de comando. Se nada casar e o processo for nativo, tenta a raiz
    do nome contra os .exe da base ("Unturned.x86_64" com "unturned.exe"),
    mas so quando essa raiz pertence a um jogo so.
    """
    if owners is not None:  # chamada no formato antigo (index, owners)
        db = {"index": db, "owners": owners, "stems": {}}
    index, owners = db["index"], db["owners"]
    best = None
    for path in paths:
        variants = path_variants(path)
        if not variants:
            continue
        for variant in variants:
            for app, x in index.get(variant, []):
                name = x["name"].lower()
                if name.startswith(">") and name[1:] != variants[0]:
                    continue  # ">" exige o nome exato do executavel
                if x.get("arguments") and x["arguments"] not in args:
                    continue
                depth = name.lstrip(">").count("/") + 1
                base_name = name.lstrip(">").split("/")[-1]
                stem = base_name.removesuffix(".exe")
                ambiguous = len(owners.get(base_name, ())) > 1
                if depth == 1 and not x.get("arguments") and (
                        ambiguous or stem in GENERIC_NAMES or len(stem) < 4):
                    continue  # nome comum demais para dizer qual jogo e
                score = (depth, bool(x.get("arguments")), variant == variants[0])
                if best is None or score > best[0]:
                    best = (score, app, name)
    if best:
        return best[1], best[2]
    for path in paths:
        name = basename(path)
        if not name or name.endswith(".exe"):
            continue
        for stem in native_stems(name):
            hits = db.get("stems", {}).get(stem, {})
            if len(hits) == 1 and len(stem) >= 4 and stem not in GENERIC_NAMES:
                app, x = next(iter(hits.values()))
                return app, x["name"].lower()
    return None


# ------------------------------------------------------------- MPRIS
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_TTL = 1
_BUS = {"stamp": -1e9, "players": []}
_PROPS = {}
_BUS_LOCK = threading.Lock()


def busctl(*args):
    try:
        r = subprocess.run(["busctl", "--user", *args], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def unwrap(value):
    """Tira o {"type": ..., "data": ...} que o busctl --json poe em tudo."""
    while isinstance(value, dict) and set(value) == {"type", "data"}:
        value = value["data"]
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


def mpris_players():
    """[(bus name, processo dono)] dos players MPRIS abertos."""
    with _BUS_LOCK:
        if time.monotonic() - _BUS["stamp"] < MPRIS_TTL:
            return _BUS["players"]
        players = []
        for line in (busctl("--list", "--no-legend") or "").splitlines():
            cols = line.split()
            if cols and cols[0].startswith(MPRIS_PREFIX):
                players.append((cols[0], cols[2] if len(cols) > 2 else ""))
        _BUS.update(stamp=time.monotonic(), players=players)
        return players


def mpris_names(prefix):
    """Bus names MPRIS que comecam com o prefixo (o do Firefox muda a cada boot)."""
    full = MPRIS_PREFIX + prefix
    return [name for name, _ in mpris_players() if name.startswith(full)]


def mpris_props(player):
    """Todas as propriedades do player numa leitura so (metadata, estado e
    posicao do mesmo instante), com cache de um segundo."""
    now = time.monotonic()
    with _BUS_LOCK:
        cached = _PROPS.get(player)
        if cached and now - cached[0] < MPRIS_TTL:
            return cached[1]
    out = busctl("--json=short", "call", player, "/org/mpris/MediaPlayer2",
                 "org.freedesktop.DBus.Properties", "GetAll", "s",
                 "org.mpris.MediaPlayer2.Player")
    props = {}
    if out:
        try:
            data = json.loads(out)["data"]
            props = unwrap(data[0] if isinstance(data, list) and data else data)
        except (ValueError, KeyError, TypeError):
            props = {}
    props = props if isinstance(props, dict) else {}
    with _BUS_LOCK:
        _PROPS[player] = (now, props)
    return props


def mpris(prop, player="org.mpris.MediaPlayer2.Stremio"):
    """Uma propriedade; Position e sempre lida na hora."""
    if prop != "Position":
        return mpris_props(player).get(prop) if player else None
    out = busctl("--json=short", "get-property", player, "/org/mpris/MediaPlayer2",
                 "org.mpris.MediaPlayer2.Player", prop)
    try:
        return unwrap(json.loads(out)) if out else None
    except ValueError:
        return None


def mpris_meta(player):
    meta = mpris_props(player).get("Metadata")
    return meta if isinstance(meta, dict) else {}


def first(value):
    return value[0] if isinstance(value, list) and value else (value or None)


_LENGTH_CACHE = {}
_PAUSED_SINCE = {}

# Uma aba de video pausada continua sendo um player MPRIS enquanto existir, e
# a presenca ficava pendurada por horas dizendo "(pausado)". Depois desse
# tempo parado, ninguem esta assistindo: a presenca sai do perfil e volta
# assim que der play.
PAUSE_GRACE = 300


def media_idle(player):
    """True quando o player existe mas esta parado ha tempo demais."""
    if mpris("PlaybackStatus", player) == "Playing":
        _PAUSED_SINCE.pop(player, None)
        return False
    started = _PAUSED_SINCE.setdefault(player, time.monotonic())
    return time.monotonic() - started > PAUSE_GRACE


def track_length(key, meta, video=None):
    """Duracao da faixa, em microssegundos.

    O Firefox publica mpris:length de forma intermitente: some do metadata no
    meio da reproducao. O ultimo valor visto fica guardado por faixa e, para
    video do YouTube, a duracao ainda pode ser lida da propria pagina.
    """
    length = meta.get("mpris:length")
    if length:
        _LENGTH_CACHE[key] = length
        return length
    if key in _LENGTH_CACHE:
        return _LENGTH_CACHE[key]
    if video:
        length = youtube_length(video)
        if length:
            _LENGTH_CACHE[key] = length
    return length


def fetch_json(url):
    """GET simples que devolve JSON."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def youtube_length(video):
    """Duracao de um video do YouTube, lida da pagina (em microssegundos)."""
    try:
        request = urllib.request.Request(
            f"https://www.youtube.com/watch?v={video}",
            headers={"User-Agent": "Mozilla/5.0"})
        # a duracao so aparece perto de 1 MB de HTML, entao nao adianta cortar cedo
        with urllib.request.urlopen(request, timeout=8) as r:
            page = r.read(2_000_000).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'"lengthSeconds":"(\d+)"', page)
    return int(m.group(1)) * 1_000_000 if m else None


def mpris_position(player, tries=3):
    """Posicao informada pelo player, ou None quando ele nao informa.

    O Firefox responde zero em parte das leituras mesmo com o video correndo
    (duas chamadas seguidas podem dar 16:51 e depois 0), entao vale insistir
    algumas vezes antes de dar a posicao por desconhecida. Um zero nunca vale
    como inicio da faixa: fabricar posicao a partir dele fazia a barra mentir
    depois de qualquer seek, que e justamente quando ela importa.
    """
    reported = mpris_props(player).get("Position")
    if reported:
        return reported
    for attempt in range(tries):
        reported = mpris("Position", player)
        if reported:
            return reported
        if attempt + 1 < tries:
            time.sleep(0.3)
    return None


# O Firefox so informa a posicao enquanto a aba esta em uso: nesses momentos
# ela vem certinha (16:47, 16:49, 16:51...), e no resto do tempo ele devolve
# zero. Guardar a ultima leitura verdadeira e seguir dali cobre os buracos, e
# voltar para a aba (ou dar um seek) faz o Firefox reportar de novo,
# corrigindo a conta sozinho. Sem nenhuma leitura verdadeira nao ha o que
# projetar: ai a presenca fica sem tempo, em vez de inventar um.
_POSITION_ANCHOR = {}
ANCHOR_MAX_AGE = 1800


def anchored_position(title, reported, playing=True):
    """Posicao real, ou a projecao da ultima leitura real enquanto ela vale."""
    now = time.monotonic()
    if reported:
        _POSITION_ANCHOR[title] = (reported, now)
        return reported

    anchor = _POSITION_ANCHOR.get(title)
    if not anchor:
        return None
    position, seen = anchor
    if now - seen > ANCHOR_MAX_AGE:  # velha demais para ainda valer
        return None
    if not playing:  # em pausa a posicao nao anda: so adia a ancora
        _POSITION_ANCHOR[title] = (position, now)
        return position
    return position + int((now - seen) * 1_000_000)


def position_elsewhere(title, exclude=None):
    """Posicao e duracao vindas de outro player que anuncie o mesmo titulo.

    O Firefox informa a posicao de forma intermitente. A extensao Plasma
    Browser Integration e os navegadores baseados em Chromium publicam um
    player MPRIS proprio com os numeros de verdade; se um deles estiver com o
    mesmo titulo, a barra passa a ser exata sem precisar de configuracao.
    """
    for name, _process in mpris_players():
        if name == exclude or "firefox" in name:
            continue
        props = mpris_props(name)
        meta = props.get("Metadata") or {}
        if not isinstance(meta, dict) or (meta.get("xesam:title") or "") != title:
            continue
        position = props.get("Position")
        if position:
            return position, meta.get("mpris:length")
    return None, None


def media_timestamps(title, position, length, player=None):
    """Barra de progresso, so quando alguem souber a posicao de verdade.

    Sem posicao nao adianta inventar: o Discord ja mostra um contador proprio
    a partir do momento em que a atividade aparece, e somar a ele um tempo
    fabricado nao aproxima nada. A duracao entao vai para o texto.
    """
    if not position:  # o player nao informa; talvez outro esteja com o mesmo
        position, other_length = position_elsewhere(title, player)
        length = length or other_length
    if not (position and length) or position > length or length >= LIVE_LENGTH:
        return None
    now = time.time()
    return {"start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000)}


def duration_label(length):
    """25:36, ou 1:49:15 quando passa de uma hora."""
    if not length:
        return ""
    total = int(length / 1_000_000)
    if total >= 3600:
        return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


# ------------------------------------------------ YouTube em qualquer navegador
# O Firefox poe a URL da aba no MPRIS (xesam:url). O Chrome e os outros
# Chromium nao poem: publicam so titulo, canal, duracao e posicao. Para eles a
# pagina sai do arquivo de sessao do proprio navegador (Sessions/Session_*),
# que guarda a URL e o titulo atuais de cada aba aberta e e regravado um ou
# dois segundos depois de qualquer mudanca. O historico fica de reserva
# (sessao criptografada, por exemplo), mas nao basta sozinho: ele guarda o
# titulo so no comeco da visita, e o YouTube Music troca de faixa sem trocar
# de visita, entao la a musica nova continua com o titulo "YouTube Music".

CHROMIUM_PROCESSES = {"chrome", "chromium", "chromium-browser", "brave",
                      "brave-browser", "vivaldi-bin", "msedge", "opera",
                      "thorium"}
CHROMIUM_DIRS = ["google-chrome", "google-chrome-beta", "google-chrome-unstable",
                 "chromium", "BraveSoftware/Brave-Browser", "vivaldi",
                 "microsoft-edge", "opera", "thorium"]
CHROMIUM_FLATPAKS = {"com.google.Chrome": "google-chrome",
                     "org.chromium.Chromium": "chromium",
                     "com.brave.Browser": "BraveSoftware/Brave-Browser",
                     "com.vivaldi.Vivaldi": "vivaldi",
                     "com.microsoft.Edge": "microsoft-edge"}
# fim do titulo da janela: "<titulo da aba ativa> - Google Chrome"
BROWSER_SUFFIXES = (" - Google Chrome", " - Chromium", " - Brave", " - Vivaldi",
                    " - Microsoft Edge", " - Opera", " - Thorium")
HISTORY_MAX_AGE = 24 * 3600
WEBKIT_EPOCH = 11644473600  # segundos entre 1601-01-01 e 1970-01-01
HISTORY_SQL = """
    SELECT url, title, last_visit_time FROM urls
    WHERE last_visit_time > ? AND instr(title, ?) > 0
      AND (url LIKE 'https://www.youtube.com/%' OR url LIKE 'https://m.youtube.com/%'
           OR url LIKE 'https://music.youtube.com/%' OR url LIKE 'https://youtu.be/%')
    ORDER BY last_visit_time DESC LIMIT 20
"""

YT_ICON = ("https://cdn.discordapp.com/app-icons/1538427046760423474/"
           "3f7a95b3c425b18bda601ff2f2e44b2d.png")
YTM_ICON = ("https://cdn.discordapp.com/app-icons/1537975657735262368/"
            "1637a3eccd8d9c22acf127e814bf088b.png")

_PROFILES = {"stamp": -1e9, "dirs": []}
_HISTORY_CACHE = {}


def config_home():
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME",
                                       pathlib.Path.home() / ".config"))


def chromium_profiles():
    """Pastas de perfil de todos os navegadores Chromium, nativos e Flatpak."""
    if time.monotonic() - _PROFILES["stamp"] < 60:
        return _PROFILES["dirs"]
    roots = [config_home() / d for d in CHROMIUM_DIRS]
    roots += [pathlib.Path.home() / ".var/app" / app / "config" / d
              for app, d in CHROMIUM_FLATPAKS.items()]
    dirs = []
    for root in roots:
        for profile in ("Default", "Profile *"):
            dirs += [pathlib.Path(p) for p in sorted(glob.glob(str(root / profile)))]
    _PROFILES.update(stamp=time.monotonic(), dirs=dirs)
    return dirs


def history_files():
    """Os arquivos History de todos os perfis de navegadores Chromium."""
    return [p / "History" for p in chromium_profiles() if (p / "History").is_file()]


def youtube_video_id(url):
    """Id do video em qualquer forma de link do YouTube, ou None."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    video = None
    if host == "youtu.be":
        video = parts.path.lstrip("/")[:11]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        video = (urllib.parse.parse_qs(parts.query).get("v") or [None])[0]
        if not video:
            m = re.match(r"/(?:shorts|live|embed|v)/([\w-]{11})", parts.path)
            video = m.group(1) if m else None
    return video if video and re.fullmatch(r"[\w-]{11}", video) else None


def media_service(url):
    """"ytm", "youtube" ou None, pelo endereco da pagina."""
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return None
    if host == "music.youtube.com":
        return "ytm"
    if (host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")
            or host.endswith("youtube-nocookie.com")):
        return "youtube"
    return None


def url_path(url):
    try:
        return urllib.parse.urlsplit(url or "").path.rstrip("/") or "/"
    except ValueError:
        return "/"


def is_live_page(url):
    """/@canal/live, /channel/<id>/live ou /live/<id>."""
    path = url_path(url)
    return media_service(url) == "youtube" and (path.endswith("/live")
                                                or path.startswith("/live/"))


def is_player_page(url):
    """Pagina que toca um video ou musica (e nao uma lista, busca ou canal)."""
    path = url_path(url)
    return bool(media_service(url)) and (
        path in ("/watch",) or path.startswith(("/shorts/", "/live/"))
        or is_live_page(url))


def page_title(page):
    """Titulo da pagina sem o "(3) " das notificacoes e sem o " - YouTube"."""
    page = re.sub(r"^\(\d+\+?\)\s*", "", page or "")
    for suffix in (" - YouTube", " | YouTube Music", " - YouTube Music"):
        if page.endswith(suffix):
            return page[:-len(suffix)]
    return page


def page_title_is(page, title):
    """O titulo da pagina e exatamente o do que esta tocando."""
    return page_title(page) == title


def history_url(title, files=None):
    """URL do YouTube que o historico dos Chromium associa ao titulo exato."""
    if not title:
        return ""
    now = time.monotonic()
    cached = _HISTORY_CACHE.get(title)
    if cached and (cached[0] or now - cached[1] < 5):
        return cached[0]  # achado fica; nao achado tenta de novo em 5 s
    cutoff = int((time.time() + WEBKIT_EPOCH - HISTORY_MAX_AGE) * 1_000_000)
    best = None
    for path in (files if files is not None else history_files()):
        try:
            # immutable: o navegador mantem o arquivo travado enquanto roda
            db = sqlite3.connect(f"{pathlib.Path(path).as_uri()}?mode=ro&immutable=1",
                                 uri=True, timeout=1)
            try:
                rows = db.execute(HISTORY_SQL, (cutoff, title)).fetchall()
            finally:
                db.close()
        except (sqlite3.Error, ValueError, OSError):
            continue  # arquivo no meio de uma escrita: fica para a proxima
        for url, page, visit in rows:
            # so o titulo exato: "Intro" nao pode virar "Best Intro Ever"
            if youtube_video_id(url) and page_title_is(page, title):
                if best is None or (visit or 0) > best[0]:
                    best = (visit or 0, url)
    url = best[1] if best else ""
    if len(_HISTORY_CACHE) > 500:
        _HISTORY_CACHE.clear()
    _HISTORY_CACHE[title] = (url, now)
    return url


# ------------------------------------------------ abas abertas do Chrome
# Formato SNSS, de components/sessions/core no codigo do Chromium: cabecalho
# "SNSS" + versao (3 em texto puro; a 5 e criptografada e fica de fora),
# depois comandos [tamanho u16][id u8][dados]. O arquivo so cresce: cada
# mudanca vira mais um comando no fim, e o estado atual e o resultado de
# aplicar todos em ordem.
SNSS_SIGNATURE = 0x53534E53
SNSS_VERSION = 3
CMD_TAB_WINDOW = 0         # (janela, aba)
CMD_TAB_INDEX = 2          # (aba, posicao na janela)
CMD_PRUNED_BACK = 5        # (aba, indice): navegacoes daqui em diante sairam
CMD_NAVIGATION = 6         # pickle: aba, indice, url, titulo, ...
CMD_SELECTED_NAV = 7       # (aba, indice da navegacao atual)
CMD_SELECTED_TAB = 8       # (janela, posicao da aba selecionada)
CMD_PRUNED_FRONT = 11      # (aba, quantas sairam do comeco)
CMD_TAB_CLOSED = 16        # (aba, hora)
CMD_WINDOW_CLOSED = 17     # (janela, hora)
CMD_PRUNED = 24            # (aba, indice, quantas)
SESSION_MAX_BYTES = 16 << 20

_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


class Tab:
    """Uma aba aberta: url e titulo da navegacao atual."""
    __slots__ = ("key", "window", "index", "selected", "url", "title")

    def __init__(self, key, window, index, selected, url, title):
        self.key, self.window, self.index = key, window, index
        self.selected, self.url, self.title = selected, url, title

    def __repr__(self):
        return f"Tab({self.url!r}, {self.title!r}, selected={self.selected})"


def _pickle_string(buf, pos, wide=False):
    """string (ou string16) de um base::Pickle: tamanho int32 + dados
    alinhados em 4 bytes."""
    (count,) = struct.unpack_from("<i", buf, pos)
    pos += 4
    size = count * 2 if wide else count
    if count < 0 or pos + size > len(buf):
        raise ValueError("string maior que o comando")
    raw = bytes(buf[pos:pos + size])
    return raw.decode("utf-16-le" if wide else "utf-8", "replace"), pos + ((size + 3) & ~3)


def _prune(info, index, count=None):
    """Mesmo ajuste que o Chromium faz ao podar navegacoes de uma aba."""
    navs = info["navs"]
    if count is None:  # do indice ate o fim
        info["navs"] = {i: v for i, v in navs.items() if i < index}
        return
    current = info["nav"]
    if index <= current < index + count:
        info["nav"] = index - 1
    elif current >= index + count:
        info["nav"] = current - count
    info["navs"] = {(i - count if i >= index + count else i): v
                    for i, v in navs.items() if not index <= i < index + count}


def parse_session(data, profile=""):
    """Abas abertas descritas por um arquivo de sessao SNSS, em ordem."""
    if len(data) < 8 or struct.unpack_from("<ii", data) != (SNSS_SIGNATURE, SNSS_VERSION):
        return []
    tabs, selected, closed_windows = {}, {}, set()

    def tab(tab_id):
        return tabs.setdefault(tab_id, {"window": None, "index": 0, "nav": 0, "navs": {}})

    pos = 8
    while pos + 3 <= len(data):
        (size,) = struct.unpack_from("<H", data, pos)
        if size < 1 or pos + 2 + size > len(data):
            break  # ultimo comando pela metade: o navegador ainda esta gravando
        command, body = data[pos + 2], data[pos + 3:pos + 2 + size]
        pos += 2 + size
        try:
            if command == CMD_NAVIGATION:
                # o pickle comeca pelo proprio tamanho (uint32)
                tab_id, index = struct.unpack_from("<ii", body, 4)
                url, at = _pickle_string(body, 12)
                title, _ = _pickle_string(body, at, wide=True)
                tab(tab_id)["navs"][index] = (url, title)
            elif command == CMD_TAB_WINDOW:
                window, tab_id = struct.unpack_from("<ii", body)
                tab(tab_id)["window"] = window
            elif command == CMD_TAB_INDEX:
                tab_id, index = struct.unpack_from("<ii", body)
                tab(tab_id)["index"] = index
            elif command == CMD_SELECTED_NAV:
                tab_id, index = struct.unpack_from("<ii", body)
                tab(tab_id)["nav"] = index
            elif command == CMD_SELECTED_TAB:
                window, index = struct.unpack_from("<ii", body)
                selected[window] = index
            elif command == CMD_TAB_CLOSED:
                tabs.pop(struct.unpack_from("<i", body)[0], None)
            elif command == CMD_WINDOW_CLOSED:
                closed_windows.add(struct.unpack_from("<i", body)[0])
            elif command == CMD_PRUNED_BACK:
                tab_id, index = struct.unpack_from("<ii", body)
                _prune(tab(tab_id), index)
            elif command == CMD_PRUNED_FRONT:
                tab_id, count = struct.unpack_from("<ii", body)
                _prune(tab(tab_id), 0, count)
            elif command == CMD_PRUNED:
                tab_id, index, count = struct.unpack_from("<iii", body)
                _prune(tab(tab_id), index, count)
        except (struct.error, ValueError):
            continue  # comando que nao entendemos: os outros continuam valendo
    found = []
    for tab_id, info in tabs.items():
        page = info["navs"].get(info["nav"])
        if info["window"] is None or info["window"] in closed_windows or not page:
            continue
        found.append(Tab((profile, tab_id), info["window"], info["index"],
                         selected.get(info["window"]) == info["index"], *page))
    found.sort(key=lambda t: (t.window, t.index))
    return found


def session_tabs(profile):
    """Abas do arquivo de sessao mais recente do perfil (relido so quando muda)."""
    files = []
    for path in glob.glob(str(pathlib.Path(profile) / "Sessions" / "Session_*")):
        try:
            files.append((os.stat(path).st_mtime_ns, path))
        except OSError:
            continue
    if not files:
        return []
    mtime, path = max(files)
    try:
        size = os.stat(path).st_size
        stamp = (path, mtime, size)
        with _SESSIONS_LOCK:
            cached = _SESSIONS.get(profile)
            if cached and cached[0] == stamp:
                return cached[1]
        with open(path, "rb") as f:
            data = f.read(SESSION_MAX_BYTES)
    except OSError:
        return []
    tabs = parse_session(data, str(profile))
    with _SESSIONS_LOCK:
        _SESSIONS[profile] = (stamp, tabs)
    return tabs


def open_tabs():
    """Todas as abas abertas em todos os perfis de navegadores Chromium."""
    tabs = []
    for profile in chromium_profiles():
        tabs += session_tabs(profile)
    return tabs


# ------------------------------------------------ janelas, pelo KWin
_WINDOWS = {"stamp": -1e9, "list": None}
_WINDOWS_LOCK = threading.Lock()
WINDOWS_TTL = 3


def browser_windows():
    """[(titulo da aba ativa, minimizada)] das janelas de navegador.

    O KWin conhece o titulo de cada janela (o do Chrome e o da aba ativa) e
    sabe se ela esta minimizada, coisa que o proprio Chrome nao fica sabendo
    no Wayland. None quando o KWin nao responde: ai nao da para saber o que
    esta na tela, e a navegacao sem midia fica de fora da presenca.
    """
    with _WINDOWS_LOCK:
        if time.monotonic() - _WINDOWS["stamp"] < WINDOWS_TTL:
            return _WINDOWS["list"]
        found, uuids = None, []
        # "window" lista todas as janelas; numa sessao traduzida a palavra
        # muda, entao o nome do navegador (que esta no titulo) serve de reserva
        for term in ("window", "Chrome", "Chromium", "Brave", "Vivaldi", "Edge", "Opera"):
            out = busctl("--json=short", "call", "org.kde.KWin", "/WindowsRunner",
                         "org.kde.krunner1", "Match", "s", term)
            if out is None:
                break  # sem KWin
            found = found or []
            try:
                matches = unwrap(json.loads(out)["data"][0])
            except (ValueError, KeyError, IndexError, TypeError):
                matches = []
            for match in matches:
                caption = match[1] if len(match) > 1 else ""
                if caption.endswith(BROWSER_SUFFIXES):
                    uuids.append((match[0].split("_", 1)[-1], caption))
            if term == "window" and matches:
                break
        windows = None if found is None else []
        for uuid, caption in dict(uuids).items():
            out = busctl("--json=short", "call", "org.kde.KWin", "/KWin",
                         "org.kde.KWin", "getWindowInfo", "s", uuid)
            try:
                info = unwrap(json.loads(out)["data"][0]) if out else {}
            except (ValueError, KeyError, IndexError, TypeError):
                info = {}
            caption = info.get("caption") or caption
            for suffix in BROWSER_SUFFIXES:
                if caption.endswith(suffix):
                    windows.append((caption[:-len(suffix)], bool(info.get("minimized"))))
                    break
        _WINDOWS.update(stamp=time.monotonic(), list=windows)
        return windows


def caption_service(title):
    """"ytm", "youtube" ou None, pelo titulo da aba ativa de uma janela."""
    if title == "YouTube Music" or title.endswith((" | YouTube Music", " - YouTube Music")):
        return "ytm"
    if title == "YouTube" or title.endswith(" - YouTube"):
        return "youtube"
    return None


def service_windows(service, windows):
    """Titulos das janelas visiveis cuja aba ativa e do servico."""
    return [title for title, minimized in windows or []
            if not minimized and caption_service(title) == service]


def visible_tab(tab, windows):
    """A aba e a ativa de uma janela que nao esta minimizada.

    A comparacao e pelo servico, nao pelo titulo: o Chrome so regrava o
    arquivo de sessao quando a URL muda, entao o titulo gravado pode ser o da
    pagina anterior; o da janela, que vem do KWin, esta sempre em dia.
    """
    return tab.selected and bool(service_windows(media_service(tab.url), windows))


# ------------------------------------------------ o que cada player esta tocando
# O Chrome publica um player MPRIS por vez, com titulo, canal, duracao e
# posicao, mas sem dizer de qual aba. A aba sai assim, nesta ordem:
#   1. a aba cujo titulo gravado e o titulo que esta tocando;
#   2. a aba ja ligada ao mpris:trackid (cada aba tem o seu, e ele nao muda
#      entre um video e outro nem durante os anuncios);
#   3. a unica aba de video ativa numa janela visivel (aba que acabou de abrir).
# Depois vem a conferencia: o titulo tocado tem de ser o do video daquela URL
# (oEmbed do YouTube). Nao sendo, e anuncio (o YouTube publica o anunciante
# como se fosse o video, as vezes um clipe de minutos) ou uma navegacao que o
# Chrome ainda nao gravou (ele grava uns 2,5 s depois). Por isso o anuncio so
# vale depois de AD_CONFIRM segundos; antes disso a presenca segura o que ja
# mostrava.
AD_CONFIRM = 5
LIVE_LENGTH = 1 << 53  # o Chrome informa INT64_MAX como duracao de uma live
_TRACK_TAB = {}  # trackid -> (aba, URL, titulo) da ultima vez que bateu
_MISMATCH = {}
_MEDIA = {"stamp": -1e9, "found": []}
_MEDIA_LOCK = threading.Lock()


def find_tab(title, tabs):
    """Aba do YouTube cujo titulo e o do que esta tocando; a selecionada ganha."""
    best = None
    for tab in tabs:
        if media_service(tab.url) and page_title(tab.title) == title:
            if best is None or (tab.selected and not best.selected):
                best = tab
    return best


def _norm(text, keep_spaces=True):
    text = (text or "").casefold().replace("’", "'").replace("‘", "'")
    if not keep_spaces:
        return re.sub(r"[^\w]", "", text)
    return " ".join(text.split())


def title_fits(title, artist, video, service, strict=False):
    """O que o player toca e o video dessa URL? None quando nao da para saber.

    No YouTube o titulo e identico. No YouTube Music o player usa o nome da
    faixa ("Together Forever (2022 Remaster)") e o video tem o do clipe
    ("Rick Astley - Together Forever (Official Video)"): vale o nome sem os
    parenteses dentro do titulo, ou o artista no nome do canal. Com strict o
    artista nao basta, porque a proxima faixa pode ser do mesmo artista.
    """
    info = youtube_oembed(video) if video else {}
    real = _norm(info.get("title"))
    if not real:
        return None
    mine = _norm(title)
    core = _norm(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or ""))
    if mine == real or (len(core) >= 3 and core in real):
        return True
    if service == "ytm" and not strict:
        singer, channel = _norm(artist, False), _norm(info.get("author_name"), False)
        if len(singer) >= 3 and channel and (singer in channel or channel in singer):
            return True
    return False


def _mismatch_age(player, title):
    seen = _MISMATCH.get(player)
    if not seen or seen[0] != title:
        seen = _MISMATCH[player] = (title, time.monotonic())
    return time.monotonic() - seen[1]


def _newest(tabs):
    """Abas do perfil cujo arquivo de sessao foi gravado por ultimo."""
    if len(tabs) < 2:
        return tabs
    stamps = {}
    with _SESSIONS_LOCK:
        for profile, (stamp, _tabs) in _SESSIONS.items():
            stamps[str(profile)] = stamp[1]
    best = max(stamps.get(t.key[0], 0) for t in tabs)
    return [t for t in tabs if stamps.get(t.key[0], 0) == best]


def resolve_player(name, process, props, tabs, windows=None):
    """Servico, pagina e situacao (anuncio, aguardando) de um player MPRIS."""
    meta = props.get("Metadata")
    meta = meta if isinstance(meta, dict) else {}
    title = meta.get("xesam:title") or ""
    artist = first(meta.get("xesam:artist")) or ""
    track = meta.get("mpris:trackid") or ""
    media = {"player": name, "process": process, "meta": meta,
             "status": props.get("PlaybackStatus"), "url": "", "service": None,
             "source": "-", "tab": None, "video": None, "ad": False, "pending": False}

    url = meta.get("xesam:url") or ""
    if url:  # o Firefox diz a URL
        media.update(url=url, service=media_service(url), source="xesam:url",
                     video=youtube_video_id(url))
        return media
    if process not in CHROMIUM_PROCESSES or not title:
        return media

    owner, how = find_tab(title, tabs), "aba"
    if owner is None and track in _TRACK_TAB:
        key = _TRACK_TAB[track][0]
        owner, how = next((t for t in tabs if t.key == key), None), "aba do player"
    if owner is None and track not in _TRACK_TAB and media["status"] == "Playing":
        # aba nova: o trackid ainda nao foi visto. Vale so uma candidata,
        # ativa numa janela visivel e sem player conhecido, para nao culpar o
        # YouTube pelo som de outro site.
        windows = browser_windows() if windows is None else windows
        known = {seen[0] for seen in _TRACK_TAB.values()}
        candidates = _newest([t for t in tabs if is_player_page(t.url)
                              and t.key not in known and visible_tab(t, windows)])
        owner, how = (candidates[0] if len(candidates) == 1 else None), "aba visivel"
    if owner is None or not media_service(owner.url):
        url = history_url(title)
        if url:
            media.update(url=url, service=media_service(url), source="historico",
                         video=youtube_video_id(url))
        return media

    service = media_service(owner.url)
    video = youtube_video_id(owner.url) or live_video_id(owner.url)
    # mesma aba, mesma URL e outro titulo: anuncio, ou o Chrome ainda nao
    # gravou a URL da faixa nova. A URL velha nao serve para o titulo novo.
    seen = _TRACK_TAB.get(track)
    stale = bool(seen) and seen[:2] == (owner.key, owner.url) and seen[2] != title
    fits = title_fits(title, artist, video, service, strict=stale)
    if fits is None:  # sem oEmbed (sem rede, video privado): sinais locais
        fits = not stale if how == "aba do player" else how == "aba"
    media.update(url=owner.url, service=service, tab=owner, video=video)
    if fits:
        if track:
            if len(_TRACK_TAB) > 200:
                _TRACK_TAB.clear()
            _TRACK_TAB[track] = (owner.key, owner.url, title)
        _MISMATCH.pop(name, None)
        media["source"] = how
    elif _mismatch_age(name, title) < AD_CONFIRM:
        media.update(pending=True, source="aguardando a aba")
    else:
        media.update(ad=True, source="anuncio")
    return media


def media_now():
    """Todos os players MPRIS, cada um com a pagina que ele esta tocando."""
    with _MEDIA_LOCK:
        if time.monotonic() - _MEDIA["stamp"] < MPRIS_TTL:
            return _MEDIA["found"]
        players = mpris_players()
        chromium = any(process in CHROMIUM_PROCESSES for _, process in players)
        tabs = open_tabs() if chromium else []
        found = [resolve_player(name, process, mpris_props(name), tabs)
                 for name, process in players]
        _MEDIA.update(stamp=time.monotonic(), found=found)
        return found


def player_url(process, meta):
    """Compatibilidade: endereco da pagina que esta tocando."""
    media = resolve_player("", process, {"Metadata": meta}, open_tabs(), windows=[])
    return media["url"]


def media_player(service):
    """O player que toca o servico pedido, ou None.

    Cada aba com midia pode virar um player e o bus name do Firefox muda a
    cada boot, entao a escolha e pela pagina, nao pelo nome. Entre dois
    players do mesmo servico, o que esta tocando ganha do que esta pausado.
    """
    best = None
    for media in media_now():
        if media["service"] != service:
            continue
        if best is None or (media["status"] == "Playing" and best["status"] != "Playing"):
            best = media
    return best


# ------------------------------------------------ navegando, sem nada tocando
BROWSE_IDLE = 900  # mesma pagina parada por mais tempo que isso: saiu de perto
_BROWSE = {}


def browsing(service):
    """(aba, titulo atual) do servico ativa numa janela visivel, fora de uma
    pagina de player, ou None."""
    windows = browser_windows()
    if not windows:
        return None
    tabs = _newest([t for t in open_tabs() if media_service(t.url) == service
                    and not is_player_page(t.url) and visible_tab(t, windows)])
    if not tabs:
        return None
    # o titulo gravado pode estar velho; o da janela, quando so ha uma, nao
    captions = service_windows(service, windows)
    return tabs[0], (captions[0] if len(captions) == 1 else tabs[0].title)


def browse_since(service, tab):
    """Desde quando o servico esta sendo navegado, ou None se parou de vez."""
    now = time.monotonic()
    if tab is None:
        _BROWSE.pop(service, None)
        return None
    url, page_since, since = _BROWSE.get(service, (None, now, now))
    if tab.url != url:
        page_since = now
    _BROWSE[service] = (tab.url, page_since, since)
    if now - page_since > BROWSE_IDLE:
        return None
    return time.time() - (now - since)


YT_PAGES = [("/feed/subscriptions", "Nas inscrições"), ("/feed/history", "No histórico"),
            ("/feed/trending", "Em alta"), ("/feed/explore", "Explorando"),
            ("/feed/library", "Na biblioteca"), ("/feed/you", "Na biblioteca"),
            ("/feed/playlists", "Nas playlists"), ("/feed/channels", "Nos canais"),
            ("/feed/downloads", "Nos downloads"), ("/gaming", "Em jogos")]
YTM_PAGES = [("/explore", "Explorando"), ("/library", "Na biblioteca"),
             ("/charts", "Nas paradas"), ("/new_releases", "Nos lançamentos"),
             ("/moods_and_genres", "Em gêneros"), ("/playlist", "Numa playlist"),
             ("/channel", "Na página de um artista"), ("/browse", "Num álbum")]


def browse_text(service, tab, title=None, show_search=False):
    """(details, link) de uma pagina de navegacao do YouTube ou do YT Music."""
    path = url_path(tab.url)
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(tab.url).query)
    except ValueError:
        query = {}
    name = page_title(title or tab.title)
    if path == "/":
        return "Na página inicial", None
    if path in ("/results", "/search"):
        term = (query.get("search_query") or query.get("q") or [""])[0]
        return (f"Pesquisando {term}" if show_search and term else "Pesquisando"), None
    if service == "youtube":
        named = name and name != "YouTube"  # titulo de outra pagina ainda
        if path.startswith(("/@", "/channel/", "/c/", "/user/")):
            return (f"No canal {name}" if named else "Num canal"), tab.url
        if path == "/playlist":
            return (f"Na playlist {name}" if named else "Numa playlist"), tab.url
        for prefix, text in YT_PAGES:
            if path.startswith(prefix):
                return text, None
    else:
        for prefix, text in YTM_PAGES:
            if path.startswith(prefix):
                return text, None
    return "Navegando", None


# ------------------------------------------------ dados publicos do video
_THUMBS = {}
_OEMBED = {}
_LIVE_IDS = {}
CANONICAL = re.compile(rb'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"')


def _trim(cache, limit=500):
    if len(cache) > limit:
        cache.clear()


def video_thumbnail(video):
    """Capa 16:9 sem tarja preta (maxresdefault) quando existe, senao a
    hqdefault, que todo video tem."""
    if video in _THUMBS:
        return _THUMBS[video]
    best = f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"
    request = urllib.request.Request(f"https://i.ytimg.com/vi/{video}/maxresdefault.jpg",
                                     method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            if r.status == 200:
                best = request.full_url
    except urllib.error.HTTPError:
        pass  # 404: video sem capa em alta
    except OSError:
        return best  # sem rede agora: tenta de novo depois
    _trim(_THUMBS)
    _THUMBS[video] = best
    return best


def youtube_oembed(video):
    """Titulo, canal e link do canal, pelo oEmbed publico do YouTube."""
    if video in _OEMBED:
        return _OEMBED[video]
    try:
        data = fetch_json("https://www.youtube.com/oembed?format=json&url="
                          + urllib.parse.quote(f"https://www.youtube.com/watch?v={video}"))
    except urllib.error.HTTPError:
        data = {}  # privado ou sem incorporacao: nao adianta repetir
    except (OSError, ValueError):
        return {}
    _trim(_OEMBED)
    _OEMBED[video] = data if isinstance(data, dict) else {}
    return _OEMBED[video]


def live_video_id(url):
    """Id do video de um link /@canal/live, pelo link canonico da pagina."""
    if not is_live_page(url):
        return None
    cached = _LIVE_IDS.get(url)
    if cached and time.monotonic() - cached[1] < 600:
        return cached[0]
    video, page, total = None, b"", 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as r:
            while total < 3_000_000:  # o link fica perto de 800 KB pagina adentro
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                page = page[-200:] + chunk
                found = CANONICAL.search(page)
                if found:
                    video = found.group(1).decode()
                    break
    except OSError:
        return None
    _trim(_LIVE_IDS)
    _LIVE_IDS[url] = (video, time.monotonic())
    return video


# ------------------------------------------------ YouTube e YouTube Music
_LAST_STATE = {}


def service_now(service):
    """("midia", player) ou ("navegando", (aba, desde)) do servico, ou None."""
    media = media_player(service)
    if media and not media_idle(media["player"]):
        browse_since(service, None)
        return "midia", media
    page = browsing(service)
    since = browse_since(service, page[0] if page else None)
    if page and since:
        return "navegando", (*page, since)
    return None


def yt_running():
    now = service_now("youtube")
    if not now:
        return None
    return now[1]["player"] if now[0] == "midia" else "navegando no YouTube"


def ytm_running():
    now = service_now("ytm")
    if not now:
        return None
    return now[1]["player"] if now[0] == "midia" else "navegando no YouTube Music"


def _service_state(service, media_state):
    now = service_now(service)
    if not now:
        return None
    kind, item = now
    if kind == "navegando":
        state = browse_state(service, *item)
    elif item["pending"]:  # troca de video que a aba ainda nao gravou
        return _LAST_STATE.get(service)
    else:
        state = media_state(item)
    _LAST_STATE[service] = state
    return state


def yt_state():
    """Video, live, Shorts, anuncio, pausa ou a pagina aberta no YouTube."""
    return _service_state("youtube", video_state)


def ytm_state():
    """Musica, anuncio, pausa ou a pagina aberta no YouTube Music."""
    return _service_state("ytm", song_state)


def browse_state(service, tab, title, since):
    game = GAMES.get("youtube" if service == "youtube" else "youtube-music") or {}
    details, link = browse_text(service, tab, title, bool(game.get("show_search")))
    extra = {"details": details, "timestamps": {"start": int(since)},
             "status_display_type": 0}  # "Assistindo YouTube", nao "Assistindo Pesquisando"
    if link:
        extra["details_url"] = link
    return None, extra


def paused_text(who, position):
    where = f" em {duration_label(position)}" if position else ""
    return f"{who} · pausado{where}" if who else f"Pausado{where}"


def video_state(media):
    meta, player, url = media["meta"], media["player"], media["url"]
    channel = first(meta.get("xesam:artist")) or ""
    title = meta.get("xesam:title") or ""
    video = media.get("video") or youtube_video_id(url) or live_video_id(url)
    shorts = url_path(url).startswith("/shorts/")
    link = None
    if video:
        link = (f"https://www.youtube.com/shorts/{video}" if shorts
                else f"https://www.youtube.com/watch?v={video}")
    info = youtube_oembed(video) if video else {}
    channel_url = info.get("author_url") or ""
    channel_url = channel_url if channel_url.startswith("https://") else None

    if media["ad"]:
        # titulo e canal do player sao do anunciante; o video e o da aba, com
        # o titulo do oEmbed (o gravado na aba pode ser o do video anterior)
        name = info.get("title") or (page_title(media["tab"].title) if media["tab"] else "")
        name = name if name and name not in ("YouTube", "YouTube Music") else "YouTube"
        extra = {"details": name, "timestamps": None}
        if video:
            extra.update(details_url=link, large_image=video_thumbnail(video),
                         large_text=name, small_image=YT_ICON, small_text="YouTube")
        return "Anúncio", extra

    extra = {"details": title or info.get("title") or "YouTube"}
    if video:
        extra.update(details_url=link, large_image=video_thumbnail(video),
                     large_text=title or extra["details"], large_url=link,
                     small_image=YT_ICON, small_text="YouTube",
                     buttons=[{"label": "Assistir no YouTube", "url": link}])
    if channel_url:
        extra["state_url"] = channel_url
        extra.setdefault("buttons", []).append({"label": "Ver o canal", "url": channel_url})

    length = meta.get("mpris:length") or 0
    live = length >= LIVE_LENGTH or is_live_page(url)
    if media["status"] != "Playing":
        position = mpris_position(player, tries=1)
        anchored_position(title, position, playing=False)
        extra["timestamps"] = None  # pausado: o relogio para
        return paused_text(channel, None if live else position), extra
    if live:
        # a posicao de uma live conta desde que ela comecou a tocar aqui
        position = mpris_position(player)
        if position:
            extra["timestamps"] = {"start": int(time.time() - position / 1_000_000)}
        return (f"🔴 Ao vivo · {channel}" if channel else "🔴 Ao vivo"), extra

    length = track_length(title, meta, video)
    position = anchored_position(title, mpris_position(player))
    extra["timestamps"] = media_timestamps(title, position, length, player)
    text = channel or "YouTube"
    if shorts:
        text = f"Shorts · {channel}" if channel else "Shorts"
    if not extra["timestamps"] and length:  # sem barra, ao menos a duracao
        text = f"{text} · {duration_label(length)}"
    return text, extra


_ART_CACHE = {}


def ytm_art(meta, url=None):
    """Capa da musica. O navegador nao publica uma capa utilizavel, entao:
    a capa do proprio video quando a pagina traz o id, senao a do album pela
    busca do iTunes."""
    video = youtube_video_id(url or meta.get("xesam:url") or "")
    if video:
        return video_thumbnail(video)

    artist = first(meta.get("xesam:artist"))
    term = " ".join(filter(None, [artist, meta.get("xesam:title")])).strip()
    if not term:
        return None
    if term in _ART_CACHE:
        return _ART_CACHE[term]

    art = None
    try:
        query = urllib.parse.urlencode({"term": term, "media": "music", "limit": 1})
        with urllib.request.urlopen(
                f"https://itunes.apple.com/search?{query}", timeout=5) as r:
            results = json.loads(r.read().decode("utf-8", "replace")).get("results")
        if results:
            art = results[0].get("artworkUrl100", "").replace("100x100", "600x600")
    except (OSError, ValueError):
        pass
    _trim(_ART_CACHE)
    _ART_CACHE[term] = art
    return art


def song_state(media):
    meta, player, url = media["meta"], media["player"], media["url"]
    artist = first(meta.get("xesam:artist")) or ""
    title = meta.get("xesam:title") or ""
    album = meta.get("xesam:album") or ""
    video = media.get("video") or youtube_video_id(url)
    link = f"https://music.youtube.com/watch?v={video}" if video else None

    if media["ad"]:
        return None, {"details": "Anúncio", "timestamps": None, "status_display_type": 0}

    extra = {"details": title or "YouTube Music"}
    art = video_thumbnail(video) if video else ytm_art(meta, url)
    if art:  # capa da musica no lugar do icone do app
        extra.update(large_image=art, small_image=YTM_ICON, small_text="YouTube Music",
                     large_text=album or (f"{title} · {artist}" if artist else title))
    if link:
        extra.update(details_url=link, large_url=link,
                     buttons=[{"label": "Ouvir no YouTube Music", "url": link}])
    if artist:  # o MPRIS nao traz o canal do artista, entao vai pela busca
        artist_url = "https://music.youtube.com/search?q=" + urllib.parse.quote(artist)
        extra["state_url"] = artist_url
        extra.setdefault("buttons", []).append(
            {"label": f"Artista: {artist}"[:32], "url": artist_url})

    if media["status"] != "Playing":
        position = mpris_position(player, tries=1)
        anchored_position(title, position, playing=False)
        extra["timestamps"] = None
        return paused_text(artist, position), extra

    length = track_length(title, meta)
    position = anchored_position(title, mpris_position(player))
    extra["timestamps"] = media_timestamps(title, position, length, player)
    text = artist or "YouTube Music"
    if not extra["timestamps"] and length:
        text = f"{text} · {duration_label(length)}"
    return text, extra


def stremio_player():
    """Bus name do Stremio (nativo ou Flatpak), se ele publicar um."""
    for name, _process in mpris_players():
        if "stremio" in name.lower():
            return name
    return None


def stremio_running():
    hit = detect("stremio", {"match": [{"cmdline": "libexec/stremio/stremio"},
                                      {"exe": ["stremio", "stremio-shell"]}]})
    player = stremio_player()
    if not (hit or player):
        return None
    if player and media_idle(player):
        return None
    if not player and media_idle(MPRIS_PREFIX + "Stremio"):
        return None  # aberto, mas sem tocar nada ha tempo demais
    return hit or player


def stremio_state():
    """Titulo em reproducao e quanto falta para acabar, via MPRIS."""
    player = stremio_player()
    status = mpris("PlaybackStatus", player) if player else None
    if status is None:  # o Stremio fechou, ou nao publica player
        return None
    meta = mpris_meta(player)

    title = meta.get("xesam:title")
    if not title:
        return ("Navegando" if status == "Stopped" else "Assistindo"), None
    series = first(meta.get("xesam:artist"))  # serie, quando for episodio
    label = f"{series} — {title}" if series and series != title else title
    if status != "Playing":
        return f"{label} (pausado)", None

    # o Stremio nao publica mpris:length nem Position, entao o Discord so pode
    # contar o tempo desde o inicio deste titulo (o timer reinicia a cada troca)
    position, length = mpris("Position", player), meta.get("mpris:length")
    if position and length and position <= length:
        now = time.time()
        return label, {"timestamps": {
            "start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000),
        }}
    return label, None


# ------------------------------------------------------------- Roblox
SOBER_LOG = (pathlib.Path.home() /
             ".var/app/org.vinegarhq.Sober/data/sober/sober_logs/latest.log")

_ROBLOX_CACHE = {}


def roblox_place():
    """placeId da experiencia aberta agora, lido do log do Sober."""
    try:
        with SOBER_LOG.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    # onGameLoaded marca a entrada; onGameLeaveBegin, a saida do mesmo place
    events = re.findall(r"onGame(Loaded|LeaveBegin)\(\).*?placeId:(\d+)", tail)
    if not events:
        return None
    kind, place = events[-1]
    return place if kind == "Loaded" else None


def roblox_game(place):
    """Nome, criador, jogadores e icone da experiencia (API publica da Roblox)."""
    if place in _ROBLOX_CACHE:
        info = dict(_ROBLOX_CACHE[place])
    else:
        try:
            universe = fetch_json(
                f"https://apis.roblox.com/universes/v1/places/{place}/universe")
            universe = universe["universeId"]
            data = fetch_json(
                f"https://games.roblox.com/v1/games?universeIds={universe}")["data"][0]
            icon = fetch_json(
                "https://thumbnails.roblox.com/v1/games/icons"
                f"?universeIds={universe}&size=512x512&format=Png&isCircular=false")
            info = {"universe": universe,
                    "name": data["name"],
                    "creator": data["creator"]["name"],
                    "icon": (icon["data"][0].get("imageUrl") or None)}
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None
        _ROBLOX_CACHE[place] = info
        info = dict(info)
    return info


def roblox_players(universe):
    """Quantos estao na experiencia agora (nao cabe cache: muda o tempo todo)."""
    try:
        data = fetch_json(
            f"https://games.roblox.com/v1/games?universeIds={universe}")["data"][0]
        return data.get("playing")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def roblox_state():
    """Experiencia aberta no Roblox, com criador, icone e gente online."""
    place = roblox_place()
    if not place:
        return "No menu", None

    info = roblox_game(place)
    if not info:
        return "Em uma experiencia", None

    extra = {"large_text": info["name"][:128]}
    if info.get("icon"):
        extra["large_image"] = info["icon"]
    extra["buttons"] = [{"label": "Ver no Roblox",
                         "url": f"https://www.roblox.com/games/{place}"}]

    # o numero da Roblox e quanta gente esta na experiencia inteira, nao no
    # servidor: como nao existe um "maximo", vai no texto e nao como party
    players = roblox_players(info["universe"])
    text = f"por {info['creator']}"
    if players:
        text = f"{text} · {players:,} jogando".replace(",", ".")
    return text, extra


def roblox_title():
    place = roblox_place()
    info = roblox_game(place) if place else None
    return info["name"][:128] if info else "Roblox"


# ------------------------------------------------------------- Claude Code
def claude_running():
    """Claude Code aberto: os processos cujo executavel se chama claude.

    A comparacao e pelo nome do executavel, nao pela linha de comando: os
    argumentos carregam o nome da sessao (`-n ...`), e nada do que esta sendo
    feito deve chegar perto da presenca.
    """
    return [p for p in processes() if p.comm == "claude"]


# ---------------------------------------------------------------- GTA SA
# O jogo guarda tudo em enderecos fixos (imagem carregada em 0x400000, sem
# ASLR), entao da para ler o estado direto de /proc/<pid>/mem: onde o CJ esta,
# vida, dinheiro. Os nomes dos lugares saem do proprio jogo (info.zon diz que
# zona cobre cada coordenada, e o GXT traduz o codigo dela), o que faz a
# presenca acompanhar o idioma instalado sem tabela embutida aqui.
GTA_NAME = "Grand Theft Auto San Andreas"
GTA_EXES = ("gta-sa.exe", "gta_sa.exe")
GTA_PLAYER_PED = 0xB6F5F0      # ponteiro do CPlayerPed
GTA_MONEY = 0xB7CE54           # dinheiro exibido no HUD
GTA_SCRIPT_SPACE = 0xA49960    # inicio das variaveis globais do script
GTA_ONMISSION = 0x68           # $ONMISSION e a global $26 (26 * 4 = 0x68)
GTA_ACTIVE_SCRIPTS = 0xA8B42C  # lista encadeada das threads de script ativas

_gta_cache = {"zones": None, "text": None, "dir": None}


def gta_dir():
    if _gta_cache["dir"] is None:
        _gta_cache["dir"] = steam_game_dir(GTA_NAME) or False
    return _gta_cache["dir"] or None


def gta_pid():
    """PID do executavel do GTA SA rodando sob Proton.

    Os wrappers do Proton carregam o caminho do jogo nos proprios argumentos,
    e ler a memoria do wrapper devolve um jogador inexistente. So conta quem
    se chama gta-sa.exe (argv[0] ou comm) e tem o executavel mapeado em
    0x400000, que e a ImageBase dele; a segunda prova so e lida para esses
    poucos candidatos, nao para o sistema inteiro.
    """
    for proc in processes():
        if not any(name_matches(proc, exe) for exe in GTA_EXES):
            continue
        try:
            maps = pathlib.Path(f"/proc/{proc.pid}/maps").read_text(errors="replace")
        except OSError:
            continue  # processo morreu, ou nao e nosso
        for line in maps.splitlines():
            low = line.lower()
            if line.startswith("00400000-") and any(e in low for e in GTA_EXES):
                return proc.pid
    return None


def gta_read(pid, addr, size):
    try:
        with open(f"/proc/{pid}/mem", "rb", 0) as mem:
            mem.seek(addr)
            data = mem.read(size)
        return data if data and len(data) == size else None
    except (OSError, ValueError, OverflowError):
        return None  # jogo fechou, ou o kernel nao deixa ler


def gta_u32(pid, addr):
    raw = gta_read(pid, addr, 4)
    return struct.unpack("<I", raw)[0] if raw else None


def gta_f32(pid, addr):
    raw = gta_read(pid, addr, 4)
    return struct.unpack("<f", raw)[0] if raw else None


def gta_zones():
    """Zonas do info.zon: caixas com o codigo de texto de cada bairro."""
    if _gta_cache["zones"] is not None:
        return _gta_cache["zones"]
    zones, path = [], gta_dir()
    if path:
        try:
            for line in (path / "data/info.zon").read_text(errors="replace").splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 10:
                    continue
                try:
                    box = [float(v) for v in parts[2:8]]
                except ValueError:
                    continue
                zones.append((box, parts[9]))
        except OSError:
            pass
    _gta_cache["zones"] = zones
    return zones


def gta_texts():
    """Textos do jogo (GXT). A chave e o JamCRC do nome, em maiusculas."""
    if _gta_cache["text"] is not None:
        return _gta_cache["text"]
    texts, path = {}, gta_dir()
    if path:
        for gxt in ("american.gxt", "english.gxt"):
            try:
                texts = parse_gxt(path / "text" / gxt)
                break
            except (OSError, ValueError, struct.error, IndexError):
                continue
    _gta_cache["text"] = texts
    return texts


def parse_gxt(path):
    """Tabela de textos do GTA SA: TABL, depois TKEY/TDAT por tabela."""
    data = pathlib.Path(path).read_bytes()
    if data[4:8] != b"TABL":
        raise ValueError("nao parece um GXT de San Andreas")
    tabl_size = struct.unpack("<I", data[8:12])[0]
    tables = [(data[i:i + 8].split(b"\0")[0].decode(errors="replace"),
               struct.unpack("<I", data[i + 8:i + 12])[0])
              for i in range(12, 12 + tabl_size, 12)]

    texts = {}
    for name, offset in tables:
        start = offset if name == "MAIN" else offset + 8
        if data[start:start + 4] != b"TKEY":
            continue
        tkey_size = struct.unpack("<I", data[start + 4:start + 8])[0]
        keys = [struct.unpack("<II", data[i:i + 8])
                for i in range(start + 8, start + 8 + tkey_size, 8)]
        dat = start + 8 + tkey_size
        if data[dat:dat + 4] != b"TDAT":
            continue
        base = dat + 8
        for text_offset, crc in keys:
            end = data.find(b"\0", base + text_offset)
            if end > 0:
                texts.setdefault(crc, data[base + text_offset:end]
                                 .decode("cp1252", "replace"))
    return texts


def gta_text(name):
    """Texto do jogo pelo nome da chave (o hash e um CRC32 invertido)."""
    crc = (~zlib.crc32(name.upper().encode())) & 0xFFFFFFFF
    return gta_texts().get(crc)


def gta_place(x, y, z):
    """Nome do bairro onde o CJ esta, no idioma do jogo.

    As caixas do info.zon se sobrepoem (a cidade inteira cobre cada bairro
    dentro dela), entao vale a menor que contem o ponto: e ela que tem o nome
    especifico ("Ganton", e nao "Los Santos").
    """
    melhor, menor = None, None
    for (x1, y1, z1, x2, y2, z2), code in gta_zones():
        if x1 <= x <= x2 and y1 <= y <= y2 and z1 <= z <= z2:
            area = (x2 - x1) * (y2 - y1)
            if menor is None or area < menor:
                melhor, menor = code, area
    return (gta_text(melhor) or melhor) if melhor else None


def gta_vehicles():
    """Modelo de veiculo -> chave de texto, lida do vehicles.ide do jogo."""
    if _gta_cache.get("cars") is not None:
        return _gta_cache["cars"]
    cars, path = {}, gta_dir()
    if path:
        try:
            for line in (path / "data/vehicles.ide").read_text(errors="replace").splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) > 5 and parts[0].isdigit():
                    cars[int(parts[0])] = parts[5]
        except OSError:
            pass
    _gta_cache["cars"] = cars
    return cars


def gta_vehicle_name(pid, ped):
    """Nome do veiculo em que o CJ esta, ou None se ele estiver a pe."""
    vehicle = gta_u32(pid, ped + 0x58C)
    if not vehicle:
        return None
    raw = gta_read(pid, vehicle + 0x22, 2)
    if not raw:
        return None
    model = struct.unpack("<H", raw)[0]
    key = gta_vehicles().get(model)
    return (gta_text(key) or key) if key else None


def gta_wanted(pid, ped):
    """Nivel de procurado (0 a 6), ou None quando a leitura nao convence."""
    wanted = gta_u32(pid, ped + 0x480)
    if not wanted:
        return None
    raw = gta_read(pid, wanted + 0x1C, 4)
    if not raw:
        return None
    level = struct.unpack("<i", raw)[0]
    return level if 0 <= level <= 6 else None


_gta_idle_scripts = set()


def gta_scripts(pid):
    """Nomes das threads de script rodando agora."""
    nomes, node = [], gta_u32(pid, GTA_ACTIVE_SCRIPTS)
    while node and len(nomes) < 120:
        raw = gta_read(pid, node + 0x08, 8)
        if not raw:
            break
        nome = raw.split(b"\0")[0].decode("ascii", "replace").strip()
        if nome and nome != "noname":
            nomes.append(nome)
        node = gta_u32(pid, node)
    return nomes


def gta_mission_name(pid, on_mission):
    """Nome do script que entrou junto com a missao.

    O jogo nao guarda "qual missao" em lugar nenhum obvio, mas carrega uma
    thread nova quando ela comeca. Guardando quais threads rodam fora de
    missao, a que sobra na comparacao e a da missao.
    """
    global _gta_idle_scripts
    atuais = set(gta_scripts(pid))
    if not atuais:
        return None
    if not on_mission:
        _gta_idle_scripts = atuais
        return None
    novos = atuais - _gta_idle_scripts
    return sorted(novos)[0] if novos else None


def gta_on_mission(pid):
    """True quando ha uma missao em andamento ($ONMISSION do script)."""
    raw = gta_read(pid, GTA_SCRIPT_SPACE + GTA_ONMISSION, 4)
    if not raw:
        return False
    valor = struct.unpack("<i", raw)[0]
    return valor == 1  # so o 1 conta: lixo em outro offset nao vira "em missao"


def gta_running():
    return gta_pid()


def gta_state():
    """Onde o CJ esta e como ele esta, lido da memoria do jogo."""
    pid = gta_pid()
    if not pid:
        return None

    ped = gta_u32(pid, GTA_PLAYER_PED)
    if not ped:  # menu principal, ou save ainda carregando
        return "Jogando", None

    matrix = gta_u32(pid, ped + 0x14)
    raw = gta_read(pid, (matrix + 0x30) if matrix else (ped + 0x34), 12)
    if not raw:
        return "Jogando", None
    x, y, z = struct.unpack("<3f", raw)
    if not all(-4000 < v < 4000 for v in (x, y, z)):
        return "Jogando", None  # coordenada sem sentido: nao publica palpite

    partes = []
    on_mission = gta_on_mission(pid)
    mission = gta_mission_name(pid, on_mission)
    if on_mission:
        partes.append(f"Em missão: {mission}" if mission else "Em missão")

    place = gta_place(x, y, z)
    if place:
        partes.append(place)

    vehicle = gta_vehicle_name(pid, ped)
    if vehicle:
        partes.append(vehicle)

    wanted = gta_wanted(pid, ped)
    if wanted:
        partes.append("★" * wanted)

    health = gta_f32(pid, ped + 0x540)
    if health is not None and 0 <= health <= 1000:
        partes.append(f"{int(health)} HP")

    money = gta_read(pid, GTA_MONEY, 4)
    if money:
        valor = struct.unpack("<i", money)[0]
        if -1_000_000 < valor < 1_000_000_000:
            partes.append(f"${valor:,}".replace(",", "."))

    return (" · ".join(partes) if partes else "Jogando"), None


# ------------------------------------------------------------- Steam, L4D2
def steam_libraries():
    """Todas as bibliotecas Steam, inclusive em outros discos."""
    roots = [pathlib.Path.home() / ".local/share/Steam",
             pathlib.Path.home() / ".steam/steam",
             pathlib.Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    libs = []
    for root in roots:
        vdf = root / "steamapps/libraryfolders.vdf"
        if not vdf.exists():
            continue
        libs.append(root)
        try:
            text = vdf.read_text(errors="replace")
        except OSError:
            continue
        libs += [pathlib.Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    return list(dict.fromkeys(libs))


def steam_game_dir(name):
    """Pasta de um jogo instalado, procurando em todas as bibliotecas."""
    for lib in steam_libraries():
        path = lib / "steamapps/common" / name
        if path.is_dir():
            return path
    return None


def l4d2_log():
    game = steam_game_dir("Left 4 Dead 2")
    return game / "left4dead2/console.log" if game else None


L4D2_MAPS = {
    "c1": "Dead Center", "c2": "Dark Carnival", "c3": "Swamp Fever",
    "c4": "Hard Rain", "c5": "The Parish", "c6": "The Passing",
    "c7": "The Sacrifice", "c8": "No Mercy", "c9": "Crash Course",
    "c10": "Death Toll", "c11": "Dead Air", "c12": "Blood Harvest",
    "c13": "Cold Stream", "c14": "The Last Stand",
}


def l4d2_state():
    """Mapa atual, lido do console.log (precisa de con_logfile no autoexec.cfg)."""
    log_file = l4d2_log()
    if not log_file:
        return "Jogando", None
    try:
        with log_file.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "Jogando", None

    hits = re.findall(r"^(?:Loading map|Map:|Host_NewGame on map)\s*\"?([\w.]+?)\"?\s*$",
                      tail, re.M)
    if not hits:
        return "No menu", None
    slug = hits[-1].removesuffix(".bsp")
    chapter = re.match(r"c\d+", slug)
    campaign = L4D2_MAPS.get(chapter.group() if chapter else "")
    m = re.search(r"m(\d+)", slug)
    if campaign:
        return (f"{campaign} — capítulo {m.group(1)}" if m else campaign), None
    return slug, None


# ------------------------------------------------------------- Minecraft
# O processo e o java que leva o caminho de uma instancia (Prism, MultiMC,
# PolyMC, inclusive em Flatpak) ou o main do Minecraft. A pasta do jogo e o
# diretorio de trabalho dele, e de la sai tudo: o nome da instancia, o
# servers.dat e o latest.log, onde o proprio jogo escreve "Connecting to
# <host>, <porta>" com o nome que a pessoa digitou (inclusive enderecos SRV,
# que nao batem com o IP conectado).
MC_HINTS = ("PrismLauncher/instances/", "MultiMC/instances/", "PolyMC/instances/",
            "net.minecraft.client.main.Main", "--gameDir", "/.minecraft")
HTTP_PORTS = {80, 443}

_MC_DNS = {}
_MC_PING = {}


def mc_proc():
    """O processo java do Minecraft, ou None."""
    best = None
    for proc in processes():
        if proc.prog not in ("java", "javaw") and not (
                proc.comm == "java" or basename(proc.exe) == "java"):
            continue
        if not any(hint in proc.cmdline for hint in MC_HINTS):
            continue
        if best is None or "instances/" in proc.cmdline:
            best = proc
    return best


def mc_pid():
    proc = mc_proc()
    return proc.pid if proc else None


def mc_game_dir(proc):
    """Pasta do jogo (a que tem saves, options.txt e servers.dat)."""
    candidates = []
    try:
        candidates.append(pathlib.Path(os.readlink(f"/proc/{proc.pid}/cwd")))
    except OSError:
        pass
    if "--gameDir" in proc.argv[:-1]:
        candidates.append(pathlib.Path(proc.argv[proc.argv.index("--gameDir") + 1]))
    m = re.search(r"(/[^\s:]*?/instances/[^/]+)/natives", proc.cmdline)
    if m:
        root = pathlib.Path(m.group(1))
        candidates += [root / "minecraft", root / ".minecraft"]
    for path in candidates:
        if any((path / f).exists() for f in ("options.txt", "servers.dat", "logs")):
            return path
    return None


def mc_instance(pid=None):
    """Nome da instancia, como o launcher mostra (instance.cfg), ou a pasta."""
    proc = mc_proc() if pid is None else next(
        (p for p in processes() if str(p.pid) == str(pid)), None)
    if not proc:
        return None
    game_dir = mc_game_dir(proc)
    if game_dir:
        try:
            cfg = (game_dir.parent / "instance.cfg").read_text(errors="replace")
            m = re.search(r"^name=(.+)$", cfg, re.M)
            if m and m.group(1).strip():
                return m.group(1).strip()
        except OSError:
            pass
        if game_dir.name in ("minecraft", ".minecraft") and \
                game_dir.parent.parent.name == "instances":
            return game_dir.parent.name
    m = re.search(r"instances/([^/]+)/natives", proc.cmdline)
    return m.group(1) if m else None


def mc_peers(pid):
    """(ip, porta) das conexoes TCP estabelecidas do jogo, fora HTTP(S)."""
    try:
        r = subprocess.run(["ss", "-tnp"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    peers = []
    for line in r.stdout.splitlines():
        if f"pid={pid}," not in line:
            continue
        cols = line.split()
        if len(cols) < 5 or cols[0] != "ESTAB":
            continue
        ip, _, port = cols[4].rpartition(":")
        ip = ip.strip("[]").replace("::ffff:", "")
        if not port.isdigit() or int(port) in HTTP_PORTS:
            continue  # login, skins, atualizacao de mod: nao e o servidor
        peers.append((ip, port))
    return peers


def is_loopback(ip):
    return ip.startswith("127.") or ip == "::1"


def mc_log_server(game_dir):
    """(host, porta) do ultimo "Connecting to" do latest.log, ou None."""
    if not game_dir:
        return None
    try:
        with (game_dir / "logs/latest.log").open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 1_000_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    hits = re.findall(r"\]: Connecting to ([^\s,]+), ?(\d+)", tail)
    return (hits[-1][0], hits[-1][1]) if hits else None


def mc_server_names(instance=None, game_dir=None):
    """Hosts salvos no servers.dat (NBT: strings legiveis bastam)."""
    if game_dir is None and instance:
        game_dir = PRISM / instance / "minecraft"
    path = game_dir / "servers.dat" if game_dir else None
    if not path or not path.exists():
        return []
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    hosts = re.findall(rb"[a-zA-Z0-9][a-zA-Z0-9.\-]+\.[a-z]{2,}(?::\d+)?", data)
    return [h.decode() for h in dict.fromkeys(hosts)]


def resolve(host):
    """IPs de um host, com cache de dez minutos."""
    cached = _MC_DNS.get(host)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        ips = set()
    _MC_DNS[host] = (time.monotonic(), ips)
    return ips


def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def read_varint(sock):
    n = shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("servidor fechou")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7
        if shift > 35:
            raise ValueError("varint grande demais")


def mc_ping(host, port, address=None, address_port=None):
    """Server List Ping: (online, max) de jogadores, ou None.

    host vai no handshake (proxies como BungeeCord escolhem o servidor por
    ele); address e address_port sao para onde conectar de fato, quando ja se
    sabe o IP e a porta da conexao aberta.
    """
    key = (host, port, address, address_port)
    cached = _MC_PING.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    result = None
    try:
        with socket.create_connection((address or host, address_port or port),
                                      timeout=4) as s:
            h = (varint(0) + varint(765) + varint(len(host.encode())) + host.encode()
                 + struct.pack(">H", port) + varint(1))
            s.sendall(varint(len(h)) + h)
            s.sendall(varint(1) + varint(0))  # status request
            read_varint(s)  # tamanho do pacote
            read_varint(s)  # id do pacote
            length = read_varint(s)
            body = b""
            while len(body) < length:
                chunk = s.recv(length - len(body))
                if not chunk:
                    break
                body += chunk
            players = json.loads(body.decode("utf-8", "replace")).get("players", {})
            result = [int(players["online"]), int(players["max"])]
    except (OSError, ValueError, KeyError, TypeError, ConnectionError):
        result = None
    _MC_PING[key] = (time.monotonic(), result)
    return result


def mc_running():
    return mc_proc()


def mc_details():
    return mc_instance() or "Minecraft"


def mc_state():
    """Servidor em que o jogo esta conectado (com lotacao), ou singleplayer."""
    proc = mc_proc()
    if not proc:
        return "Jogando", None
    game_dir = mc_game_dir(proc)
    instance = mc_instance(proc.pid)
    peers = mc_peers(proc.pid)
    logged = mc_log_server(game_dir)

    # o log diz o nome; a conexao aberta diz que ainda esta la. A porta do
    # log e a de antes do SRV, entao a conexao pode estar em outra.
    remote = [p for p in peers if not is_loopback(p[0])]
    target = None
    if logged and peers:
        host, port = logged
        peer = next((p for p in peers if p[1] == port), None)
        if peer is None and remote and not is_loopback(host.split(":")[0]):
            peer = remote[0]
        if peer:
            target = (host, port, peer)
    if not target and remote:
        ip, port = remote[0]
        name = None
        for saved in mc_server_names(game_dir=game_dir):
            candidate = saved.split(":")[0]
            if any(p in resolve(candidate) for p, _ in remote):
                name = candidate
                break
        target = (name or ip, port, remote[0])
    if not target:
        return (f"Singleplayer — {instance}" if instance else "Singleplayer"), None

    host, port, (address, address_port) = target
    shown = host if port == "25565" else f"{host}:{port}"
    # a lotacao vai so no party; o Discord ja a escreve ao lado do state
    return f"em {shown}", mc_ping(host, int(port), address, int(address_port))


# ------------------------------------------------------------- catalogo
# chave -> presenca. O client_id vem da base de jogos detectaveis do Discord
# (https://discord.com/api/v9/applications/detectable) ou de um app proprio.
# Como cada entrada e reconhecida esta em entry_rules(); "running" troca a
# regra por uma funcao quando o processo sozinho nao basta.
GAMES = {
    "rs2client": {
        "client_id": "357606832899883008",
        "details": "RuneScape 3",
        "state": "Gielinor",
        "large_text": "RuneScape",
    },
    "TaskBarHero.exe": {
        "client_id": "1510483737685655632",
        "details": "TBH: Task Bar Hero",
        "state": "Jogando",
        "large_text": "TBH: Task Bar Hero",
    },
    # Toca no navegador, entao a presenca segue o MPRIS e as abas abertas, nao
    # um processo proprio. Precisa de um app em discord.com/developers.
    "youtube-music": {
        "poll": 5,  # a posicao so aparece em janelas curtas
        "grace": 10,  # troca de faixa derruba o player por um instante
        "client_id": "1537975657735262368",
        "details": "YouTube Music",  # o state troca pela musica
        "state": ytm_state,          # artista, capa, progresso, anuncio, pagina
        "large_text": "YouTube Music",
        "large_image": YTM_ICON,
        "running": ytm_running,
        "type": 2,  # Ouvindo
        "status_display": "details",  # lista de membros: "Ouvindo <musica>"
        "show_search": False,  # True mostra o termo pesquisado
    },
    # Videos do YouTube em qualquer navegador. Precisa de um app proprio:
    # "YouTube" nao existe na base detectavel do Discord.
    "youtube": {
        "poll": 5,  # a posicao so aparece em janelas curtas
        "grace": 10,  # o autoplay derruba o player por um instante
        "client_id": "1538427046760423474",
        "details": "YouTube",  # o state troca pelo titulo do video
        "large_image": YT_ICON,  # quando nao ha capa (pagina de canal, busca)
        "state": yt_state,     # canal, capa, progresso, live, Shorts, anuncio
        "large_text": "YouTube",
        "running": yt_running,
        "type": 3,  # Assistindo
        "status_display": "details",  # lista de membros: "Assistindo <video>"
        "show_search": False,  # True mostra o termo pesquisado
    },
    # O Stremio nao esta na base detectavel do Discord: o client_id vem de um app
    # criado em discord.com/developers/applications. Sem ele, a entrada fica inativa.
    "stremio": {
        "poll": 5,  # a posicao so aparece em janelas curtas
        "grace": 10,
        "running": stremio_running,
        "client_id": "1536170931540463647",
        "details": "Stremio",
        "state": stremio_state,
        "large_text": "Stremio",
        # nao ha Art Assets no app; usa o proprio icone dele
        "large_image": ("https://cdn.discordapp.com/app-icons/1536170931540463647/"
                        "2c1838e508bcbe74dbafaaef7180be93.png"),
        "type": 3,  # Assistindo
    },
    # Roblox roda pelo Sober (Flatpak, comando "sober"); a experiencia atual
    # sai do log do Sober. O roblox-player: dos links continua valendo.
    "roblox-player:": {
        "client_id": "363445589247131668",
        "match": [{"exe": "sober"}, {"cmdline": "roblox-player:"}],
        "details": roblox_title,
        "state": roblox_state,
        "large_text": "Roblox",
    },
    # Diz apenas que ha uma sessao aberta. Nem o nome da sessao, nem o
    # diretorio, nem o que esta sendo feito: nada disso vira presenca.
    "claude-code": {
        "client_id": "1544203323681407046",
        "details": "Claude Code",
        "state": "Programming",
        "large_text": "Claude Code",
        "large_image": ("https://cdn.discordapp.com/app-icons/1544203323681407046/"
                        "23856947b5b6c5bcc928d1adc9a3a420.png"),
        "running": claude_running,
    },
    "gta-sa.exe": {
        "client_id": "363447565905166336",
        "details": "Grand Theft Auto: San Andreas",
        "state": gta_state,   # bairro, vida e dinheiro, lidos do jogo
        "running": gta_running,
        "poll": 10,
        "large_text": "Grand Theft Auto: San Andreas",
    },
    # o binario nativo e o hl2_linux de varios jogos Source: o -game decide.
    # Um servidor dedicado (srcds_linux -game left4dead2) nao e jogar.
    "left4dead2": {
        "client_id": "356954277803065354",
        "match": [{"exe": "hl2_linux", "args": "-game left4dead2"},
                  {"exe": "left4dead2.exe"}],
        "details": "Left 4 Dead 2",
        "state": l4d2_state,
        "large_text": "Left 4 Dead 2",
    },
    "PrismLauncher/instances": {
        "client_id": "1410791091501928458",
        "running": mc_running,
        # o Discord ja mostra "Minecraft: Java Edition" como titulo; details repete
        "details": mc_details,
        "state": mc_state,  # servidor conectado, atualizado a cada poll
        "large_text": "Minecraft",
    },
}

POLL = 15          # atualizacao da presenca de um app aberto
DETECT_POLL = 5    # procura de apps que ainda nao estao abertos
ALIVE_CHECK = 2    # um app aberto continua aberto?
IPC_TIMEOUT = 15

# Avisa as sessoes que e hora de sair. Sem isso o processo morria com as
# conexoes abertas e o cliente continuava exibindo a ultima atividade: quem
# publicou tem de ser quem apaga, na mesma conexao.
STOPPING = threading.Event()

# entradas renomeadas: o config antigo do usuario continua valendo
LEGACY_KEYS = {"libexec/stremio/stremio": "stremio"}

CONFIG = config_home() / "discord-game-presence.json"


def load_config():
    """Aplica ~/.config/discord-game-presence.json sobre a tabela padrao.

    Formato: {"games": {"<chave>": {"client_id": "...", "details": "...",
    "state": "...", "large_text": "...", "large_image": "...", "exe": "...",
    "args": "...", "cmdline": "...", "match": [...]}}}. Entradas novas sao
    adicionadas; as existentes tem so os campos citados trocados. Um
    client_id vazio, ou "enabled": false, desliga a entrada.

    Outras chaves de primeiro nivel sao ignoradas de proposito: o icone da
    bandeja guarda as preferencias dele no mesmo arquivo, em "tray".
    """
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return

    games = data.get("games") or {}
    for old, new in LEGACY_KEYS.items():  # configs escritos antes da renomeacao
        if old in games:
            games.setdefault(new, {}).update(games.pop(old))

    for proc, fields in games.items():
        if not isinstance(fields, dict):
            continue
        if proc in GAMES:
            if any(k in fields for k in ("exe", "args", "cmdline", "match")):
                GAMES[proc].pop("running", None)  # a regra do usuario manda
            GAMES[proc].update(fields)
        elif fields.get("client_id"):
            GAMES[proc] = {"details": proc, "state": "Jogando",
                           "large_text": proc, **fields}


# ------------------------------------------------------------- IPC
class IPCError(Exception):
    """O Discord respondeu ERROR a um comando."""


def ipc_candidates():
    """Sockets de IPC existentes, na ordem em que vale tentar.

    O cliente nativo (e o Vesktop) criam $XDG_RUNTIME_DIR/discord-ipc-N. O
    Discord do Flathub repassa o socket para $XDG_RUNTIME_DIR/app/<id>/
    (o socat do discord.sh dele), entao essas pastas tambem entram.
    """
    bases = [os.environ.get("XDG_RUNTIME_DIR") or "/tmp"]
    for var in ("TMPDIR", "TMP", "TEMP"):
        if os.environ.get(var):
            bases.append(os.environ[var])
    paths = []
    for base in dict.fromkeys(bases):
        paths += [os.path.join(base, f"discord-ipc-{i}") for i in range(10)]
        for app in sorted(glob.glob(os.path.join(base, "app", "*"))):
            paths += [os.path.join(app, f"discord-ipc-{i}") for i in range(10)]
    return [p for p in dict.fromkeys(paths) if os.path.exists(p)]


def ipc_path():
    found = ipc_candidates()
    return found[0] if found else None


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:  # sem isso um socket fechado no meio girava para sempre
            raise ConnectionError("IPC fechou")
        data += chunk
    return data


def send(sock, op, payload):
    """Manda um frame e devolve a resposta dele.

    Eventos soltos (DISPATCH) sao pulados, PING recebe PONG, CLOSE derruba a
    conexao e um ERROR vira IPCError com a mensagem do Discord, em vez de a
    presenca simplesmente nao aparecer sem explicacao.
    """
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack("<II", op, len(data)) + data)
    for _ in range(50):
        rop, length = struct.unpack("<II", recv_exact(sock, 8))
        raw = recv_exact(sock, length) if length else b""
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {}
        if rop == 2:
            raise ConnectionError(f"Discord fechou: {body.get('code')} {body.get('message')}")
        if rop == 3:
            sock.sendall(struct.pack("<II", 4, len(raw)) + raw)
            continue
        if not isinstance(body, dict):
            continue
        if body.get("evt") == "ERROR":
            raise IPCError((body.get("data") or {}).get("message") or "erro sem mensagem")
        if op == 1 and body.get("cmd") == "DISPATCH":
            continue  # evento, nao a resposta do comando
        return body
    raise ConnectionError("Discord nao respondeu ao comando")


def ipc_connect(client_id):
    """Conexao com handshake feito, no primeiro socket que aceitar, ou None.

    Um socket velho (de um Discord que caiu) recusa a conexao; antes isso
    prendia o daemon nele, agora a busca segue para o proximo.
    """
    for path in ipc_candidates():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(IPC_TIMEOUT)  # sem isso um Discord mudo travaria a thread
        try:
            sock.connect(path)
            send(sock, 0, {"v": 1, "client_id": client_id})
            return sock
        except (OSError, ConnectionError, ValueError, IPCError):
            sock.close()
    return None


def clean_text(value, limit=128):
    """Texto aceito pelo Discord (2 a 128 caracteres), ou None."""
    if value is None:
        return None
    value = " ".join(str(value).split())
    if len(value) < 2:
        return None
    return value if len(value) <= limit else value[:limit - 1] + "…"


def clean_url(value, limit=256):
    """Link http(s) curto o bastante, ou None."""
    value = str(value or "")
    return value if value.startswith(("https://", "http://")) and len(value) <= limit else None


# Tipos que o SET_ACTIVITY aceita (Jogando, Ouvindo, Assistindo, Competindo) e
# qual linha vai para a lista de membros, segundo docs.discord.com.
RPC_TYPES = {0, 2, 3, 5}
STATUS_DISPLAY = {"name": 0, "state": 1, "details": 2}
# Campos que o Discord passou a aceitar em 2025 (links nos textos e nas
# imagens, linha da lista de membros). Se um cliente antigo recusar, a mesma
# atividade sai de novo sem eles.
NEW_FIELDS = ("details_url", "state_url", "status_display_type")
NEW_ASSETS = ("large_url", "small_url")


def sanitize_activity(activity):
    """Corta o que o Discord recusaria: um campo invalido derruba a atividade
    inteira, nao so o campo."""
    kind = activity.get("type", 0)
    out = {"type": kind if kind in RPC_TYPES else 0}
    for key in ("details", "state"):
        text = clean_text(activity.get(key))
        if text:
            out[key] = text
            url = clean_url(activity.get(f"{key}_url"))
            if url:
                out[f"{key}_url"] = url
    display = activity.get("status_display_type")
    if display in (0, 1, 2) and not isinstance(display, bool):
        out["status_display_type"] = display
    times = activity.get("timestamps") or {}
    times = {k: int(v) for k, v in times.items()
             if k in ("start", "end") and isinstance(v, (int, float)) and v > 0}
    if "end" in times and times.get("start", 0) >= times["end"]:
        times.pop("end")
    if times:
        out["timestamps"] = times
    assets, given = {}, activity.get("assets") or {}
    for size in ("large", "small"):
        image = given.get(f"{size}_image")
        if image and len(str(image)) <= 1024:
            assets[f"{size}_image"] = str(image)
            url = clean_url(given.get(f"{size}_url"))
            if url:
                assets[f"{size}_url"] = url
        text = clean_text(given.get(f"{size}_text"))
        if text:
            assets[f"{size}_text"] = text
    if assets:
        out["assets"] = assets
    buttons = []
    for button in activity.get("buttons") or []:
        label = " ".join(str(button.get("label") or "").split())[:32]
        url = str(button.get("url") or "")
        if label and url.startswith(("https://", "http://")) and len(url) <= 512:
            buttons.append({"label": label, "url": url})
    if buttons:
        out["buttons"] = buttons[:2]  # o Discord aceita 2
    party = activity.get("party")
    if party:
        size = party.get("size") or []
        if (len(size) == 2 and all(isinstance(n, int) for n in size)
                and 1 <= size[0] and 0 < size[1]):
            out["party"] = {"id": str(party.get("id")), "size": [min(size), size[1]]}
    return out


def legacy_activity(activity):
    """A mesma atividade sem os campos de 2025."""
    out = {k: v for k, v in activity.items() if k not in NEW_FIELDS}
    if "assets" in out:
        out["assets"] = {k: v for k, v in out["assets"].items() if k not in NEW_ASSETS}
    return out


def clear_presence(client_id):
    """Apaga do Discord a presenca desse app.

    Necessario porque o cliente mantem a ultima atividade publicada mesmo
    depois que a conexao morre: se o daemon for morto (um `systemctl restart`,
    por exemplo), a presenca fica pendurada ate alguem mandar apagar.
    """
    sock = ipc_connect(client_id)
    if not sock:
        return  # Discord fechado: nao ha presenca pendurada para apagar
    try:
        send(sock, 1, {"cmd": "SET_ACTIVITY",
                       "args": {"pid": os.getpid(), "activity": None},
                       "nonce": str(time.time())})
    except (OSError, ValueError, ConnectionError, IPCError):
        pass
    finally:
        sock.close()


def same_activity(old, new):
    """Compara duas atividades ignorando variacao pequena nos tempos.

    A comparacao e sobre a atividade inteira (titulo, texto, imagem, botoes)
    e nao so sobre uma linha dela: trocar de video dentro do mesmo canal muda
    o titulo e mais nada, e comparar so o texto de baixo deixava a presenca
    parada no video anterior.
    """
    if old is None or new is None:
        return old is new
    times_old = old.get("timestamps") or {}
    times_new = new.get("timestamps") or {}
    rest_old = {k: v for k, v in old.items() if k != "timestamps"}
    rest_new = {k: v for k, v in new.items() if k != "timestamps"}
    if rest_old != rest_new or set(times_old) != set(times_new):
        return False
    return all(abs(times_new[k] - times_old[k]) <= 5 for k in times_new)


def build_activity(game, state, start):
    """Atividade a publicar, a partir do (texto, extras) do state."""
    # o segundo item e o tamanho da party (lista) ou campos extras (dict:
    # "details", "timestamps", "buttons", "large_image", "large_text", links...)
    text, extra = state
    fields = extra if isinstance(extra, dict) else {}
    details = fields["details"] if "details" in fields else game["details"]
    assets = {
        # nome de um Art Asset do app, ou uma URL de imagem; o state
        # pode trocar a arte a cada faixa (capa da musica, por exemplo)
        "large_image": fields.get("large_image") or game.get("large_image", "game"),
        "large_text": fields.get("large_text") or game["large_text"],
    }
    for key in ("large_url", "small_image", "small_text", "small_url"):
        if fields.get(key):
            assets[key] = fields[key]
    activity = {
        "type": fields.get("type", game.get("type", 0)),
        "details": details() if callable(details) else details,
        "state": text,
        # jogo sem tempo proprio conta desde que o processo abriu; midia sem
        # posicao conhecida (ou pausada) manda None e fica sem tempo nenhum
        "timestamps": fields["timestamps"] if "timestamps" in fields
                      else {"start": int(start)},
        "assets": assets,
    }
    for key in ("details_url", "state_url"):
        if fields.get(key):
            activity[key] = fields[key]
    display = fields.get("status_display_type",
                         STATUS_DISPLAY.get(game.get("status_display")))
    if display is not None:
        activity["status_display_type"] = display
    if fields.get("buttons"):
        activity["buttons"] = fields["buttons"]
    if extra and not isinstance(extra, dict):  # "(45 of 1000)" ao lado do state
        activity["party"] = {"id": game["client_id"], "size": list(extra)}
    return sanitize_activity(activity)


def session(key, game):
    """Publica a presenca de um app, do inicio ao fim dele."""
    poll = game.get("poll", POLL)
    grace = game.get("grace", 0)

    def current_state():
        s = game["state"]
        if not callable(s):
            return (s, None)
        return s()  # None significa "nao ha mais nada tocando"

    # so conecta quando ha o que mostrar: um app aberto sem nada tocando
    # (Stremio parado no menu) nao abre e fecha conexao a cada volta
    hit = detect(key, game)
    first_state = current_state() if hit else None
    if first_state is None:
        return
    sock = ipc_connect(game["client_id"])
    if not sock:
        return
    opened = time.time()

    def publish(activity):
        send(sock, 1, {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(time.time()),
        })
        times = activity.get("timestamps") or {}
        if "end" in times:
            marca = (f" [{duration_label((time.time() - times['start']) * 1_000_000)}"
                     f"/{duration_label((times['end'] - times['start']) * 1_000_000)}]")
        elif times:
            marca = f" [desde {time.strftime('%H:%M', time.localtime(times['start']))}]"
        else:
            marca = " [sem tempo]"
        log(f"publicou {game['large_text']}: "
            f"{activity.get('details')} | {activity.get('state')}{marca}")

    published, refused, missing, legacy = None, None, None, False
    state, next_state = first_state, time.monotonic() + poll
    try:
        while True:
            if state is None:
                # o app sumiu; da um tempo antes de apagar, para uma troca de
                # faixa ou um reload nao piscarem a presenca
                missing = missing or time.monotonic()
                if time.monotonic() - missing >= grace:
                    break
            else:
                missing = None
                activity = build_activity(game, state, hit_start(hit) or opened)
                if not same_activity(published, activity) and activity != refused:
                    try:
                        publish(legacy_activity(activity) if legacy else activity)
                        published = activity
                    except IPCError as error:
                        plain, reason = legacy_activity(activity), error
                        if not legacy and plain != activity:
                            try:  # cliente que nao conhece os campos de 2025
                                publish(plain)
                                published, legacy, reason = activity, True, None
                                log(f"{game['large_text']}: publicado sem links ({error})")
                            except IPCError as again:
                                reason = again
                        if reason is not None:
                            refused = activity  # nao insiste na mesma atividade recusada
                            log(f"o Discord recusou {game['large_text']}: {reason}")
            # Confere a cada poucos segundos se o app continua aberto, e so
            # rele o estado no ritmo dele: fechar o jogo tirava a presenca do
            # perfil so na volta seguinte, ate 15 s depois.
            while not STOPPING.wait(ALIVE_CHECK):
                hit = detect(key, game)
                if not hit:
                    state = None
                    break
                if state is None or time.monotonic() >= next_state:
                    state = current_state()
                    next_state = time.monotonic() + poll
                    break
            if STOPPING.is_set():
                break
    finally:
        try:
            send(sock, 1, {"cmd": "SET_ACTIVITY",
                           "args": {"pid": os.getpid(), "activity": None},
                           "nonce": str(time.time())})
            if published:
                log(f"apagou {game['large_text']}")
        except (OSError, ValueError, ConnectionError, IPCError) as error:
            log(f"nao consegui apagar {game['large_text']}: {error}")
        sock.close()


def watch(key, game):
    """Espera o app abrir, publica a presenca, repete."""
    while not STOPPING.is_set():
        try:
            found = detect(key, game)
        except Exception as error:  # um detector quebrado nao derruba os outros
            log(f"{key}: deteccao falhou ({error!r})")
            found = None
        if found:
            try:
                session(key, game)
            except (OSError, ConnectionError, ValueError) as error:
                log(f"{key}: sessao caiu ({error})")
            except Exception as error:
                log(f"{key}: erro inesperado ({error!r})")
        if STOPPING.wait(DETECT_POLL):
            return


def active_games():
    return [(key, game) for key, game in GAMES.items()
            if game.get("client_id") and game.get("enabled", True)]


# ------------------------------------------------------------- atualizacao
# A bandeja avisa quando sai uma release nova no GitHub e instala com um
# clique. O que se baixa depende de como esta copia foi instalada: os scripts
# e as units (install.sh), o pacote da distro (pkexec, porque e root) ou o
# AppImage. Nada e trocado sem conferir o sha256 que o GitHub publica para cada
# arquivo da release (campo "digest"), ou o SHA256SUMS anexado a ela.
REPO = "gabrielmf1998/Discord-Arrpc-Detector-KDE"
RELEASES_API = (os.environ.get("DGP_RELEASES_API")
                or f"https://api.github.com/repos/{REPO}/releases/latest")
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UPDATE_DIR = pathlib.Path(os.environ.get("XDG_CACHE_HOME")
                          or pathlib.Path.home() / ".cache") / "discord-game-presence" / "update"
UPDATE_LIMIT = 64 << 20  # nenhum arquivo da release chega perto disso
SCRIPT_FILES = {  # arquivo da release -> lugar dele numa instalacao por script
    "discord-game-presence.py": ".local/bin/discord-game-presence.py",
    "discord-presence-tray.py": ".local/bin/discord-presence-tray.py",
    "discord-game-presence.service": ".config/systemd/user/discord-game-presence.service",
    "discord-game-presence-tray.service": ".config/systemd/user/discord-game-presence-tray.service",
}
SCRIPT_REQUIRED = ("discord-game-presence.py", "discord-presence-tray.py")
PACKAGE_ASSETS = {
    "pacman": r"discord-game-presence-{v}-\d+-any\.pkg\.tar\.zst",
    "deb": r"discord-game-presence_{v}-\d+_all\.deb",
    "rpm": r"discord-game-presence-{v}-\d+\.noarch\.rpm",
    "appimage": r"DiscordPresence-{v}-x86_64\.AppImage",
}
SELF_UPDATING = ("script", "appimage")  # instalam sem pedir senha
DAEMON_SERVICE = "discord-game-presence.service"
TRAY_SERVICE = "discord-game-presence-tray.service"


class UpdateError(Exception):
    """Atualizacao que nao deu para fazer, com o motivo (em ingles, vai para a bandeja)."""


def version_tuple(text):
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", (text or "").strip())
    return tuple(int(part or 0) for part in m.groups()) if m else None


def is_newer(release, current=VERSION):
    found = version_tuple(release["version"]) if release else None
    return bool(found) and found > (version_tuple(current) or (0,))


def _fetch(url, timeout, accept=None):
    headers = {"User-Agent": f"discord-game-presence/{VERSION}"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                  timeout=timeout)


def latest_release(timeout=20):
    """A ultima release publicada (rascunho e pre-release nao contam), ou None."""
    with _fetch(RELEASES_API, timeout, "application/vnd.github+json") as response:
        data = json.loads(response.read(4 << 20))
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    if not version_tuple(tag):
        return None
    assets = {}
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict) or not asset.get("name") or not asset.get("browser_download_url"):
            continue
        if asset.get("state", "uploaded") != "uploaded":
            continue  # o gh ainda esta subindo esse arquivo
        digest = str(asset.get("digest") or "").lower()
        assets[asset["name"]] = {
            "url": asset["browser_download_url"],
            "sha256": digest[7:] if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else None}
    return {"version": tag.lstrip("v"), "tag": tag, "name": data.get("name") or tag,
            "page": data.get("html_url") or RELEASES_PAGE,
            "notes": str(data.get("body") or ""), "assets": assets}


def install_kind(path=None):
    """Como esta copia foi instalada: script, pacman, deb, rpm, appimage ou manual.

    Manual e qualquer outra coisa (um clone do git, um link para ele): essa
    copia so e avisada, nunca sobrescrita.
    """
    path = pathlib.Path(path or __file__).resolve()
    appdir = os.environ.get("APPDIR")
    if os.environ.get("APPIMAGE") and appdir and str(path).startswith(appdir.rstrip("/") + "/"):
        return "appimage"
    if path.parent == (pathlib.Path.home() / ".local/bin").resolve():
        return "script"
    if str(path).startswith("/usr/"):
        for kind, command in (("pacman", ["pacman", "-Qqo"]), ("deb", ["dpkg-query", "-S"]),
                              ("rpm", ["rpm", "-qf"])):
            if not shutil.which(command[0]):
                continue
            try:
                done = subprocess.run(command + [str(path)], capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.returncode == 0:
                return kind
    return "manual"


def update_files(release, kind):
    """{nome: arquivo da release} do que esta instalacao precisa baixar."""
    assets = release["assets"]
    if kind == "script":
        missing = [name for name in SCRIPT_REQUIRED if name not in assets]
        if missing:
            raise UpdateError("the release has no " + " or ".join(missing))
        return {name: assets[name] for name in SCRIPT_FILES if name in assets}
    pattern = PACKAGE_ASSETS.get(kind)
    if not pattern:
        raise UpdateError("this copy was not installed by the installer or a package; "
                          "download it from the release page")
    wanted = re.compile(pattern.format(v=re.escape(release["version"])))
    found = sorted(name for name in assets if wanted.fullmatch(name))
    if not found:
        raise UpdateError(f"the release has no {kind} package")
    return {found[-1]: assets[found[-1]]}


def release_sums(release, timeout=20):
    """O SHA256SUMS da release, para arquivo sem o digest do GitHub."""
    info = release["assets"].get("SHA256SUMS")
    if not info:
        return {}
    with _fetch(info["url"], timeout) as response:
        text = response.read(1 << 20).decode("utf-8", "replace")
    sums = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line.strip())
        if m:
            sums[m.group(2)] = m.group(1).lower()
    return sums


def download_checked(url, dest, expected, limit=UPDATE_LIMIT, timeout=60):
    digest, total = hashlib.sha256(), 0
    with _fetch(url, timeout) as response, open(dest, "wb") as out:
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateError(f"{dest.name} is larger than any release file should be")
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != expected:
        raise UpdateError(f"{dest.name} failed the sha256 check; nothing was changed")


def _install_script(release, staged, home=None):
    """Troca os arquivos de uma instalacao por script, guardando os antigos."""
    home = pathlib.Path(home or pathlib.Path.home())
    source = staged["discord-game-presence.py"].read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^VERSION = "([^"]+)"$', source, re.M)
    if not m or m.group(1) != release["version"]:
        raise UpdateError(f"the downloaded daemon is not version {release['version']}")
    for name in SCRIPT_REQUIRED:
        try:
            compile(staged[name].read_bytes(), name, "exec")
        except (SyntaxError, ValueError) as error:
            raise UpdateError(f"{name} does not compile: {error}") from None
    backup = home / ".local/share/discord-game-presence/backup" / VERSION
    backup.mkdir(parents=True, exist_ok=True)
    units = False
    for name, src in staged.items():
        dest = home / SCRIPT_FILES[name]
        if dest.exists():
            shutil.copy2(dest, backup / name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fresh = dest.with_name(dest.name + ".new")
        shutil.copyfile(src, fresh)
        os.chmod(fresh, 0o755 if name.endswith(".py") else 0o644)
        os.replace(fresh, dest)  # quem esta rodando segue com o arquivo antigo
        units = units or name.endswith(".service")
    if units:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def _install_appimage(staged):
    target = pathlib.Path(os.environ["APPIMAGE"])
    fresh = target.with_name(target.name + ".new")
    try:
        shutil.copyfile(next(iter(staged.values())), fresh)
        os.chmod(fresh, 0o755)
        os.replace(fresh, target)
    except OSError as error:
        raise UpdateError(f"could not replace {target}: {error.strerror}") from None


def package_command(kind, package):
    """O comando (sem o pkexec) que instala o pacote baixado."""
    options = {
        "pacman": [["pacman", "-U", "--noconfirm", package]],
        "deb": [["apt-get", "install", "-y", package]],
        "rpm": [["dnf", "install", "-y", package],
                ["zypper", "--non-interactive", "install", "--allow-unsigned-rpm", package],
                ["rpm", "-U", package]],
    }
    for command in options.get(kind, []):
        if shutil.which(command[0]):
            return command
    raise UpdateError(f"no {kind} package manager found")


def _install_package(kind, package):
    command = package_command(kind, str(package))
    if not shutil.which("pkexec"):
        raise UpdateError("pkexec is missing; install the package by hand: " + " ".join(command))
    try:
        done = subprocess.run(["pkexec"] + command, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise UpdateError("the package manager took too long") from None
    if done.returncode in (126, 127):  # janela de senha fechada ou negada
        raise UpdateError("authorization was cancelled; nothing was changed")
    if done.returncode:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise UpdateError(f"{command[0]} failed: {tail[0][:200]}")


def install_update(release, kind=None, path=None):
    """Baixa, confere e instala a release. Nao reinicia nada (restart_services)."""
    kind = kind or install_kind(path)
    files = update_files(release, kind)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(UPDATE_DIR / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError("another update is already running") from None
        work = UPDATE_DIR / release["version"]
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        try:
            sums = (release_sums(release)
                    if any(not info["sha256"] for info in files.values()) else {})
            staged = {}
            for name, info in files.items():
                expected = info["sha256"] or sums.get(name)
                if not expected:
                    raise UpdateError(f"{name} has no published sha256; nothing was changed")
                download_checked(info["url"], work / name, expected)
                staged[name] = work / name
            if kind == "script":
                _install_script(release, staged)
            elif kind == "appimage":
                _install_appimage(staged)
            else:
                _install_package(kind, staged[next(iter(staged))])
        except (OSError, urllib.error.URLError) as error:
            raise UpdateError(f"download failed: {getattr(error, 'reason', error)}") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return kind


def restart_services(tray=True):
    """Reinicia o daemon (se estava rodando) e, se pedido, o servico da bandeja."""
    def systemctl(*args):
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    systemctl("daemon-reload")  # um pacote novo pode trazer units novas
    if systemctl("is-active", DAEMON_SERVICE).stdout.strip() == "active":
        systemctl("restart", DAEMON_SERVICE)
    if tray and systemctl("is-active", TRAY_SERVICE).stdout.strip() == "active":
        systemctl("--no-block", "restart", TRAY_SERVICE)


def update_cli(install):
    try:
        release = latest_release()
    except (OSError, ValueError) as error:
        print(f"nao deu para consultar as releases: {error}")
        return 1
    if not is_newer(release):
        print(f"{VERSION} e a versao mais nova")
        return 0
    print(f"{VERSION} -> {release['version']} disponivel: {release['page']}")
    if not install:
        return 0
    try:
        kind = install_update(release)
    except UpdateError as error:
        print(f"nao atualizou: {error}")
        return 1
    restart_services()
    print(f"atualizado para {release['version']} ({kind})")
    return 0


def scan_report():
    """--scan: o que esta aberto agora, por qual processo, desde quando."""
    load_config()
    lines = [f"discord-game-presence {VERSION}",
             f"sockets IPC: {', '.join(ipc_candidates()) or 'nenhum'}", ""]
    for key, game in sorted(GAMES.items(), key=lambda kv: kv[1]["large_text"].lower()):
        enabled = game.get("client_id") and game.get("enabled", True)
        try:
            hit = detect(key, game)
        except Exception as error:
            hit, why = None, f"erro: {error!r}"
        else:
            why = ""
        if isinstance(hit, list):
            why = ", ".join(f"pid {p.pid} ({p.argv[0][-60:]})" for p in hit[:3])
        elif isinstance(hit, Proc):
            why = f"pid {hit.pid} ({hit.argv[0][-60:]})"
        elif isinstance(hit, int) and not isinstance(hit, bool):
            why = f"pid {hit}"
        elif isinstance(hit, str):
            why = hit
        start = hit_start(hit) if hit else None
        since = f", aberto desde {time.strftime('%H:%M:%S', time.localtime(start))}" if start else ""
        mark = "ABERTO " if hit else "fechado"
        off = "" if enabled else "  [desligado]"
        lines.append(f"{mark}  {game['large_text']}{off}"
                     + (f"\n         {why}{since}" if hit else ""))
    found = media_now()
    if found:
        lines += ["", "players MPRIS:"]
        for media in found:
            lines.append(f"  {media['player']} ({media['process']}): "
                         f"{media['service'] or 'outro'} via {media['source']}, {media['status']}")
    tabs = open_tabs()
    if tabs:
        lines += ["", f"abas do Chrome lidas: {len(tabs)}, do YouTube: "
                      f"{sum(1 for t in tabs if media_service(t.url))}"]
    windows = browser_windows()
    lines.append("janelas do navegador pelo KWin: "
                 + ("sem KWin" if windows is None else str(len(windows))))
    return "\n".join(lines)


def main():
    if "--version" in sys.argv[1:]:
        print(VERSION)
        return
    if "--scan" in sys.argv[1:]:
        print(scan_report())
        return
    if "--check-update" in sys.argv[1:] or "--update" in sys.argv[1:]:
        sys.exit(update_cli("--update" in sys.argv[1:]))

    load_config()
    active = active_games()

    # todo o catalogo, nao so o que esta ligado: desligar um app tambem tem de
    # apagar a presenca que ele deixou
    ids = list(dict.fromkeys(game["client_id"] for game in GAMES.values()
                             if game.get("client_id")))
    for client_id in ids:  # restos de uma execucao anterior
        clear_presence(client_id)

    threads = [threading.Thread(target=watch, args=(key, game), daemon=True)
               for key, game in active]

    def shutdown(*_):
        STOPPING.set()  # cada sessao apaga a sua presenca e fecha a conexao
        deadline = time.monotonic() + 6
        for thread in threads:
            thread.join(timeout=max(0.1, deadline - time.monotonic()))
        for client_id in ids:  # rede de seguranca, para o que nao fechou
            clear_presence(client_id)
        log("encerrado")
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"{VERSION}: vigiando {len(threads)} app(s): "
        + ", ".join(sorted(game["large_text"] for _, game in active)))
    for thread in threads:
        thread.start()
    while not STOPPING.wait(3600):
        pass


if __name__ == "__main__":
    main()

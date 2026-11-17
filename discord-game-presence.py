#!/usr/bin/env python3
"""Rich Presence de jogos que o Discord nao detecta no Linux.

A base de jogos detectaveis do Discord so lista executaveis win32/darwin (ou
nenhum), entao nem o cliente nativo do RuneScape nem jogos rodando sob Proton
sao reconhecidos. Este daemon procura os processos, fala o protocolo IPC do
Discord direto e publica a presenca enquanto o jogo estiver aberto.

    discord-game-presence            roda o daemon
    discord-game-presence --scan     mostra o que seria detectado agora, e por que
    discord-game-presence --version
"""
import glob
import gzip
import json
import os
import pathlib
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib

VERSION = "1.2.0"

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
    if not (position and length) or position > length:
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
# Chromium nao poem: publicam so titulo, canal e duracao. Para eles a URL sai
# do historico do proprio navegador, procurando a visita recente cujo titulo
# de pagina e o titulo que esta tocando ("<titulo> - YouTube", "<faixa> |
# YouTube Music"). Sem essa visita (historico desligado, aba anonima) o player
# nao conta como YouTube: melhor nao mostrar do que mostrar o app errado.

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
HISTORY_MAX_AGE = 24 * 3600
WEBKIT_EPOCH = 11644473600  # segundos entre 1601-01-01 e 1970-01-01
HISTORY_SQL = """
    SELECT url, title, last_visit_time FROM urls
    WHERE last_visit_time > ? AND instr(title, ?) > 0
      AND (url LIKE 'https://www.youtube.com/%' OR url LIKE 'https://m.youtube.com/%'
           OR url LIKE 'https://music.youtube.com/%' OR url LIKE 'https://youtu.be/%')
    ORDER BY last_visit_time DESC LIMIT 20
"""

_HISTORY_FILES = {"stamp": -1e9, "files": []}
_HISTORY_CACHE = {}


def config_home():
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME",
                                       pathlib.Path.home() / ".config"))


def history_files():
    """Os arquivos History de todos os perfis de navegadores Chromium."""
    if time.monotonic() - _HISTORY_FILES["stamp"] < 60:
        return _HISTORY_FILES["files"]
    roots = [config_home() / d for d in CHROMIUM_DIRS]
    roots += [pathlib.Path.home() / ".var/app" / app / "config" / d
              for app, d in CHROMIUM_FLATPAKS.items()]
    files = []
    for root in roots:
        for profile in ("Default", "Profile *"):
            files += [pathlib.Path(p) for p in glob.glob(str(root / profile / "History"))]
    _HISTORY_FILES.update(stamp=time.monotonic(), files=files)
    return files


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


def page_title_is(page, title):
    """O titulo da pagina e exatamente o do que esta tocando."""
    page = re.sub(r"^\(\d+\+?\)\s*", "", page or "")  # "(3) " das notificacoes
    for suffix in (" - YouTube", " | YouTube Music", " - YouTube Music"):
        if page.endswith(suffix):
            page = page[:-len(suffix)]
            break
    return page == title


def history_url(title, files=None):
    """URL do YouTube que o historico dos Chromium associa ao titulo."""
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
            if not youtube_video_id(url):
                continue
            candidate = (page_title_is(page, title), visit or 0, url)
            if best is None or candidate > best:
                best = candidate
    url = best[2] if best else ""
    _HISTORY_CACHE[title] = (url, now)
    return url


def player_url(process, meta):
    """Endereco da pagina que esta tocando: do MPRIS, ou do historico."""
    url = meta.get("xesam:url") or ""
    if url:
        return url
    if process in CHROMIUM_PROCESSES:
        return history_url(meta.get("xesam:title") or "")
    return ""


def media_player(service):
    """(player, metadata, url) de quem toca o servico pedido, ou None.

    Cada aba com midia pode virar um player e o bus name do Firefox muda a
    cada boot, entao a escolha e pela URL, nao pelo nome. Entre duas abas do
    mesmo servico, a que esta tocando ganha da que esta pausada.
    """
    best = None
    for name, process in mpris_players():
        props = mpris_props(name)
        meta = props.get("Metadata")
        if not isinstance(meta, dict):
            continue
        url = player_url(process, meta)
        if media_service(url) != service:
            continue
        playing = props.get("PlaybackStatus") == "Playing"
        if best is None or (playing and not best[0]):
            best = (playing, name, meta, url)
    return best[1:] if best else None


def firefox_player(wanted):
    """Compatibilidade: primeiro player cuja URL satisfaz o teste."""
    for name, process in mpris_players():
        meta = mpris_meta(name)
        if wanted(player_url(process, meta)):
            return name
    return None


def is_ytm(url):
    return media_service(url) == "ytm"


def is_youtube(url):
    return media_service(url) == "youtube"


def ytm_player():
    found = media_player("ytm")
    return found[0] if found else None


def ytm_running():
    player = ytm_player()
    return player if player and not media_idle(player) else None


def yt_running():
    found = media_player("youtube")
    return found[0] if found and not media_idle(found[0]) else None


def yt_title():
    """Titulo do video, que vai na primeira linha da presenca."""
    found = media_player("youtube")
    if not found:
        return "YouTube"
    return (found[1].get("xesam:title") or "YouTube")[:128]


def yt_state():
    """Video em reproducao no YouTube: titulo, canal, capa e progresso."""
    found = media_player("youtube")
    if not found:  # aba fechada: encerra a presenca em vez de publicar generico
        return None
    player, meta, url = found
    channel = first(meta.get("xesam:artist"))
    title = meta.get("xesam:title") or "YouTube"

    extra = {}
    video = youtube_video_id(url)
    if video:  # miniatura do proprio video como icone
        extra["large_image"] = f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"
        extra["large_text"] = title[:128]
        extra["buttons"] = [{"label": "Assistir no YouTube",
                             "url": f"https://youtu.be/{video}"}]

    if mpris("PlaybackStatus", player) != "Playing":
        anchored_position(title, mpris_position(player), playing=False)
        return f"{channel} (pausado)" if channel else "Pausado", extra or None

    length = track_length(title, meta, video)
    position = anchored_position(title, mpris_position(player))
    extra["timestamps"] = media_timestamps(title, position, length, player)
    text = channel or "YouTube"
    if not extra["timestamps"] and length:  # sem barra, ao menos a duracao
        text = f"{text}  ·  {duration_label(length)}"
    return text, extra


_ART_CACHE = {}


def ytm_art(meta, url=None):
    """Capa da musica. O navegador nao publica uma capa utilizavel, entao:
    miniatura do video quando a URL traz o id, senao a capa do album pela
    busca do iTunes."""
    video = youtube_video_id(url or meta.get("xesam:url") or "")
    if video:
        return f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"

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
    _ART_CACHE[term] = art
    return art


def ytm_state():
    """Musica atual, progresso na faixa e links."""
    found = media_player("ytm")
    if not found:  # aba fechada: encerra a presenca em vez de publicar generico
        return None
    player, meta, url = found
    artist = first(meta.get("xesam:artist"))
    title = meta.get("xesam:title") or "YouTube Music"
    label = f"{title} — {artist}" if artist else title

    extra = {}
    art = ytm_art(meta, url)
    if art:  # capa da musica no lugar do icone do app
        extra["large_image"] = art
        extra["large_text"] = meta.get("xesam:album") or title
    if url:
        extra["buttons"] = [{"label": "Ouvir no YouTube Music", "url": url}]
    if artist:  # o MPRIS nao traz o canal do artista, entao vai pela busca
        query = urllib.parse.quote(artist)
        extra.setdefault("buttons", []).append(
            {"label": f"Artista: {artist}"[:32],
             "url": f"https://music.youtube.com/search?q={query}"})

    if mpris("PlaybackStatus", player) != "Playing":
        anchored_position(title, mpris_position(player), playing=False)
        return f"{label} (pausado)", extra or None

    length = track_length(title, meta)
    position = anchored_position(title, mpris_position(player))
    extra["timestamps"] = media_timestamps(title, position, length, player)
    if not extra["timestamps"] and length:
        label = f"{label}  ·  {duration_label(length)}"
    return label, extra


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
    # Toca no navegador, entao a presenca segue o MPRIS, nao um processo proprio.
    # Precisa de um app criado em discord.com/developers/applications.
    "youtube-music": {
        "poll": 5,  # a posicao so aparece em janelas curtas
        "grace": 10,  # troca de faixa derruba o player por um instante
        "client_id": "1537975657735262368",
        "details": "YouTube Music",
        "state": ytm_state,
        "large_text": "YouTube Music",
        "large_image": ("https://cdn.discordapp.com/app-icons/1537975657735262368/"
                        "1637a3eccd8d9c22acf127e814bf088b.png"),
        "running": ytm_running,
        "type": 2,  # Ouvindo
    },
    # Videos comuns do YouTube, em qualquer navegador, tambem via MPRIS. Precisa
    # de um app proprio: "YouTube" nao existe na base detectavel do Discord.
    "youtube": {
        "poll": 5,  # a posicao so aparece em janelas curtas
        "grace": 10,  # o autoplay derruba o player por um instante
        "client_id": "1538427046760423474",
        "details": yt_title,   # titulo do video
        # usado quando o video nao expoe id na URL (pagina de canal, por exemplo)
        "large_image": ("https://cdn.discordapp.com/app-icons/1538427046760423474/"
                        "3f7a95b3c425b18bda601ff2f2e44b2d.png"),
        "state": yt_state,     # canal, capa e progresso
        "large_text": "YouTube",
        "running": yt_running,
        "type": 3,  # Assistindo
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


def sanitize_activity(activity):
    """Corta o que o Discord recusaria: um campo invalido derruba a atividade
    inteira, nao so o campo."""
    out = {"type": activity.get("type", 0)}
    for key in ("details", "state"):
        text = clean_text(activity.get(key))
        if text:
            out[key] = text
    times = activity.get("timestamps") or {}
    times = {k: int(v) for k, v in times.items()
             if k in ("start", "end") and isinstance(v, (int, float)) and v > 0}
    if "end" in times and times.get("start", 0) >= times["end"]:
        times.pop("end")
    if times:
        out["timestamps"] = times
    assets = {}
    image = (activity.get("assets") or {}).get("large_image")
    if image and len(str(image)) <= 1024:
        assets["large_image"] = str(image)
    text = clean_text((activity.get("assets") or {}).get("large_text"))
    if text:
        assets["large_text"] = text
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
    # "timestamps", "buttons", "large_image", "large_text")
    text, extra = state
    details = game["details"]
    fields = extra if isinstance(extra, dict) else {}
    activity = {
        "type": game.get("type", 0),
        "details": details() if callable(details) else details,
        "state": text,
        # jogo sem tempo proprio conta desde que o processo abriu; midia sem
        # posicao conhecida manda None e fica sem tempo nenhum
        "timestamps": fields["timestamps"] if "timestamps" in fields
                      else {"start": int(start)},
        "assets": {
            # nome de um Art Asset do app, ou uma URL de imagem; o state
            # pode trocar a arte a cada faixa (capa da musica, por exemplo)
            "large_image": fields.get("large_image") or game.get("large_image", "game"),
            "large_text": fields.get("large_text") or game["large_text"],
        },
    }
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

    published, refused, missing = None, None, None
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
                        publish(activity)
                        published = activity
                    except IPCError as error:
                        refused = activity  # nao insiste na mesma atividade recusada
                        log(f"o Discord recusou {game['large_text']}: {error}")
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
    players = mpris_players()
    if players:
        lines += ["", "players MPRIS:"]
        for name, process in players:
            meta = mpris_meta(name)
            url = player_url(process, meta)
            origem = "xesam:url" if meta.get("xesam:url") else ("historico" if url else "-")
            lines.append(f"  {name} ({process}): {media_service(url) or 'outro'}"
                         f" via {origem}, {mpris_props(name).get('PlaybackStatus')}")
    return "\n".join(lines)


def main():
    if "--version" in sys.argv[1:]:
        print(VERSION)
        return
    if "--scan" in sys.argv[1:]:
        print(scan_report())
        return

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

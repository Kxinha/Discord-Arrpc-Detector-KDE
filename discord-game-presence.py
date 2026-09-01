#!/usr/bin/env python3
"""Rich Presence de jogos que o Discord nao detecta no Linux.

A base de jogos detectaveis do Discord so lista executaveis win32/darwin (ou
nenhum), entao nem o cliente nativo do RuneScape nem jogos rodando sob Proton
sao reconhecidos. Este daemon procura os processos, fala o protocolo IPC do
Discord direto e publica a presenca enquanto o jogo estiver aberto.
"""
import gzip
import json
import os
import pathlib
import re
import signal
import socket
import struct
import subprocess
import threading
import urllib.parse
import urllib.request
import time

PRISM = pathlib.Path.home() / ".local/share/PrismLauncher/instances"


def log(message):
    """Uma linha no journal: journalctl --user -u discord-game-presence."""
    print(message, flush=True)


def mc_pid():
    r = subprocess.run(["pgrep", "-f", "PrismLauncher/instances"], capture_output=True, text=True)
    pids = r.stdout.split()
    return pids[0] if pids else None


def mc_instance(pid):
    try:
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return None
    m = re.search(r"instances/([^/]+)/natives", cmdline)
    return m.group(1) if m else None


def mc_peers(pid):
    """IPs remotos das conexoes TCP do processo, sem loopback/LAN."""
    r = subprocess.run(["ss", "-tnp"], capture_output=True, text=True)
    peers = []
    for line in r.stdout.splitlines():
        if f"pid={pid}," not in line:
            continue
        cols = line.split()
        if len(cols) < 5:
            continue
        peer = cols[4].rsplit(":", 1)
        ip = peer[0].strip("[]").replace("::ffff:", "")
        if ip.startswith(("127.", "192.168.", "10.", "::1")):
            continue
        peers.append((ip, peer[1]))
    return peers


def mc_server_names(instance):
    """Hosts salvos no servers.dat da instancia (NBT: strings legiveis bastam)."""
    if not instance:
        return []
    path = PRISM / instance / "minecraft" / "servers.dat"
    if not path.exists():
        return []
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    hosts = re.findall(rb"[a-zA-Z0-9][a-zA-Z0-9.\-]+\.[a-z]{2,}(?::\d+)?", data)
    return [h.decode() for h in dict.fromkeys(hosts)]


def mpris(prop, player="org.mpris.MediaPlayer2.Stremio"):
    r = subprocess.run(
        ["busctl", "--user", "--json=short", "get-property", player,
         "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", prop],
        capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        return json.loads(r.stdout)["data"]
    except (ValueError, KeyError):
        return None


def mpris_names(prefix):
    """Bus names MPRIS que comecam com o prefixo (o do Firefox muda a cada boot)."""
    r = subprocess.run(["busctl", "--user", "--list", "--no-legend"],
                       capture_output=True, text=True)
    full = f"org.mpris.MediaPlayer2.{prefix}"
    return [line.split()[0] for line in r.stdout.splitlines()
            if line.split() and line.split()[0].startswith(full)]


def mpris_meta(player):
    meta = mpris("Metadata", player) or {}
    return {k: (v["data"] if isinstance(v, dict) else v) for k, v in meta.items()}


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

    O Firefox publica mpris:length de forma intermitente - some do metadata no
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


def mpris_position(player):
    """Posicao informada pelo player, ou None quando ele nao informa.

    O Firefox nao implementa isso: devolve 0 fixo e nunca publica
    mpris:length. Um zero aqui vale como "nao sei", nao como inicio da faixa -
    fabricar posicao a partir dele fazia a barra mentir depois de qualquer
    seek, que e justamente quando ela importa.
    """
    reported = mpris("Position", player)
    return reported if reported else None


# O Firefox informa a posicao em rajadas: publica por um tempo depois de um
# seek ou de uma troca de video - quando a pagina chama setPositionState - e
# volta a zero. Guardar a ultima leitura verdadeira e seguir dali cobre os
# buracos, e um seek do usuario faz o proprio Firefox reportar de novo,
# corrigindo a conta sozinho. Sem nenhuma leitura verdadeira nao ha o que
# projetar: ai a presenca fica sem tempo, em vez de inventar um.
_POSITION_ANCHOR = {}
ANCHOR_MAX_AGE = 600


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


def position_elsewhere(title):
    """Posicao e duracao vindas de outro player que anuncie o mesmo titulo.

    O Firefox informa a posicao de forma intermitente. A extensao Plasma
    Browser Integration e os navegadores baseados em Chromium publicam um
    player MPRIS proprio com os numeros de verdade; se um deles estiver com o
    mesmo titulo, a barra passa a ser exata sem precisar de configuracao.
    """
    for name in mpris_names(""):
        if "firefox" in name:
            continue
        meta = mpris_meta(name)
        if (meta.get("xesam:title") or "") != title:
            continue
        position = mpris("Position", name)
        if position:
            return position, meta.get("mpris:length")
    return None, None


def media_timestamps(title, position, length):
    """Barra de progresso, so quando alguem souber a posicao de verdade.

    Sem posicao nao ha meio-termo util: um cronometro correndo ao lado do
    titulo de um video parece a posicao dele, e estaria sempre errado. Melhor
    nao mostrar tempo nenhum e deixar a duracao no texto.
    """
    if not position:  # o player nao informa; talvez outro esteja com o mesmo
        position, other_length = position_elsewhere(title)
        length = length or other_length
    if not (position and length):
        return None
    now = time.time()
    return {"start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000)}


def duration_label(length):
    """25:36 a partir de microssegundos, para acompanhar o texto."""
    if not length:
        return ""
    total = int(length / 1_000_000)
    return f"{total // 60}:{total % 60:02d}"


def firefox_player(wanted):
    """Player do Firefox cuja aba casa com o site pedido.

    O bus name muda a cada boot, e cada aba com midia vira um player; entao a
    escolha e pela URL, nao pelo nome.
    """
    for name in mpris_names("firefox"):
        url = mpris_meta(name).get("xesam:url") or ""
        if wanted(url):
            return name
    return None


def is_ytm(url):
    return "music.youtube.com" in url


def is_youtube(url):
    return "youtube.com" in url and not is_ytm(url)


def ytm_player():
    return firefox_player(is_ytm)


def ytm_running():
    player = ytm_player()
    return bool(player) and not media_idle(player)


def yt_running():
    player = firefox_player(is_youtube)
    return bool(player) and not media_idle(player)


def yt_title():
    """Titulo do video, que vai na primeira linha da presenca."""
    player = firefox_player(is_youtube)
    if not player:
        return "YouTube"
    return (mpris_meta(player).get("xesam:title") or "YouTube")[:128]


def yt_state():
    """Video em reproducao no YouTube: titulo, canal, capa e progresso."""
    player = firefox_player(is_youtube)
    if not player:  # aba fechada: encerra a presenca em vez de publicar generico
        return None
    meta = mpris_meta(player)
    channel = meta.get("xesam:artist")
    channel = channel[0] if isinstance(channel, list) and channel else channel
    title = meta.get("xesam:title") or "YouTube"

    extra = {}
    url = meta.get("xesam:url") or ""
    video = re.search(r"[?&]v=([\w-]{11})", url)
    if video:  # miniatura do proprio video como icone
        extra["large_image"] = f"https://i.ytimg.com/vi/{video.group(1)}/hqdefault.jpg"
        extra["large_text"] = title[:128]
        extra["buttons"] = [{"label": "Assistir no YouTube",
                             "url": f"https://youtu.be/{video.group(1)}"}]

    if mpris("PlaybackStatus", player) != "Playing":
        anchored_position(title, mpris_position(player), playing=False)
        return f"{channel} (pausado)" if channel else "Pausado", extra or None

    length = track_length(title, meta, video.group(1) if video else None)
    position = anchored_position(title, mpris_position(player))
    extra["timestamps"] = media_timestamps(title, position, length)
    text = channel or "YouTube"
    if not extra["timestamps"] and length:  # sem barra, ao menos a duracao
        text = f"{text}  ·  {duration_label(length)}"
    return text, extra


_ART_CACHE = {}


def ytm_art(meta):
    """Capa da musica. O Firefox nao publica mpris:artUrl, entao: miniatura do
    video quando a URL traz o id, senao a capa do album pela busca do iTunes."""
    url = meta.get("xesam:url") or ""
    video = re.search(r"[?&]v=([\w-]{11})", url)
    if video:
        return f"https://i.ytimg.com/vi/{video.group(1)}/hqdefault.jpg"

    artist = meta.get("xesam:artist")
    artist = artist[0] if isinstance(artist, list) and artist else artist
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
    player = ytm_player()
    if not player:  # aba fechada: encerra a presenca em vez de publicar generico
        return None
    meta = mpris_meta(player)
    artist = meta.get("xesam:artist")
    artist = artist[0] if isinstance(artist, list) and artist else artist
    title = meta.get("xesam:title") or "YouTube Music"
    label = f"{title} — {artist}" if artist else title

    extra = {}
    art = ytm_art(meta)
    if art:  # capa da musica no lugar do icone do app
        extra["large_image"] = art
        extra["large_text"] = meta.get("xesam:album") or title
    url = meta.get("xesam:url")
    if url:
        extra["buttons"] = [{"label": "Ouvir no YouTube Music", "url": url}]
    if artist:  # o MPRIS nao traz o canal do artista, entao vai pela busca
        query = urllib.parse.quote(artist)
        extra.setdefault("buttons", []).append(
            {"label": f"Artista: {artist}"[:31],
             "url": f"https://music.youtube.com/search?q={query}"})

    if mpris("PlaybackStatus", player) != "Playing":
        anchored_position(title, mpris_position(player), playing=False)
        return f"{label} (pausado)", extra or None

    length = track_length(title, meta)
    position = anchored_position(title, mpris_position(player))
    extra["timestamps"] = media_timestamps(title, position, length)
    if not extra["timestamps"] and length:
        label = f"{label}  ·  {duration_label(length)}"
    return label, extra


def stremio_state():
    """Titulo em reproducao e quanto falta para acabar, via MPRIS."""
    status = mpris("PlaybackStatus")
    if status is None:  # o Stremio fechou
        return None
    meta = mpris("Metadata") or {}

    def field(key):
        v = meta.get(key)
        return v["data"] if isinstance(v, dict) else v

    title = field("xesam:title")
    if not title:
        return ("Navegando" if status == "Stopped" else "Assistindo"), None
    artist = field("xesam:artist")  # serie, quando for episodio
    series = artist[0] if isinstance(artist, list) and artist else artist
    label = f"{series} — {title}" if series and series != title else title
    if status != "Playing":
        return f"{label} (pausado)", None

    # o Stremio nao publica mpris:length nem Position, entao o Discord so pode
    # contar o tempo desde o inicio deste titulo (o timer reinicia a cada troca)
    position, length = mpris("Position"), field("mpris:length")
    if position and length:
        now = time.time()
        return label, {"timestamps": {
            "start": int(now - position / 1_000_000),
            "end": int(now + (length - position) / 1_000_000),
        }}
    return label, None


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


def steam_libraries():
    """Todas as bibliotecas Steam, inclusive em outros discos."""
    roots = [pathlib.Path.home() / ".local/share/Steam",
             pathlib.Path.home() / ".steam/steam"]
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
    log = l4d2_log()
    if not log:
        return "Jogando", None
    try:
        with log.open("rb") as f:
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
    campaign = L4D2_MAPS.get(re.match(r"c\d+", slug).group() if re.match(r"c\d+", slug) else "")
    m = re.search(r"m(\d+)", slug)
    if campaign:
        return (f"{campaign} — capítulo {m.group(1)}" if m else campaign), None
    return slug, None


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


def mc_ping(host, port):
    """Server List Ping: (online, max) de jogadores, ou None."""
    try:
        with socket.create_connection((host, port), timeout=4) as s:
            h = (varint(0) + varint(765) + varint(len(host)) + host.encode()
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
            return players["online"], players["max"]
    except (OSError, ValueError, KeyError, ConnectionError):
        return None


def mc_state():
    """Servidor em que o jogo esta conectado (com lotacao), ou singleplayer."""
    pid = mc_pid()
    if not pid:
        return "Jogando", None
    instance = mc_instance(pid)
    peers = mc_peers(pid)
    if not peers:
        return (f"Singleplayer — {instance}" if instance else "Singleplayer"), None

    # casa o IP conectado com um host salvo em servers.dat; senao mostra o IP
    ip, port = peers[0]
    name = ip
    for host in mc_server_names(instance):
        candidate = host.split(":")[0]
        try:
            ips = {i[4][0] for i in socket.getaddrinfo(candidate, None)}
        except OSError:
            continue
        if any(p in ips for p, _ in peers):
            name = candidate
            break
    else:
        if port != "25565":
            name = f"{ip}:{port}"

    # a lotacao vai so no party; o Discord ja a escreve ao lado do state
    return f"em {name}", mc_ping(name.split(":")[0], int(port))

# nome do processo -> presenca. O client_id vem da base de jogos detectaveis
# do Discord (https://discord.com/api/v9/applications/detectable).
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
    # Toca no Firefox, entao a presenca segue o MPRIS, nao um processo proprio.
    # Precisa de um app criado em discord.com/developers/applications.
    "youtube-music": {
        "client_id": "1537975657735262368",
        "details": "YouTube Music",
        "state": ytm_state,
        "large_text": "YouTube Music",
        "large_image": ("https://cdn.discordapp.com/app-icons/1537975657735262368/"
                        "1637a3eccd8d9c22acf127e814bf088b.png"),
        "running": ytm_running,
        "type": 2,  # Ouvindo
    },
    # Videos comuns do YouTube, tambem no Firefox e tambem via MPRIS. Precisa de
    # um app proprio: "YouTube" nao existe na base detectavel do Discord.
    "youtube": {
        "client_id": "1538427046760423474",
        "details": yt_title,   # titulo do video
        # usado quando o video nao expoe id na URL (live, shorts, embed)
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
        "running": lambda: (any("libexec/stremio/stremio" in line
                                for line in process_cmdlines())
                            and not media_idle("org.mpris.MediaPlayer2.Stremio")),
        "client_id": "1536170931540463647",
        "details": "Stremio",
        "state": stremio_state,
        "large_text": "Stremio",
        # nao ha Art Assets no app; usa o proprio icone dele
        "large_image": ("https://cdn.discordapp.com/app-icons/1536170931540463647/"
                        "2c1838e508bcbe74dbafaaef7180be93.png"),
        "type": 3,  # Assistindo
    },
    # Roblox roda pelo Sober (flatpak): os processos aparecem com o argumento
    # roblox-player:..., e a experiencia atual sai do log do Sober
    "roblox-player:": {
        "client_id": "363445589247131668",
        "details": roblox_title,
        "state": roblox_state,
        "large_text": "Roblox",
    },
    "gta-sa.exe": {
        "client_id": "363447565905166336",
        "details": "Grand Theft Auto: San Andreas",
        "state": "Jogando",
        "large_text": "Grand Theft Auto: San Andreas",
    },
    # casa tanto o binario nativo (hl2_linux -game left4dead2) quanto o .exe sob Proton
    "left4dead2": {
        "client_id": "356954277803065354",
        "details": "Left 4 Dead 2",
        "state": l4d2_state,
        "large_text": "Left 4 Dead 2",
    },
    "PrismLauncher/instances": {
        "client_id": "1410791091501928458",
        # o Discord ja mostra "Minecraft: Java Edition" como titulo; details repete
        "details": lambda: mc_instance(mc_pid()) or "Minecraft",
        "state": mc_state,  # servidor conectado, atualizado a cada poll
        "large_text": "Minecraft",
    },
}

POLL = 15
IPC_TIMEOUT = 15

# Avisa as sessoes que e hora de sair. Sem isso o processo morria com as
# conexoes abertas e o cliente continuava exibindo a ultima atividade: quem
# publicou tem de ser quem apaga, na mesma conexao.
STOPPING = threading.Event()

# entradas renomeadas: o config antigo do usuario continua valendo
LEGACY_KEYS = {"libexec/stremio/stremio": "stremio"}

CONFIG = pathlib.Path(
    os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
) / "discord-game-presence.json"


def load_config():
    """Aplica ~/.config/discord-game-presence.json sobre a tabela padrao.

    Formato: {"games": {"<processo>": {"client_id": "...", "details": "...",
    "state": "...", "large_text": "...", "large_image": "..."}}}. Entradas
    novas sao adicionadas; as existentes tem so os campos citados trocados.
    Um client_id vazio, ou "enabled": false, desliga a entrada.

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
        if proc in GAMES:
            GAMES[proc].update(fields)
        elif fields.get("client_id"):
            GAMES[proc] = {"details": proc, "state": "Jogando",
                           "large_text": proc, **fields}


def ipc_path():
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    for i in range(10):
        p = os.path.join(base, f"discord-ipc-{i}")
        if os.path.exists(p):
            return p
    return None


def send(sock, op, payload):
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack("<II", op, len(data)) + data)
    header = sock.recv(8)
    if len(header) < 8:
        raise ConnectionError("IPC fechou")
    _, length = struct.unpack("<II", header)
    body = b""
    while len(body) < length:
        body += sock.recv(length - len(body))
    return json.loads(body)


SHELL_NAMES = {"bash", "sh", "zsh", "dash", "fish", "ksh", "csh", "tcsh"}
_PROC_CACHE = {"stamp": 0.0, "rows": []}


def process_cmdlines():
    """Linhas de comando dos processos, com um cache de dois segundos.

    Uma invocacao de shell (`bash -c ...`) fica de fora: o padrao procurado
    aparece na linha de comando de quem esta procurando, e a busca acabava
    encontrando a si mesma - um falso positivo que so some quando o terminal
    fecha.
    """
    now = time.monotonic()
    if now - _PROC_CACHE["stamp"] < 2:
        return _PROC_CACHE["rows"]

    rows, mine = [], os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == mine:
            continue
        try:
            raw = pathlib.Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue  # o processo morreu no meio da leitura
        argv = raw.decode("utf-8", "replace").split("\0")
        if not argv or not argv[0]:
            continue  # thread do kernel
        if (os.path.basename(argv[0]) in SHELL_NAMES
                and any(a.startswith("-") and "c" in a for a in argv[1:2])):
            continue
        rows.append(" ".join(argv).strip())

    _PROC_CACHE.update(stamp=now, rows=rows)
    return rows


def running(proc):
    # a chave e um trecho da linha de comando, ou uma funcao quando o processo
    # sozinho nao basta (uma aba de navegador, por exemplo)
    if callable(proc):
        return proc()
    return any(proc in line for line in process_cmdlines())


def same_activity(old, new):
    """Compara duas atividades ignorando variacao pequena nos tempos.

    A comparacao e sobre a atividade inteira - titulo, texto, imagem, botoes -
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


def clear_presence(client_id):
    """Apaga do Discord a presenca desse app.

    Necessario porque o cliente mantem a ultima atividade publicada mesmo
    depois que a conexao morre: se o daemon for morto (um `systemctl restart`,
    por exemplo), a presenca fica pendurada ate alguem mandar apagar.
    """
    path = ipc_path()
    if not path:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(IPC_TIMEOUT)
        sock.connect(path)
        try:
            send(sock, 0, {"v": 1, "client_id": client_id})
            send(sock, 1, {"cmd": "SET_ACTIVITY",
                           "args": {"pid": os.getpid(), "activity": None},
                           "nonce": str(time.time())})
        finally:
            sock.close()
    except (OSError, ValueError):
        pass  # Discord fechado: nao ha presenca pendurada para apagar


def session(proc, game):
    """Publica a presenca de um app, do inicio ao fim dele."""
    path = ipc_path()
    if not path:
        return

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(IPC_TIMEOUT)  # sem isso um Discord mudo travaria a thread
    sock.connect(path)
    send(sock, 0, {"v": 1, "client_id": game["client_id"]})
    start = int(time.time())

    def current_state():
        s = game["state"]
        if not callable(s):
            return (s, None)
        return s()  # None significa "nao ha mais nada tocando"

    def build(state):
        # o segundo item e o tamanho da party (lista) ou campos extras (dict:
        # "timestamps", "buttons", "large_image", "large_text")
        text, extra = state
        details = game["details"]
        fields = extra if isinstance(extra, dict) else {}
        activity = {
            "type": game.get("type", 0),
            "details": details() if callable(details) else details,
            "state": text,
            # jogo sem tempo proprio conta desde que a sessao abriu; midia sem
            # posicao conhecida manda None e fica sem tempo nenhum
            "timestamps": fields["timestamps"] if "timestamps" in fields
                          else {"start": start},
            "assets": {
                # nome de um Art Asset do app, ou uma URL de imagem; o state
                # pode trocar a arte a cada faixa (capa da musica, por exemplo)
                "large_image": fields.get("large_image") or game.get("large_image", "game"),
                "large_text": fields.get("large_text") or game["large_text"],
            },
        }
        if not activity["timestamps"]:
            del activity["timestamps"]
        if fields.get("buttons"):
            activity["buttons"] = fields["buttons"][:2]  # o Discord aceita 2
        if extra and not isinstance(extra, dict):  # "(45 of 1000)" ao lado do state
            activity["party"] = {"id": game["client_id"], "size": list(extra)}
        return activity

    def publish(activity):
        send(sock, 1, {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(time.time()),
        })
        log(f"publicou {game['large_text']}: "
            f"{activity['details']} | {activity['state']}")

    published = None
    try:
        while running(proc) and not STOPPING.is_set():
            state = current_state()
            if state is None:
                break  # o app fechou ou parou; a presenca sai no finally
            activity = build(state)
            if not same_activity(published, activity):
                publish(activity)
                published = activity
            if STOPPING.wait(POLL):
                break
    finally:
        try:
            send(sock, 1, {"cmd": "SET_ACTIVITY",
                           "args": {"pid": os.getpid(), "activity": None},
                           "nonce": str(time.time())})
            log(f"apagou {game['large_text']}")
        except (OSError, ValueError) as error:
            log(f"nao consegui apagar {game['large_text']}: {error}")
        sock.close()


def watch(proc, game):
    """Espera o jogo abrir, publica a presenca, repete."""
    alive = game.get("running", proc)
    while not STOPPING.is_set():
        if running(alive):
            try:
                session(alive, game)
            except (OSError, ConnectionError, ValueError) as error:
                log(f"{proc}: sessao caiu ({error})")
        if STOPPING.wait(POLL):
            return


def main():
    load_config()
    active = [(proc, game) for proc, game in GAMES.items()
              if game.get("client_id") and game.get("enabled", True)]

    # todo o catalogo, nao so o que esta ligado: desligar um app tambem tem de
    # apagar a presenca que ele deixou
    ids = list(dict.fromkeys(game["client_id"] for game in GAMES.values()
                             if game.get("client_id")))
    for client_id in ids:  # restos de uma execucao anterior
        clear_presence(client_id)

    threads = [threading.Thread(target=watch, args=(proc, game), daemon=True)
               for proc, game in active]

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

    log(f"vigiando {len(threads)} app(s): "
        + ", ".join(sorted(game["large_text"] for _, game in active)))
    for thread in threads:
        thread.start()
    while not STOPPING.wait(3600):
        pass


if __name__ == "__main__":
    main()

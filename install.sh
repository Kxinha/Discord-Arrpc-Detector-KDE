#!/usr/bin/env bash
# Instalador do discord-game-presence: Vesktop nativo, daemon de Rich Presence
# e o icone de bandeja que controla tudo.
#
#   curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Discord-Arrpc-Detector-KDE/main/install.sh | bash
#
# Opcoes, sempre do lado do bash:  curl ... | SKIP_VESKTOP=1 bash
#   SKIP_VESKTOP=1      nao instala o Vesktop (ja usa outro cliente com IPC)
#   SKIP_TRAY=1         nao liga o icone de bandeja
#   DGP_VERSION=1.4.0   instala essa versao em vez da ultima release
#   DGP_MIRROR=gitlab   baixa a release do espelho no GitLab
#   REPO_RAW=<url raw>  pega o codigo de uma branch, sem release nem sha256
#
# Pela internet, os arquivos vem da ultima release do GitHub e so sao
# instalados se o sha256 bater com o SHA256SUMS dela. Rodado de um clone, usa
# os arquivos do clone. Rodar de novo atualiza e reinicia o que ja estava
# instalado, guardando a versao anterior.
#
# Suporta Arch, Debian/Ubuntu, Fedora, openSUSE e, como reserva, qualquer distro
# com systemd (Vesktop vai para /opt a partir do tarball oficial).
set -euo pipefail

REPO="gabrielmf1998/Discord-Arrpc-Detector-KDE"
GITLAB="https://gitlab.com/gabriel17166/arrpc-personalizado-pessoal-linux"
DGP_VERSION="${DGP_VERSION:-latest}"
DGP_MIRROR="${DGP_MIRROR:-github}"
case "$DGP_MIRROR:$DGP_VERSION" in
    gitlab:latest) RELEASE_URL="$GITLAB/-/releases/permalink/latest/downloads" ;;
    gitlab:*)      RELEASE_URL="$GITLAB/-/releases/v${DGP_VERSION#v}/downloads" ;;
    *:latest)      RELEASE_URL="https://github.com/$REPO/releases/latest/download" ;;
    *)             RELEASE_URL="https://github.com/$REPO/releases/download/v${DGP_VERSION#v}" ;;
esac
RELEASE_URL="${DGP_RELEASE_URL:-$RELEASE_URL}"
REPO_RAW="${REPO_RAW:-}"
# token so e preciso se o repositorio voltar a ser privado
GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
SKIP_VESKTOP="${SKIP_VESKTOP:-0}"
SKIP_TRAY="${SKIP_TRAY:-0}"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "rode como usuario normal, nao root (o servico e --user)"

fetch() {
    # baixa uma URL do repositorio, com token quando ele for privado
    if [ -n "$GITHUB_TOKEN" ]; then
        curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" "$1" -o "$2"
    else
        curl -fsSL "$1" -o "$2"
    fi
}

detect_distro() {
    [ -r /etc/os-release ] || { echo unknown; return; }
    . /etc/os-release
    case "${ID}${ID_LIKE:-}" in
        *arch*|*cachyos*|*manjaro*|*endeavour*) echo arch ;;
        *debian*|*ubuntu*|*mint*|*pop*)         echo debian ;;
        *fedora*|*rhel*|*nobara*)               echo fedora ;;
        *suse*)                                 echo suse ;;
        *)                                      echo unknown ;;
    esac
}

# o que da lista ainda nao esta instalado: sem nada faltando, nem pede senha
missing_pkgs() {
    local distro="$1"; shift
    case "$distro" in
        arch)   pacman -T "$@" 2>/dev/null || true ;;
        debian) local p
                for p in "$@"; do
                    dpkg-query -W -f='${Status}\n' "$p" 2>/dev/null \
                        | grep -q "ok installed" || echo "$p"
                done ;;
        fedora|suse)
                local p
                for p in "$@"; do rpm -q --whatprovides "$p" >/dev/null 2>&1 || echo "$p"; done ;;
        *)      printf '%s\n' "$@" ;;
    esac
}

pkg_install() {
    local distro="$1"; shift
    [ $# -gt 0 ] || return 0
    local missing
    mapfile -t missing < <(missing_pkgs "$distro" "$@")
    if [ ${#missing[@]} -eq 0 ]; then
        msg "dependencias ja instaladas"
        return 0
    fi
    set -- "${missing[@]}"
    msg "instalando dependencias: $*"
    case "$distro" in
        arch)   sudo pacman -S --needed --noconfirm "$@" ;;
        debian) sudo apt-get update -qq && sudo apt-get install -y "$@" ;;
        fedora) sudo dnf install -y "$@" ;;
        suse)   sudo zypper --non-interactive install "$@" ;;
        *)      warn "distro desconhecida: instale manualmente: $*" ;;
    esac
}

# ---------------------------------------------------------------- dependencias
# O tray e GTK3 + AppIndicator (nada de Qt), entao precisa do PyGObject, do
# binding do indicador e do librsvg para desenhar os icones em SVG.
install_deps() {
    local distro="$1"
    case "$distro" in
        arch)   pkg_install arch python iproute2 systemd curl \
                    python-gobject gtk3 libayatana-appindicator librsvg ;;
        debian) pkg_install debian python3 iproute2 curl \
                    python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
                    librsvg2-common ;;
        fedora) pkg_install fedora python3 iproute curl \
                    python3-gobject gtk3 libappindicator-gtk3 librsvg2 ;;
        suse)   pkg_install suse python3 iproute2 curl \
                    python3-gobject gtk3 libayatana-appindicator3-1 \
                    typelib-1_0-AyatanaAppIndicator3-0_1 ;;
        *)      warn "instale manualmente: python3, iproute2 (ss), curl, PyGObject, GTK3, AppIndicator, librsvg" ;;
    esac
    command -v busctl >/dev/null || warn "busctl ausente: Stremio e YouTube Music nao funcionarao"
}

# ------------------------------------------------------------------- Vesktop
# O Discord oficial nao expoe o socket de IPC em todos os empacotamentos, e o
# flatpak isola o /run do usuario. Vesktop nativo cria discord-ipc-0 no
# XDG_RUNTIME_DIR, que e exatamente o que o daemon precisa.
vesktop_installed() {
    command -v vesktop >/dev/null || [ -x /opt/Vesktop/vesktop ]
}

install_vesktop() {
    local distro="$1"
    if vesktop_installed; then
        msg "Vesktop ja instalado"
        return
    fi
    local api="https://api.github.com/repos/Vencord/Vesktop/releases/latest"
    local tmp="$WORK/vesktop"; mkdir -p "$tmp"

    case "$distro" in
        arch)
            # so os repositorios oficiais; sem pacote la, vai o tarball (nada de AUR)
            if pacman -Si vesktop >/dev/null 2>&1; then
                sudo pacman -S --needed --noconfirm vesktop && return
            fi
            ;;
        debian)
            local url; url=$(curl -fsSL "$api" | grep -o 'https://[^"]*amd64\.deb' | head -1)
            [ -n "$url" ] || die "nao achei o .deb do Vesktop"
            curl -fL "$url" -o "$tmp/vesktop.deb"
            sudo apt-get install -y "$tmp/vesktop.deb" && return
            ;;
        fedora|suse)
            local url; url=$(curl -fsSL "$api" | grep -o 'https://[^"]*x86_64\.rpm' | head -1)
            [ -n "$url" ] || die "nao achei o .rpm do Vesktop"
            curl -fL "$url" -o "$tmp/vesktop.rpm"
            if [ "$distro" = fedora ]; then
                sudo dnf install -y "$tmp/vesktop.rpm" && return
            else
                sudo zypper --non-interactive install --allow-unsigned-rpm "$tmp/vesktop.rpm" && return
            fi
            ;;
    esac

    # reserva universal: tarball oficial em /opt
    msg "instalando Vesktop em /opt a partir do tarball"
    # o tarball x86_64 nao tem sufixo de arquitetura (arm64 tem)
    local url; url=$(curl -fsSL "$api" | grep -oE 'https://[^"]*/vesktop-[0-9.]+\.tar\.gz' | head -1)
    [ -n "$url" ] || die "nao achei o tarball do Vesktop"
    curl -fL "$url" -o "$tmp/vesktop.tar.gz"
    sudo mkdir -p /opt/Vesktop
    sudo tar -xzf "$tmp/vesktop.tar.gz" -C /opt/Vesktop --strip-components=1
    sudo ln -sf /opt/Vesktop/vesktop /usr/local/bin/vesktop
}

# ------------------------------------------------------------ daemon + tray
SCRIPTS=(discord-game-presence.py discord-presence-tray.py)
UNITS=(discord-game-presence.service discord-game-presence-tray.service)

# Junta os quatro arquivos em $1: do clone, de uma branch ou da release.
gather_files() {
    local dest="$1" here="" f
    # "curl | bash" nao tem arquivo de script; "bash install.sh" no clone tem
    # (com o script vindo do pipe, BASH_SOURCE e so "bash", que nao e este arquivo)
    local src="${BASH_SOURCE[0]:-}"
    if [ -f "$src" ] && grep -q '^gather_files()' "$src" 2>/dev/null; then
        here="$(cd "$(dirname "$src")" && pwd)"
    fi
    if [ -n "$here" ] && [ -f "$here/discord-game-presence.py" ]; then
        msg "usando os arquivos do clone em $here"
        cp "$here/${SCRIPTS[0]}" "$here/${SCRIPTS[1]}" "$dest/"
        cp "$here/systemd/${UNITS[0]}" "$here/systemd/${UNITS[1]}" "$dest/"
    elif [ -n "$REPO_RAW" ]; then
        msg "baixando de $REPO_RAW (sem conferir sha256)"
        for f in "${SCRIPTS[@]}"; do fetch "$REPO_RAW/$f" "$dest/$f" || die "falhou baixar $f"; done
        for f in "${UNITS[@]}"; do fetch "$REPO_RAW/systemd/$f" "$dest/$f" || die "falhou baixar $f"; done
    else
        msg "baixando a release ($DGP_VERSION) de $RELEASE_URL"
        for f in "${SCRIPTS[@]}" "${UNITS[@]}" SHA256SUMS; do
            fetch "$RELEASE_URL/$f" "$dest/$f" || die "falhou baixar $f de $RELEASE_URL"
        done
        for f in "${SCRIPTS[@]}" "${UNITS[@]}"; do
            grep -q "  $f\$" "$dest/SHA256SUMS" || die "$f nao esta no SHA256SUMS da release"
        done
        ( cd "$dest" && sha256sum --quiet --ignore-missing -c SHA256SUMS ) \
            || die "sha256 nao confere: nada foi instalado"
        msg "sha256 conferido"
    fi
    python3 "$dest/${SCRIPTS[0]}" --version >/dev/null 2>&1 \
        || die "o daemon baixado nao roda com este python3"
}

# A versao anterior vai para o mesmo lugar que a atualizacao pela bandeja usa.
backup_old() {
    local old="$BIN_DIR/${SCRIPTS[0]}" version f
    [ -f "$old" ] || return 0
    version="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$old")"
    local dir="$HOME/.local/share/discord-game-presence/backup/${version:-anterior}"
    mkdir -p "$dir"
    for f in "${SCRIPTS[@]}"; do [ -f "$BIN_DIR/$f" ] && cp -p "$BIN_DIR/$f" "$dir/"; done
    for f in "${UNITS[@]}"; do [ -f "$UNIT_DIR/$f" ] && cp -p "$UNIT_DIR/$f" "$dir/"; done
    msg "versao anterior (${version:-?}) guardada em $dir"
}

install_daemon() {
    mkdir -p "$BIN_DIR" "$UNIT_DIR"
    local tmp="$WORK/files"; mkdir -p "$tmp"
    gather_files "$tmp"
    backup_old
    install -m 755 "$tmp/${SCRIPTS[0]}" "$tmp/${SCRIPTS[1]}" "$BIN_DIR/"
    install -m 644 "$tmp/${UNITS[0]}" "$tmp/${UNITS[1]}" "$UNIT_DIR/"
    msg "instalado: $(python3 "$BIN_DIR/${SCRIPTS[0]}" --version) em $BIN_DIR"

    # restart, e nao so --now: rodar de novo tem de trocar o que ja estava rodando
    systemctl --user daemon-reload
    systemctl --user enable discord-game-presence.service
    systemctl --user restart discord-game-presence.service
    if [ "$SKIP_TRAY" = 1 ]; then
        msg "tray nao habilitado (SKIP_TRAY=1)"
    else
        systemctl --user enable discord-game-presence-tray.service
        systemctl --user restart discord-game-presence-tray.service \
            || warn "o tray nao subiu agora; ele inicia junto com a sessao grafica"
    fi
}

# ------------------------------------------------------------------- L4D2 map
# O L4D2 nao publica o mapa em lugar nenhum; a unica fonte e o log do console.
setup_l4d2() {
    local lib game cfg
    for lib in "$HOME/.local/share/Steam" "$HOME/.steam/steam" \
               /run/media/*/*/SteamLibrary /mnt/*/SteamLibrary; do
        game="$lib/steamapps/common/Left 4 Dead 2/left4dead2"
        [ -d "$game" ] || continue
        cfg="$game/cfg/autoexec.cfg"
        if ! grep -qs con_logfile "$cfg" 2>/dev/null; then
            printf 'con_logfile "console.log"\n' >> "$cfg"
            msg "L4D2: con_logfile ativado em $cfg"
        fi
        return
    done
}

main() {
    # um trap RETURN dentro das funcoes continuava armado e, com set -u,
    # derrubava o fim do script; uma pasta so, apagada na saida, resolve
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    local distro; distro="$(detect_distro)"
    msg "distro detectada: $distro"
    install_deps "$distro"
    [ "$SKIP_VESKTOP" = 1 ] || install_vesktop "$distro"
    install_daemon
    setup_l4d2

    msg "pronto. servico: systemctl --user status discord-game-presence"
    msg "o icone na bandeja controla tudo: apps, aparencia e inicio automatico"
    cat <<'EOF'

Proximos passos:
  * Stremio e YouTube Music precisam de um Application ID proprio, criado em
    https://discord.com/developers/applications (o Discord nao os reconhece).
    Depois, coloque os ids em ~/.config/discord-game-presence.json:

      {"games": {"youtube-music": {"client_id": "SEU_ID"},
                 "libexec/stremio/stremio": {"client_id": "SEU_ID"}}}

  * Reinicie o daemon apos editar:
      systemctl --user restart discord-game-presence
    (so opcoes de exibicao: systemctl --user reload discord-game-presence)

  * Para atualizar, rode o mesmo comando de novo, ou use o item Updates
    do icone de bandeja.
EOF
}

main "$@"

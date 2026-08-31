#!/usr/bin/env bash
# Instalador do discord-game-presence: Vesktop nativo, daemon de Rich Presence
# e o icone de bandeja que controla tudo.
#
#   curl -fsSL <URL_RAW>/install.sh | bash
#
# Suporta Arch, Debian/Ubuntu, Fedora, openSUSE e, como reserva, qualquer distro
# com systemd (Vesktop vai para /opt a partir do tarball oficial).
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/gabrielmf1998/Discord-Arrpc-Detector-KDE/main}"
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

pkg_install() {
    local distro="$1"; shift
    [ $# -gt 0 ] || return 0
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
    local tmp; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN

    case "$distro" in
        arch)
            sudo pacman -S --needed --noconfirm vesktop && return
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
install_daemon() {
    mkdir -p "$BIN_DIR" "$UNIT_DIR"
    local here; here="$(dirname "$0")"

    if [ -f "$here/discord-game-presence.py" ]; then
        install -m 755 "$here/discord-game-presence.py" "$BIN_DIR/discord-game-presence.py"
        install -m 755 "$here/discord-presence-tray.py" "$BIN_DIR/discord-presence-tray.py"
        install -m 644 "$here/systemd/discord-game-presence.service" "$UNIT_DIR/"
        install -m 644 "$here/systemd/discord-game-presence-tray.service" "$UNIT_DIR/"
    else  # instalacao via curl | bash: baixa do repositorio
        fetch "$REPO_RAW/discord-game-presence.py" "$BIN_DIR/discord-game-presence.py" \
            || die "falhou baixar o daemon"
        fetch "$REPO_RAW/discord-presence-tray.py" "$BIN_DIR/discord-presence-tray.py" \
            || die "falhou baixar o tray"
        chmod 755 "$BIN_DIR/discord-game-presence.py" "$BIN_DIR/discord-presence-tray.py"
        for unit in discord-game-presence.service discord-game-presence-tray.service; do
            fetch "$REPO_RAW/systemd/$unit" "$UNIT_DIR/$unit" || die "falhou baixar $unit"
        done
    fi

    systemctl --user daemon-reload
    systemctl --user enable --now discord-game-presence.service
    if [ "$SKIP_TRAY" = 1 ]; then
        msg "tray nao habilitado (SKIP_TRAY=1)"
    else
        systemctl --user enable --now discord-game-presence-tray.service \
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
EOF
}

main "$@"

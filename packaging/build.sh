#!/usr/bin/env bash
# Constroi os pacotes em dist/: .rpm, .deb, .pkg.tar.zst (Arch), .AppImage e
# os arquivos soltos da instalacao por script, mais o SHA256SUMS de tudo.
#
#   packaging/build.sh            constroi tudo o que a maquina permitir
#   packaging/build.sh rpm deb    so os formatos pedidos
#
# Tudo o que sai em dist/ vai anexado a release do GitHub: a atualizacao pela
# bandeja baixa daqui o arquivo da instalacao de cada um (os scripts e units,
# ou o pacote) e confere o sha256 antes de trocar qualquer coisa.
#
# Nada aqui precisa de root, e a maquina que constroi nao precisa ser a mesma
# distro do pacote: rpm/deb/arch sao so arvores de arquivos com metadados.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# a versao mora no daemon (discord-game-presence --version); VERSION=x sobrescreve
VERSION="${VERSION:-$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$ROOT/discord-game-presence.py")}"
RELEASE="${RELEASE:-1}"
PKGNAME="discord-game-presence"
MAINTAINER="kxinha <gabriel17166@gmail.com>"
URL="https://github.com/gabrielmf1998/Discord-Arrpc-Detector-KDE"
SUMMARY="Rich Presence for games and apps Discord ignores on Linux"

DIST="$ROOT/dist"
WORK="$ROOT/.build"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

# Arvore /usr comum a rpm, deb e arch. O daemon e o tray perdem o .py para
# virarem comandos, e as units apontam para /usr/bin em vez de ~/.local/bin.
stage_tree() {
    local dest="$1"
    install -Dm755 "$ROOT/discord-game-presence.py"  "$dest/usr/bin/$PKGNAME"
    install -Dm755 "$ROOT/discord-presence-tray.py"  "$dest/usr/bin/discord-presence-tray"
    install -Dm644 "$ROOT/packaging/files/$PKGNAME.service"      "$dest/usr/lib/systemd/user/$PKGNAME.service"
    install -Dm644 "$ROOT/packaging/files/$PKGNAME-tray.service" "$dest/usr/lib/systemd/user/$PKGNAME-tray.service"
    install -Dm644 "$ROOT/packaging/files/discord-presence-tray.desktop" \
        "$dest/usr/share/applications/discord-presence-tray.desktop"
    install -Dm644 "$ROOT/packaging/files/$PKGNAME.svg" \
        "$dest/usr/share/icons/hicolor/scalable/apps/$PKGNAME.svg"
    install -Dm644 "$ROOT/README.md"    "$dest/usr/share/doc/$PKGNAME/README.md"
    install -Dm644 "$ROOT/INSTALAR.txt" "$dest/usr/share/doc/$PKGNAME/INSTALAR.txt"
    install -Dm644 "$ROOT/docs/config.exemplo.json" \
        "$dest/usr/share/doc/$PKGNAME/config.exemplo.json"
}

post_install_note() {
    cat <<'EOF'

Discord Presence installed. Enable it for your user (no root):

    systemctl --user enable --now discord-game-presence.service
    systemctl --user enable --now discord-game-presence-tray.service

The tray icon is the interface: apps, appearance and autostart live there.
EOF
}

# ------------------------------------------------------------------- rpm
build_rpm() {
    command -v rpmbuild >/dev/null || { warn "rpmbuild ausente: pulando .rpm"; return; }
    msg "construindo .rpm"
    local top="$WORK/rpm"
    rm -rf "$top"; mkdir -p "$top"/{BUILD,RPMS,SOURCES,SPECS,BUILDROOT}
    local payload="$WORK/payload-rpm"
    rm -rf "$payload"; stage_tree "$payload"
    tar -C "$payload" -czf "$top/SOURCES/$PKGNAME-$VERSION.tar.gz" .

    sed -e "s/@VERSION@/$VERSION/g" -e "s/@RELEASE@/$RELEASE/g" \
        "$ROOT/packaging/rpm/$PKGNAME.spec.in" > "$top/SPECS/$PKGNAME.spec"

    rpmbuild --define "_topdir $top" --define "dist %{nil}" \
        -bb "$top/SPECS/$PKGNAME.spec" >/dev/null
    mkdir -p "$DIST"
    find "$top/RPMS" -name '*.rpm' -exec cp {} "$DIST/" \;
}

# ------------------------------------------------------------------- deb
build_deb() {
    command -v dpkg-deb >/dev/null || { warn "dpkg-deb ausente: pulando .deb"; return; }
    msg "construindo .deb"
    local payload="$WORK/payload-deb"
    rm -rf "$payload"; stage_tree "$payload"
    mkdir -p "$payload/DEBIAN"

    cat > "$payload/DEBIAN/control" <<EOF
Package: $PKGNAME
Version: $VERSION-$RELEASE
Section: utils
Priority: optional
Architecture: all
Depends: python3, python3-gi, gir1.2-gtk-3.0, iproute2, systemd
Recommends: gir1.2-ayatanaappindicator3-0.1, librsvg2-common
Maintainer: $MAINTAINER
Homepage: $URL
Description: $SUMMARY
 Discord only detects games listed with a Windows or macOS executable, so
 native Linux games, anything running under Proton and media apps are never
 picked up. This daemon watches the processes itself and speaks Discord's
 IPC protocol directly.
 .
 A GTK3 tray icon (no Qt, no windows) controls which apps Discord can see,
 the icon look, and autostart.
EOF

    cat > "$payload/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
echo ""
echo "Discord Presence installed. Enable it for your user (no root):"
echo "    systemctl --user enable --now discord-game-presence.service"
echo "    systemctl --user enable --now discord-game-presence-tray.service"
EOF
    chmod 755 "$payload/DEBIAN/postinst"

    mkdir -p "$DIST"
    dpkg-deb --root-owner-group --build "$payload" \
        "$DIST/${PKGNAME}_${VERSION}-${RELEASE}_all.deb" >/dev/null
}

# ------------------------------------------------------------------ arch
# Sem makepkg a maquina nao monta o pacote sozinha, mas um pacote do pacman e
# um tarball com .PKGINFO - da para escrever a mao, e o PKGBUILD fica no repo
# para quem quiser construir do jeito idiomatico.
build_arch() {
    command -v bsdtar >/dev/null || { warn "bsdtar ausente: pulando .pkg.tar.zst"; return; }
    msg "construindo .pkg.tar.zst"
    local payload="$WORK/payload-arch"
    rm -rf "$payload"; stage_tree "$payload"

    local size; size=$(du -sb "$payload" | cut -f1)
    cat > "$payload/.PKGINFO" <<EOF
pkgname = $PKGNAME
pkgbase = $PKGNAME
pkgver = $VERSION-$RELEASE
pkgdesc = $SUMMARY
url = $URL
builddate = $(date +%s)
packager = $MAINTAINER
size = $size
arch = any
license = MIT
depend = python
depend = python-gobject
depend = gtk3
depend = iproute2
optdepend = libayatana-appindicator: tray icon
optdepend = librsvg: tray icon rendering
EOF

    cat > "$payload/.INSTALL" <<'EOF'
post_install() {
    echo ""
    echo "Discord Presence installed. Enable it for your user (no root):"
    echo "    systemctl --user enable --now discord-game-presence.service"
    echo "    systemctl --user enable --now discord-game-presence-tray.service"
}
post_upgrade() { post_install; }
EOF

    mkdir -p "$DIST"
    ( cd "$payload" && bsdtar -cf - .PKGINFO .INSTALL usr | zstd -q -19 \
        > "$DIST/$PKGNAME-$VERSION-$RELEASE-any.pkg.tar.zst" )
}

# -------------------------------------------------------------- appimage
# O AppImage carrega o proprio codigo, mas usa o Python/GTK do sistema: um
# bundle completo de GTK passaria de 100 MB e quebraria a integracao com o
# tema de icones do desktop, que e justamente o que a bandeja usa.
build_appimage() {
    local tool="$WORK/appimagetool/AppRun"
    if [ ! -x "$tool" ]; then
        msg "baixando appimagetool"
        mkdir -p "$WORK/appimagetool"
        local url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
        if ! curl -fsSL "$url" -o "$WORK/appimagetool.AppImage"; then
            warn "sem appimagetool: pulando .AppImage"; return
        fi
        chmod +x "$WORK/appimagetool.AppImage"
        ( cd "$WORK/appimagetool" && "$WORK/appimagetool.AppImage" --appimage-extract >/dev/null 2>&1 \
            && cp -r squashfs-root/* . 2>/dev/null || true )
        [ -x "$tool" ] || { warn "nao consegui extrair o appimagetool: pulando"; return; }
    fi

    msg "construindo .AppImage"
    local app="$WORK/AppDir"
    rm -rf "$app"; mkdir -p "$app/usr/bin"
    install -Dm755 "$ROOT/discord-game-presence.py" "$app/usr/bin/$PKGNAME"
    install -Dm755 "$ROOT/discord-presence-tray.py" "$app/usr/bin/discord-presence-tray"
    install -Dm644 "$ROOT/packaging/files/$PKGNAME.svg" "$app/$PKGNAME.svg"
    install -Dm644 "$ROOT/packaging/files/$PKGNAME.svg" \
        "$app/usr/share/icons/hicolor/scalable/apps/$PKGNAME.svg"
    install -Dm644 "$ROOT/packaging/files/discord-presence-tray.desktop" \
        "$app/$PKGNAME.desktop"
    sed -i "s/^Icon=.*/Icon=$PKGNAME/" "$app/$PKGNAME.desktop"
    install -Dm755 "$ROOT/packaging/appimage/AppRun" "$app/AppRun"

    mkdir -p "$DIST"
    ARCH=x86_64 "$tool" --no-appstream "$app" \
        "$DIST/DiscordPresence-$VERSION-x86_64.AppImage" >/dev/null 2>&1 \
        || { warn "appimagetool falhou"; return; }
}

# ----------------------------------------------------------------- files
# A instalacao por script (install.sh) se atualiza com estes quatro arquivos:
# os nomes sao os que o daemon procura na release (SCRIPT_FILES).
build_files() {
    msg "copiando os arquivos da instalacao por script"
    mkdir -p "$DIST"
    install -m755 "$ROOT/discord-game-presence.py" "$ROOT/discord-presence-tray.py" "$DIST/"
    install -m644 "$ROOT/systemd/$PKGNAME.service" "$ROOT/systemd/$PKGNAME-tray.service" "$DIST/"
}

write_sums() {
    ( cd "$DIST" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | sort \
        | xargs -r sha256sum > SHA256SUMS )
}

main() {
    local targets=("$@")
    [ ${#targets[@]} -gt 0 ] || targets=(files rpm deb arch appimage)
    mkdir -p "$DIST"
    for target in "${targets[@]}"; do
        case "$target" in
            files) build_files ;;
            rpm) build_rpm ;;
            deb) build_deb ;;
            arch) build_arch ;;
            appimage) build_appimage ;;
            *) warn "formato desconhecido: $target" ;;
        esac
    done
    write_sums
    msg "pacotes em $DIST:"
    ls -lh "$DIST" | tail -n +2 | awk '{printf "    %-52s %s\n", $9, $5}'
    post_install_note
}

main "$@"

#!/usr/bin/env bash
# Runs inside the container as the normal user "tester" (real systemd user
# manager and session bus). $1 = script | package, $2 = distro name.
set -u
KIND="$1"; DISTRO="$2"
res() { echo "RESULT|$1|$2|${3:-}"; }
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
cd ~

# a headless X server, as a desktop session would have
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &

if [ "$KIND" = script ]; then
    # exactly what the README says, with the release served from the test files
    cat /src/install.sh | SKIP_VESKTOP=1 DGP_RELEASE_URL=file:///rel bash > /tmp/install.log 2>&1
    code=$?
    res "installer (curl, then bash) exits 0" "$([ $code = 0 ] && echo ok || echo fail)" "$(tail -3 /tmp/install.log | tr '\n' ' ')"
    res "installer verified sha256" "$(grep -q 'sha256 conferido' /tmp/install.log && echo ok || echo fail)"
    res "dependencies installed with the distro's package manager" \
        "$(grep -q 'instalando dependencias' /tmp/install.log && echo ok || echo fail)" \
        "$(grep 'instalando dependencias' /tmp/install.log | head -1)"
    res "daemon and tray units enabled" \
        "$(systemctl --user is-enabled discord-game-presence.service discord-game-presence-tray.service >/dev/null && echo ok || echo fail)"
else
    case "$DISTRO" in
        arch)        sudo pacman -U --noconfirm /rel/*.pkg.tar.zst ;;
        ubuntu*|debian*) sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y /rel/*.deb ;;
        fedora)      sudo dnf install -y /rel/*.rpm ;;
        *)           false ;;
    esac > /tmp/install.log 2>&1
    code=$?
    res "native package installs with its dependencies" "$([ $code = 0 ] && echo ok || echo fail)" "$(tail -2 /tmp/install.log | tr '\n' ' ')"
    [ $code = 0 ] || { echo "SUMMARY|$KIND|fail|package"; exit 1; }
    systemctl --user daemon-reload
fi

python3 /src/tests/distros/checks.py "$KIND"
checks=$?

if [ "$KIND" = script ]; then
    rm -rf /tmp/src && mkdir /tmp/src && tar -C /src --exclude=./.build --exclude=./dist --exclude=./tests/distros/out --exclude=__pycache__ -cf - . | tar -C /tmp/src -xf -
    (cd /tmp/src && python3 -m unittest discover -s tests) > /tmp/unit.log 2>&1
    grep -A30 '^FAIL:\|^ERROR:' /tmp/unit.log
    out="$(grep '^FAIL:\|^ERROR:' /tmp/unit.log | tr '\n' ' ')$(tail -3 /tmp/unit.log | tr '\n' ' ')"
    res "unit tests with this distro's python $(python3 -c 'import platform; print(platform.python_version())')" \
        "$(echo "$out" | grep -q '^.*OK' && echo ok || echo fail)" "$out"
fi
exit $checks

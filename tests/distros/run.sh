#!/usr/bin/env bash
# Distro matrix: every supported distro in a container with systemd as PID 1,
# a normal user with a real `systemctl --user` manager and session bus, and a
# headless X server. Nothing of this PC is touched (rootless podman).
#
#   tests/distros/run.sh                 # all distros, script + package installs
#   tests/distros/run.sh fedora arch     # some of them
#
# Uses dist/ as the release (run packaging/build.sh first). Writes
# tests/distros/out/<distro>-<kind>.log and results.tsv, then the report.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/out"
ENGINE="${ENGINE:-$(command -v podman || command -v docker)}"
[ -n "$ENGINE" ] || { echo "needs podman or docker"; exit 2; }
# shellcheck source=images.sh
. "$HERE/images.sh"
[ -f "$SRC/dist/SHA256SUMS" ] || { echo "no dist/: run packaging/build.sh"; exit 2; }
mkdir -p "$OUT"; : > "$OUT/results.tsv"
WANT=("$@")

wanted() { [ ${#WANT[@]} = 0 ] && return 0; local w; for w in "${WANT[@]}"; do [ "$w" = "$1" ] && return 0; done; return 1; }

# userdbd's worker answers ENOPKG for every NSS user under rootless podman
# (systemd 262), so logind never sees the user; lookups then go straight to NSS.
build_image() {  # name base packages
    local name="$1" base="$2" pkgs="$3"
    "$ENGINE" build -q -t "dgp-test-$name" -f - "$HERE" >/dev/null <<EOF
FROM $base
RUN $pkgs
RUN userdel -r ubuntu >/dev/null 2>&1; useradd -m -u 1000 -s /bin/bash tester 2>/dev/null || useradd -m -s /bin/bash tester; \
    mkdir -p /etc/sudoers.d /var/lib/systemd/linger && \
    echo 'tester ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/tester && chmod 440 /etc/sudoers.d/tester && \
    touch /var/lib/systemd/linger/tester; \
    systemctl mask systemd-firstboot.service systemd-homed.service systemd-resolved.service \
        systemd-userdbd.socket systemd-userdbd.service >/dev/null 2>&1 || true
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/lib/systemd/systemd"]
EOF
}

run_one() {  # name kind
    local name="$1" kind="$2" c="dgp-$1-$2" log="$OUT/$1-$2.log" start
    start=$(date +%s)
    "$ENGINE" rm -f "$c" >/dev/null 2>&1
    "$ENGINE" run -d --name "$c" --systemd=always --tmpfs /tmp \
        -v "$SRC:/src:ro,Z" -v "$SRC/dist:/rel:ro,Z" "dgp-test-$name" >/dev/null || {
        printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$kind" "container starts" fail "" >> "$OUT/results.tsv"; return; }
    # the user manager (linger) brings up /run/user/1000 and the session bus
    for _ in $(seq 60); do
        "$ENGINE" exec "$c" test -S /run/user/1000/bus 2>/dev/null && break; sleep 1
    done
    "$ENGINE" exec "$c" test -S /run/user/1000/bus 2>/dev/null || {
        printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$kind" "user session bus comes up" fail \
            "$("$ENGINE" exec "$c" systemctl --failed --no-legend 2>&1 | tr '\n' ' ')" >> "$OUT/results.tsv"
        "$ENGINE" rm -f "$c" >/dev/null 2>&1; return; }
    "$ENGINE" exec -u tester "$c" bash /src/tests/distros/inside.sh "$kind" "$name" > "$log" 2>&1
    grep '^RESULT|' "$log" | while IFS='|' read -r _ check state detail; do
        printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$kind" "$check" "$state" "$detail" >> "$OUT/results.tsv"
    done
    grep -q '^SUMMARY|' "$log" || printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$kind" "checks run to the end" fail \
        "$(grep -v '^RESULT|' "$log" | tail -2 | tr '\n' ' ')" >> "$OUT/results.tsv"
    "$ENGINE" rm -f "$c" >/dev/null 2>&1
    echo "  $name $kind: $(grep -c '|ok|' "$log") ok, $(grep -c '|fail|' "$log") failed ($(( $(date +%s) - start )) s)"
}

for entry in "${DISTROS[@]}"; do
    IFS='|' read -r name base pkgs <<< "$entry"
    wanted "$name" || continue
    echo "== $name"
    build_image "$name" "$base" "$pkgs" || { echo "  image build failed"; continue; }
    run_one "$name" script
    case "$name" in opensuse*) ;; *) run_one "$name" package ;; esac
done
python3 "$HERE/report.py" "$OUT/results.tsv" "$SRC"

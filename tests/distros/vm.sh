#!/usr/bin/env bash
# The same checks in a real VM (KVM): full kernel, real boot, real logind
# login with lingering, nothing of a container in the way. Uses the official
# Ubuntu 24.04 cloud image (sha256 checked against Ubuntu's SHA256SUMS), a
# throwaway overlay disk, and cloud-init fed over HTTP from 127.0.0.1.
#
#   tests/distros/vm.sh            # results go to tests/distros/out/results.tsv
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/out"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/dgp-vm"
BASE="https://cloud-images.ubuntu.com/noble/current"
IMG=noble-server-cloudimg-amd64.img
NAME=ubuntu-24.04-vm
mkdir -p "$CACHE" "$OUT"
[ -f "$SRC/dist/SHA256SUMS" ] || { echo "no dist/: run packaging/build.sh"; exit 2; }

cd "$CACHE"
[ -f "$IMG" ] || curl -fsSLO "$BASE/$IMG"
curl -fsSLO "$BASE/SHA256SUMS"
sha256sum --quiet --ignore-missing -c SHA256SUMS

WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT
qemu-img create -q -f qcow2 -F qcow2 -b "$CACHE/$IMG" "$WORK/disk.qcow2" 12G
mkdir -p "$WORK/seed" "$WORK/results"
printf 'instance-id: dgp-%s\nlocal-hostname: dgp-test\n' "$(date +%s)" > "$WORK/seed/meta-data"
: > "$WORK/seed/vendor-data"
cat > "$WORK/seed/user-data" <<'EOF'
#cloud-config
users:
  - name: tester
    uid: 1000
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
packages: [xvfb, desktop-file-utils]
mounts:
  - [src, /src, 9p, "trans=virtio,version=9p2000.L,ro", "0", "0"]
  - [rel, /rel, 9p, "trans=virtio,version=9p2000.L,ro", "0", "0"]
  - [res, /res, 9p, "trans=virtio,version=9p2000.L", "0", "0"]
runcmd:
  - loginctl enable-linger tester
  - for i in $(seq 60); do [ -S /run/user/1000/bus ] && break; sleep 1; done
  - su - tester -c 'bash /src/tests/distros/inside.sh script ubuntu-24.04-vm' > /res/script.log 2>&1 || true
  - poweroff
EOF
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
python3 -m http.server -b 127.0.0.1 -d "$WORK/seed" "$PORT" >/dev/null 2>&1 &
echo "booting $NAME (KVM)"
start=$(date +%s)
timeout 900 qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 4096 -display none \
    -drive file="$WORK/disk.qcow2",if=virtio \
    -nic user,model=virtio-net-pci \
    -smbios type=1,serial=ds="nocloud;s=http://10.0.2.2:$PORT/" \
    -virtfs local,path="$SRC",mount_tag=src,security_model=none,readonly=on \
    -virtfs local,path="$SRC/dist",mount_tag=rel,security_model=none,readonly=on \
    -virtfs local,path="$WORK/results",mount_tag=res,security_model=mapped-xattr \
    -serial file:"$OUT/$NAME-console.log" || echo "qemu exited with $?"
cp "$WORK/results/script.log" "$OUT/$NAME-script.log" 2>/dev/null || : > "$OUT/$NAME-script.log"
grep '^RESULT|' "$OUT/$NAME-script.log" | while IFS='|' read -r _ check state detail; do
    printf '%s\t%s\t%s\t%s\t%s\n' "$NAME" script "$check" "$state" "$detail" >> "$OUT/results.tsv"
done
grep -q '^SUMMARY|' "$OUT/$NAME-script.log" || printf '%s\tscript\tchecks run to the end\tfail\tsee %s-console.log\n' \
    "$NAME" "$NAME" >> "$OUT/results.tsv"
echo "  $NAME script: $(grep -c '|ok|' "$OUT/$NAME-script.log") ok, $(grep -c '|fail|' "$OUT/$NAME-script.log") failed ($(( $(date +%s) - start )) s)"

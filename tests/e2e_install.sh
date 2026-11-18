#!/usr/bin/env bash
# Ponta a ponta do instalador, do jeito que as pessoas rodam:
#   cat install.sh | bash   (o mesmo que curl ... | bash)
#
# Confere que ele baixa os quatro arquivos da release, confere o sha256,
# instala, reinicia daemon e bandeja, nao pede senha com as dependencias ja
# instaladas, guarda a versao anterior ao rodar de novo, recusa uma release
# adulterada sem trocar nada e usa os arquivos do clone com bash install.sh.
#
# A release e um servidor local, systemctl e sudo sao de mentira e o HOME e
# temporario. Rode isolado, sem rede nem o systemd de verdade:
#
#   bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid --unshare-net \
#         --tmpfs /run/user/$(id -u) --tmpfs /tmp --die-with-parent \
#         bash tests/e2e_install.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -e "/run/user/$(id -u)/bus" ] && { echo "rode dentro do bwrap (veja o comeco do arquivo)"; exit 2; }
export HOME=/tmp/fakehome; mkdir -p $HOME
stub=/tmp/stub; mkdir -p $stub
cat > $stub/systemctl <<'S'
#!/bin/sh
echo "systemctl $*" >> /tmp/calls.log
S
cat > $stub/sudo <<'S'
#!/bin/sh
echo "sudo $*" >> /tmp/calls.log; exit 1
S
chmod +x $stub/*
export PATH=$stub:$PATH
# a release falsa: os mesmos quatro arquivos que packaging/build.sh anexa
mkdir -p /tmp/rel
cp "$REPO"/discord-game-presence.py "$REPO"/discord-presence-tray.py /tmp/rel/
cp "$REPO"/systemd/*.service /tmp/rel/
( cd /tmp/rel && sha256sum ./* | sed 's#  \./#  #' > SHA256SUMS )
cp -r /tmp/rel /tmp/bad
echo "# adulterado" >> /tmp/bad/discord-presence-tray.py
cd /tmp
python3 -m http.server 8765 --bind 127.0.0.1 --directory /tmp >/dev/null 2>&1 &
for i in $(seq 50); do curl -fs http://127.0.0.1:8765/rel/SHA256SUMS >/dev/null && break; sleep 0.1; done
ok() { if eval "$2"; then echo "[ok] $1"; else echo "[FALHOU] $1"; FAIL=1; fi; }
FAIL=0
echo "--- 1a instalacao, como curl | bash"
cat "$REPO/install.sh" | SKIP_VESKTOP=1 DGP_RELEASE_URL=http://127.0.0.1:8765/rel bash > /tmp/o1 2>&1; c1=$?
tail -5 /tmp/o1
ok "saiu com 0" '[ $c1 = 0 ]'
ok "baixou da release e conferiu sha256" 'grep -q "sha256 conferido" /tmp/o1'
ok "scripts iguais aos da release" 'cmp -s /tmp/rel/discord-game-presence.py $HOME/.local/bin/discord-game-presence.py && cmp -s /tmp/rel/discord-presence-tray.py $HOME/.local/bin/discord-presence-tray.py'
ok "scripts executaveis" '[ -x $HOME/.local/bin/discord-game-presence.py ]'
ok "units no lugar, com ExecReload" 'grep -q ExecReload $HOME/.config/systemd/user/discord-game-presence.service && [ -f $HOME/.config/systemd/user/discord-game-presence-tray.service ]'
ok "reinicia daemon e bandeja" 'grep -q "restart discord-game-presence.service" /tmp/calls.log && grep -q "restart discord-game-presence-tray.service" /tmp/calls.log'
ok "nao pediu sudo (dependencias ja instaladas)" '! grep -q "^sudo" /tmp/calls.log'
echo "--- 2a vez: atualiza e guarda a anterior"
cat "$REPO/install.sh" | SKIP_VESKTOP=1 DGP_RELEASE_URL=http://127.0.0.1:8765/rel bash > /tmp/o2 2>&1; c2=$?
ok "saiu com 0 e guardou a versao anterior" '[ $c2 = 0 ] && ls $HOME/.local/share/discord-game-presence/backup/*/discord-presence-tray.py >/dev/null 2>&1'
echo "--- release adulterada"
before=$(sha256sum $HOME/.local/bin/discord-presence-tray.py)
cat "$REPO/install.sh" | SKIP_VESKTOP=1 DGP_RELEASE_URL=http://127.0.0.1:8765/bad bash > /tmp/o3 2>&1; c3=$?
grep -E "xx|FAILED" /tmp/o3 | head -3
ok "recusou e nao trocou nada" '[ $c3 != 0 ] && grep -q "sha256 nao confere" /tmp/o3 && [ "$before" = "$(sha256sum $HOME/.local/bin/discord-presence-tray.py)" ]'
echo "--- release sem o arquivo"
cat "$REPO/install.sh" | SKIP_VESKTOP=1 DGP_RELEASE_URL=http://127.0.0.1:8765/nada bash > /tmp/o4 2>&1; c4=$?
ok "falha clara quando a release nao existe" '[ $c4 != 0 ] && grep -q "falhou baixar" /tmp/o4'
echo "--- de um clone"
SKIP_VESKTOP=1 bash "$REPO/install.sh" > /tmp/o5 2>&1; c5=$?
ok "bash install.sh usa os arquivos do clone" '[ $c5 = 0 ] && grep -q "usando os arquivos do clone" /tmp/o5'
exit $FAIL

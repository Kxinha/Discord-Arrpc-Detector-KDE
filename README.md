# arRPC Personalizado Pessoal Linux

Meu Rich Presence caseiro para o Discord no Linux — feito porque o Discord
ignora metade do que eu uso: jogo nativo, jogo sob Proton, série no Stremio,
música e vídeo no navegador. Em vez de esperar suporte, o daemon procura os
processos e **fala o protocolo IPC do Discord direto**.

Python puro (só biblioteca padrão), roda como serviço systemd de usuário e
sobe sozinho no boot. Sem Node, sem arRPC oficial, sem plugin do Vencord.

Vem com um **ícone de bandeja** (GTK3 + AppIndicator, nada de Qt e nenhuma
janela): tudo — quais apps o Discord enxerga, aparência do ícone, início
automático — se resolve pelo menu.

---

## O problema

O Discord detecta jogos comparando os processos abertos com a base pública de
jogos detectáveis (`https://discord.com/api/v9/applications/detectable`). No
Linux essa base falha de várias formas ao mesmo tempo:

| Situação | O que a base traz | Resultado |
|---|---|---|
| RuneScape nativo (`rs2client`) | só executáveis `win32` e `darwin` | nunca detecta |
| GTA SA sob Proton (`gta-sa.exe`) | executável `win32` | o processo tem caminho Wine, não bate |
| TBH: Task Bar Hero | **nenhum** executável cadastrado | nunca detecta |
| Stremio, YouTube, YouTube Music | não existem na base | nunca detecta |

Este projeto resolve pelo outro lado: eu digo qual processo é qual app, e o
daemon publica a presença.

## O que ele mostra

| App | Como detecta | O que aparece |
|---|---|---|
| **RuneScape 3** | processo `rs2client` | "Gielinor" |
| **Minecraft** (Prism) | processo da instância | instância, **servidor conectado** e **lotação** (ex.: `860 of 5000`) |
| **Left 4 Dead 2** | `hl2_linux` ou `.exe` no Proton | **campanha e capítulo** ("Dark Carnival — capítulo 3") |
| **GTA: San Andreas** | `gta-sa.exe` sob Proton | "Jogando" |
| **TBH: Task Bar Hero** | `TaskBarHero.exe` sob Proton | "Jogando" |
| **Stremio** | MPRIS | **série — episódio** ("Prison Break — Unearthed (2x9)") |
| **YouTube** (Firefox) | MPRIS + URL da aba | **título e canal**, miniatura do vídeo, progresso, botão do vídeo |
| **YouTube Music** (Firefox) | MPRIS + URL da aba | **faixa — artista**, capa do álbum, progresso, botões |

Adicionar um app novo é escolher no menu da bandeja, ou editar um JSON —
nenhuma linha de código. Veja [O ícone de bandeja](#o-ícone-de-bandeja) e
[Adicionar seus próprios apps](#adicionar-seus-próprios-apps).

---

## Instalação

### Pacote pronto

Baixe da [página de releases](https://github.com/gabrielmf1998/arrpc-personalizado-pessoal-linux/releases):

| Distro | Arquivo | Comando |
|---|---|---|
| **Fedora**, RHEL, Nobara | `.rpm` | `sudo dnf install ./discord-game-presence-*.noarch.rpm` |
| **Debian**, Ubuntu, Mint, Pop!_OS | `.deb` | `sudo apt install ./discord-game-presence_*_all.deb` |
| **Arch**, Manjaro, EndeavourOS, CachyOS | `.pkg.tar.zst` | `sudo pacman -U ./discord-game-presence-*-any.pkg.tar.zst` |
| Qualquer outra | `.AppImage` | `chmod +x DiscordPresence-*.AppImage && ./DiscordPresence-*.AppImage` |

Depois, uma vez, sem root:

```bash
systemctl --user enable --now discord-game-presence.service
systemctl --user enable --now discord-game-presence-tray.service
```

Arch também pode construir do jeito idiomático com o
[`PKGBUILD`](packaging/arch/PKGBUILD) do repositório.

O `.AppImage` carrega o código do projeto mas usa o **Python e o GTK do
sistema** — empacotar GTK inteiro passaria de 100 MB e ainda quebraria a
integração com o tema de ícones do desktop, que é justamente o que a bandeja
usa. Ele aceita `./DiscordPresence-*.AppImage daemon` para rodar só o daemon e
`install` para copiar tudo para `~/.local/bin`.

### Script (instala também o Vesktop)

```bash
curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/arrpc-personalizado-pessoal-linux/main/install.sh | bash
```

O instalador:

1. detecta a distro (Arch, Debian/Ubuntu, Fedora, openSUSE, ou genérica);
2. instala as dependências (`python3`, `iproute2`/`ss`, `curl`);
3. instala o **Vesktop nativo** — pacote do repositório no Arch, `.deb`/`.rpm`
   oficial nas demais, tarball em `/opt` como reserva;
4. instala o daemon em `~/.local/bin` e o serviço em `~/.config/systemd/user`,
   já habilitado;
5. liga o log de console do L4D2, necessário para saber o mapa.

Já usa outro cliente que exponha o socket de IPC? `SKIP_VESKTOP=1 curl ... | bash`.

Prefere clonar: `git clone` + `./install.sh` faz o mesmo.
Passo a passo manual, sem script: [`INSTALAR.txt`](INSTALAR.txt).

### Por que Vesktop nativo

O daemon precisa do socket `$XDG_RUNTIME_DIR/discord-ipc-0`. O Discord em
Flatpak roda em sandbox com `/run` isolado e o socket não fica visível de fora.
Vesktop nativo (`.deb`/`.rpm`/pacote da distro) cria o socket no runtime dir
real do usuário — que é o que funciona.

---

## O ícone de bandeja

O menu é a interface inteira — não existe janela. Ele fala com o systemd, com
o arquivo de configuração e com o próprio catálogo do daemon.

| Menu | O que faz |
|---|---|
| **Daemon: running/stopped** | estado do serviço, no topo |
| **Detected now:** | o que está sendo detectado neste instante |
| **Apps Discord can see** | liga e desliga cada app do catálogo; marca com `• detected` o que está aberto agora |
| ↳ **Add a running app** | varre os processos abertos, cruza com a base de jogos detectáveis do Discord e adiciona o que reconhecer — em um clique, com o Application ID certo |
| ↳ **Remove an app I added** | tira do catálogo o que você adicionou |
| ↳ **Enable all / Disable all** | tudo de uma vez |
| **Appearance → Icon shape** | nove desenhos próprios: Blob, Controller, Broadcast, Bolt, Chat, Headset, Rocket, Ghost, Dot |
| **Appearance → Icon from desktop theme** | usa o ícone do tema do sistema (Breeze, no KDE): Discord, Vesktop, Games, Gamepad… — para o ícone combinar com o resto do painel |
| **Appearance → Color** | 15 cores com hex (paleta do Discord + Breeze/Plasma + neutras) e **Match KDE accent**, que lê a cor do seu esquema atual |
| **Daemon → Start at boot** | `systemctl --user enable` do daemon |
| **Daemon → Start tray at login** | o mesmo para a bandeja |
| **Daemon → Clear stuck presence** | manda o Discord apagar qualquer presença pendurada |
| **Daemon → Reload catalog / View log** | recarrega o catálogo; abre o journal em um terminal |
| **Configuration** | mostra o caminho do JSON, abre o arquivo, abre a pasta, copia o caminho |
| **Start/Stop/Restart daemon**, **Quit tray** | controle direto |

O ícone fica **acinzentado quando o daemon está parado** e colorido quando
está rodando, então dá para ver o estado sem abrir o menu. As preferências
ficam em `"tray"` dentro do mesmo `~/.config/discord-game-presence.json`.

Ele roda pelo serviço `discord-game-presence-tray.service`, ligado à sessão
gráfica. Para instalar sem a bandeja: `SKIP_TRAY=1 ./install.sh`.

---

## Adicionar seus próprios apps

Toda a configuração vive em `~/.config/discord-game-presence.json`. O que
estiver lá é aplicado por cima da tabela padrão: entradas novas são
adicionadas, entradas existentes têm só os campos citados trocados, e um
`client_id` vazio desliga a entrada.

### 1. Descubra o Application ID

Se o jogo estiver na base do Discord, use o id de lá:

```bash
curl -s https://discord.com/api/v9/applications/detectable \
  | python3 -c 'import json,sys
for a in json.load(sys.stdin):
    if "parte do nome" in a["name"].lower(): print(a["id"], a["name"])'
```

Se **não** estiver (caso de Stremio, YouTube, e de qualquer app que não seja
jogo), crie um app seu em https://discord.com/developers/applications →
**New Application**. O nome do app é o nome que aparece no seu perfil. Suba
uma imagem em **General Information → App Icon** e copie o **Application ID**.

### 2. Descubra o processo

```bash
ps aux | grep -i <nome do app>
```

O casamento é por trecho da linha de comando, então qualquer parte estável
serve. É isso que permite pegar jogos sob Proton, cujo processo aparece como
`S:\common\Jogo\jogo.exe` — basta usar `jogo.exe`.

Comandos de shell (`bash -c …`) ficam de fora da busca de propósito: um
comando digitado no terminal carrega o próprio nome procurado e virava falso
positivo. Scripts de jogo (o `hl2.sh` do L4D2, por exemplo) continuam valendo.

### 3. Escreva a entrada

```json
{
  "games": {
    "jogo.exe": {
      "client_id": "1234567890",
      "details": "Nome do Jogo",
      "state": "Jogando",
      "large_text": "Nome do Jogo",
      "large_image": "https://exemplo/imagem.png"
    }
  }
}
```

| Campo | Para que serve |
|---|---|
| `client_id` | Application ID. Vazio desliga a entrada |
| `details` | primeira linha da presença |
| `state` | segunda linha |
| `large_text` | texto ao passar o mouse na imagem |
| `large_image` | nome de um Art Asset do app **ou** uma URL de imagem |
| `type` | `0` jogando, `2` ouvindo, `3` assistindo (veja os limites) |

Depois: `systemctl --user restart discord-game-presence`.

### Trocando os apps de mídia pelos seus

Stremio, YouTube e YouTube Music vêm apontando para apps que eu criei. Para
usar os seus (com o seu ícone), basta sobrescrever o id:

```json
{
  "games": {
    "youtube":                 { "client_id": "SEU_ID" },
    "youtube-music":           { "client_id": "SEU_ID" },
    "stremio":                 { "client_id": "SEU_ID" }
  }
}
```

### Quando o JSON não basta

Presenças que mudam sozinhas — servidor do Minecraft, mapa do L4D2, faixa
tocando — são funções Python na tabela `GAMES`, dentro do
`discord-game-presence.py`. `state` e `details` aceitam qualquer callable que
devolva texto; `state` pode devolver `(texto, extras)` para mandar também
`timestamps`, `buttons`, `party` ou trocar a imagem a cada faixa. Os detectores
existentes servem de modelo.

---

## Como as detecções mais interessantes funcionam

### Minecraft: servidor e lotação

1. acha o PID pela linha de comando (`PrismLauncher/instances`);
2. extrai a instância de `-Djava.library.path=.../instances/<nome>/natives`;
3. lista as conexões TCP com `ss -tnp`, descartando loopback e LAN;
4. resolve os hosts do `servers.dat` da instância e casa com o IP conectado —
   assim mostra `play.exemplo.com`, e não um IP de CDN;
5. faz um **Server List Ping** (handshake + status do protocolo Minecraft,
   ~30 linhas aqui) para pegar `online`/`max`;
6. publica como `party.size`, que o Discord escreve como "(860 of 5000)".

### Left 4 Dead 2: mapa

O L4D2 não expõe o mapa em processo, socket ou arquivo aberto — mapas oficiais
moram dentro de VPKs. A única fonte é o console. O instalador escreve
`con_logfile "console.log"` em `left4dead2/cfg/autoexec.cfg`, e o daemon lê o
fim do log, traduzindo `c2m3_coaster` → "Dark Carnival — capítulo 3".

> O log só passa a existir **depois de reiniciar o jogo** uma vez.

### Navegador e Stremio: MPRIS

Tudo vem do D-Bus (`org.mpris.MediaPlayer2`), lido com `busctl`. O bus name do
Firefox muda a cada boot e cada aba com mídia vira um player, então a escolha é
**pela URL da aba**, não pelo nome — é assim que YouTube e YouTube Music não se
confundem, e é por isso que essas entradas usam uma função em `running` no
lugar de um nome de processo.

O Firefox é impreciso de dois jeitos, e ambos precisaram de contorno:

- **`Position` trava em 0** mesmo com o vídeo no meio. O daemon guarda a última
  posição real vista e projeta o tempo a partir dela, reancorando sempre que
  uma leitura verdadeira chega (o que também corrige seeks). Se o vídeo já
  estava rodando quando o daemon começou a olhar, a posição é tratada como
  desconhecida e nenhum tempo é publicado — melhor sem barra do que com barra
  errada.
- **`mpris:length` some do metadata** no meio da reprodução. A duração fica em
  cache por faixa e, para vídeo do YouTube, é lida da própria página
  (`lengthSeconds`, que aparece só depois de ~1 MB de HTML).

Capa: o Firefox não publica `mpris:artUrl` utilizável, então a arte vem da
miniatura do vídeo quando a URL traz o id, ou da busca do iTunes por
artista + título no caso do YouTube Music.

---

## Limites conhecidos

- **O `type` é ignorado.** Mandar `2` (Ouvindo) ou `3` (Assistindo) é aceito
  pelo IPC, mas o Discord responde com `type: 0` — via RPC, app comum só
  publica como "Jogando". O formato de barra do Spotify não é alcançável.
- **Botões não aparecem para você mesmo**, só para quem vê seu perfil.
- **Stremio não publica posição**, então não há progresso real do episódio — o
  cronômetro reinicia a cada título novo.
- Mídia **pausada por mais de 5 minutos** sai do perfil: uma aba de vídeo
  pausada continua sendo um player enquanto existir, e a presença ficava
  pendurada dizendo "(pausado)" por horas. Volta sozinha ao dar play.
- Página de canal, Shorts e embeds do YouTube não trazem id na URL: sem
  miniatura e sem duração nesses casos.
- O link do artista no YouTube Music leva à **busca**, porque o MPRIS não traz
  o canal.

---

## Operação

```bash
systemctl --user status  discord-game-presence          # estado
systemctl --user restart discord-game-presence          # após editar a config
journalctl --user -u discord-game-presence -f           # log ao vivo
systemctl --user restart discord-game-presence-tray     # a bandeja
```

Pela bandeja, os mesmos comandos estão em **Daemon** e no rodapé do menu.

Uma thread por app, cada uma esperando o processo aparecer. Se o Discord
estiver fechado ou o socket cair, a thread tenta de novo no ciclo seguinte
(15s). O serviço tem `Restart=always`.

O Discord mantém a última atividade publicada mesmo depois que a conexão
morre — sem cuidado, fechar o app deixaria a presença pendurada com o
cronômetro correndo. Por isso o daemon apaga as presenças **ao iniciar**
(restos de execuções anteriores, inclusive de um `restart` anterior) e **ao
ser encerrado** (`SIGTERM`/`SIGINT`), e nunca publica um estado genérico
quando o player já sumiu. Se algo ficar preso, `restart` resolve.

## Desinstalar

```bash
systemctl --user disable --now discord-game-presence
rm ~/.local/bin/discord-game-presence.py
rm ~/.config/systemd/user/discord-game-presence.service
rm -f ~/.config/discord-game-presence.json
systemctl --user daemon-reload
```

## Arquivos

```
discord-game-presence.py                   o daemon
discord-presence-tray.py                   o ícone de bandeja (GTK3, sem Qt)
install.sh                                 instalador multi-distro
INSTALAR.txt                               instalação manual, passo a passo
docs/config.exemplo.json                   modelo de configuração
systemd/*.service                          units para instalação por usuário
packaging/build.sh                         gera .rpm, .deb, .pkg.tar.zst e .AppImage
packaging/rpm|arch|appimage|files/         spec, PKGBUILD, AppRun e units de /usr
```

### Gerar os pacotes

```bash
packaging/build.sh              # tudo o que a máquina permitir
packaging/build.sh rpm deb      # só alguns formatos
```

Sai em `dist/`. Não precisa de root, e a máquina que constrói não precisa ser
da mesma distro do pacote — `.rpm`, `.deb` e o do pacman são árvores de
arquivos com metadados. O `.AppImage` baixa o `appimagetool` na primeira vez.

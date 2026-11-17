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
| **Minecraft** (Prism, MultiMC, Flatpak) | `java` da instância | nome da instância, **servidor conectado** (o nome que você digitou, lido do log do jogo) e **lotação** (ex.: `860 of 5000`) |
| **Left 4 Dead 2** | `hl2_linux -game left4dead2` ou `left4dead2.exe` no Proton | **campanha e capítulo** ("Dark Carnival — capítulo 3") |
| **GTA: San Andreas** | memória do processo | **bairro, veículo, vida, dinheiro**, estrelas de procurado e "Em missão" |
| **TBH: Task Bar Hero** | `TaskBarHero.exe` sob Proton | "Jogando" |
| **Roblox** (Sober) | processo `sober` | **nome da experiência**, criador, ícone e quantos estão jogando |
| **Stremio** (nativo ou Flatpak) | processo + MPRIS | **série — episódio** ("Prison Break — Unearthed (2x9)") |
| **YouTube** (Firefox, Chrome, Chromium, Brave, Vivaldi, Edge) | MPRIS + URL da aba | **título e canal**, miniatura do vídeo (Shorts e lives também), progresso, botão do vídeo |
| **YouTube Music** (os mesmos navegadores) | MPRIS + URL da aba | **faixa — artista**, capa do álbum, progresso, botões |
| **Claude Code** | nome do executável | "Programming" e o tempo de sessão — nada além disso |

Adicionar um app novo é escolher no menu da bandeja, ou editar um JSON —
nenhuma linha de código. Veja [O ícone de bandeja](#o-ícone-de-bandeja) e
[Adicionar seus próprios apps](#adicionar-seus-próprios-apps).

---

## Instalação

### Pacote pronto

Baixe da [página de releases](https://github.com/gabrielmf1998/Discord-Arrpc-Detector-KDE/releases):

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
curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Discord-Arrpc-Detector-KDE/main/install.sh | bash
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

O daemon precisa do socket `$XDG_RUNTIME_DIR/discord-ipc-N`. Vesktop nativo
(`.deb`/`.rpm`/pacote da distro) e o Discord nativo criam o socket no runtime
dir real do usuário, que é o caminho mais simples.

O Discord do Flathub também serve: o `discord.sh` dele repassa o socket para
`$XDG_RUNTIME_DIR/app/com.discordapp.Discord/discord-ipc-0`, e o daemon procura
nessas pastas sozinho. O Vesktop em Flatpak não expõe o socket para fora da
sandbox. Um socket velho, de um cliente que caiu, é pulado em vez de prender o
daemon nele.

---

## O ícone de bandeja

O menu é a interface inteira — não existe janela. Ele fala com o systemd, com
o arquivo de configuração e com o próprio catálogo do daemon.

| Menu | O que faz |
|---|---|
| **Daemon: running/stopped** | estado do serviço, no topo |
| **Detected now:** | o que está sendo detectado neste instante |
| **Apps Discord can see** | liga e desliga cada app do catálogo; marca com `• detected` o que está aberto agora |
| ↳ **Add a running app** | varre os processos abertos, cruza com a base de jogos detectáveis do Discord e adiciona o que reconhecer, em um clique, com o Application ID e o ícone certos. Usa as mesmas regras do arRPC (pedaços do caminho, entradas de launcher ignoradas, argumentos exigidos), com duas proteções a mais: um nome que vários jogos usam (`game.exe`, `client.exe`) só vale se um pedaço maior do caminho desempatar, e jogos nativos (`Unturned.x86_64`) casam com o `.exe` da base quando o nome é de um jogo só. Wrappers do Proton e processos do Wine (`explorer.exe`, `services.exe`) nunca viram sugestão |
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
ps -eo comm,args | grep -i <nome do app>
```

Use o **nome do executável**: `jogo.exe` para um jogo sob Proton (o processo
dele aparece como `S:\common\Jogo\jogo.exe`), `Jogo.x86_64` ou `jogo` para
um nativo. O nome é comparado inteiro, sem diferença de maiúsculas, contra o
programa que está rodando (`argv[0]`), o `comm` do kernel, o executável real e,
quando o processo é um interpretador (`bash`, `python3`, `java -jar`, `mono`),
o script que ele carrega. `.exe`, `.x86_64`, `.x86` e `.sh` podem ficar de fora.

Por isso não contam mais como "jogo aberto" um `grep jogo.exe`, um editor
aberto no `autoexec.cfg`, um `tail -f` no log nem os wrappers do Proton
(`proton waitforexitandrun …/jogo.exe`), que só carregam o caminho nos
argumentos. Comandos de shell (`bash -c …`) também ficam de fora.

Para conferir o que o daemon está vendo, sem publicar nada:

```bash
discord-game-presence --scan      # ou ~/.local/bin/discord-game-presence.py --scan
```

Ele lista cada app do catálogo, aberto ou fechado, com o PID e o processo que
provou que está aberto, desde quando, os sockets de IPC encontrados e cada
player MPRIS com o serviço reconhecido.

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
| `exe` | nome do executável (ou lista); sem ele vale a chave da entrada |
| `args` | trecho(s) que também precisam estar nos argumentos, ex.: `"-game left4dead2"` |
| `cmdline` | trecho qualquer da linha de comando, o jeito antigo (editores, `grep` e `tail` não contam) |
| `match` | lista de regras alternativas, cada uma com `exe`/`args`/`cmdline` |
| `grace` | segundos de tolerância antes de apagar quando o app some (útil para mídia) |

Uma chave sem `/`, `\`, `:` ou espaço é tratada como `exe`; uma com algum
deles continua valendo como trecho de linha de comando, então configs antigos
seguem funcionando. Exemplo com duas regras:

```json
"meujogo": {
  "client_id": "1234567890",
  "match": [{"exe": "meujogo_linux", "args": "-game meujogo"},
            {"exe": "meujogo.exe"}]
}
```

O **tempo de jogo** conta desde que o processo nasceu (lido de `/proc`), e
não desde que o daemon o viu: reiniciar o daemon, ligar outro app na bandeja ou
o Discord reconectar não zeram mais o cronômetro.

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

1. acha o processo `java` que leva o caminho de uma instância (Prism, MultiMC,
   PolyMC, inclusive em Flatpak) ou o main do Minecraft. Um `tail` no log ou o
   `JavaCheck.jar` do launcher não contam;
2. a pasta do jogo é o diretório de trabalho desse processo; o nome da
   instância sai do `instance.cfg` (o nome que o launcher mostra, não a pasta);
3. lista as conexões TCP estabelecidas com `ss -tnp`, descartando as de HTTP(S)
   (login, skins, atualização de mod), que antes podiam virar "o servidor";
4. o nome vem do `logs/latest.log`, onde o próprio jogo escreve
   `Connecting to <host>, <porta>`: é o endereço que você digitou, inclusive
   quando o servidor usa registro SRV e o IP conectado não bate com ele.
   Servidores de LAN e ZeroTier também aparecem. Sem essa linha, vale o
   `servers.dat` casado por DNS (com cache), e por último o IP;
5. faz um **Server List Ping** (handshake + status do protocolo Minecraft) no
   IP e porta realmente conectados, com o nome do servidor no handshake, para
   pegar `online`/`max`;
6. publica como `party.size`, que o Discord escreve como "(860 of 5000)".

### Left 4 Dead 2: mapa

O L4D2 não expõe o mapa em processo, socket ou arquivo aberto — mapas oficiais
moram dentro de VPKs. A única fonte é o console. O instalador escreve
`con_logfile "console.log"` em `left4dead2/cfg/autoexec.cfg`, e o daemon lê o
fim do log, traduzindo `c2m3_coaster` → "Dark Carnival — capítulo 3".

> O log só passa a existir **depois de reiniciar o jogo** uma vez.

### GTA San Andreas: lendo o jogo por dentro

O jogo carrega em endereço fixo (`0x400000`, sem ASLR), então o estado sai de
`/proc/<pid>/mem`: ponteiro do jogador em `0xB6F5F0`, posição pela matriz da
entidade, vida em `+0x540`, dinheiro em `0xB7CE54`, veículo em `+0x58C`.

Os **nomes vêm do próprio jogo**, não de uma tabela embutida aqui: o
`data/info.zon` diz que zona cobre cada coordenada — vale a menor caixa que
contém o ponto, senão sai "Los Santos" no lugar de "Ganton" — e o
`text/american.gxt` traduz o código dela. O GXT indexa por **CRC32 invertido**
(JamCRC) do nome da chave, o que dá para confirmar testando contra chaves
conhecidas. O mesmo caminho nomeia os veículos, via `data/vehicles.ide`. Como
tudo sai dos arquivos do jogo, a presença acompanha o idioma instalado.

"Em missão" vem de `$ONMISSION`, a variável global `$26` do script. O endereço
não foi copiado da internet: `tools/gta-calibrar-missao.py` fotografa as
variáveis fora e dentro de uma missão e mostra quais foram de 0 para 1 — de
380 candidatos sobraram dois, e um deles é o `$26` conhecido.

Nada disso altera o jogo: só leitura.

### Claude Code: só que está aberto

A detecção compara o **nome do executável** (`comm`), não a linha de comando:
os argumentos carregam o nome da sessão (`-n ...`), e nada do que está sendo
feito deve virar presença. O texto é fixo — "Claude Code" e "Programming" —
então não vazam diretório, projeto nem conversa; só que há uma sessão aberta e
há quanto tempo.

### Navegador e Stremio: MPRIS

Tudo vem do D-Bus (`org.mpris.MediaPlayer2`), lido com `busctl`, numa chamada
`GetAll` por player: metadados, estado e posição saem do mesmo instante. O bus
name do Firefox muda a cada boot e cada aba com mídia vira um player, então a
escolha é **pela URL da aba**, não pelo nome. É assim que YouTube e YouTube
Music não se confundem, e quando duas abas do mesmo serviço existem, a que está
tocando ganha da pausada.

O Firefox põe a URL no MPRIS (`xesam:url`). **Chrome, Chromium, Brave, Vivaldi
e Edge não põem**: publicam só título, canal e duração. Para eles a URL sai do
histórico do próprio navegador (o arquivo `History` de cada perfil, aberto só
para leitura): vale a visita das últimas 24 h cujo título de página é o que
está tocando (`<título> - YouTube`, `<faixa> | YouTube Music`), com prioridade
para o título exato. Nada do histórico vai para o Discord além do link do vídeo
que já está na presença. Sem essa visita (histórico desligado, aba anônima) o
player não conta como YouTube: melhor não mostrar do que mostrar o app errado.
Em compensação o Chrome informa a posição sempre, então a barra de progresso
fica exata.

Links de vídeo em qualquer formato dão a miniatura e o botão: `watch?v=`,
`youtu.be/`, `/shorts/`, `/live/` e `/embed/`. Uma troca de faixa ou o autoplay
derrubam o player por um instante; as entradas de mídia têm 10 s de tolerância
(`grace`) antes de apagar, para a presença não piscar.

O Firefox informa a posição **em rajadas**: publica por um tempo depois de um
seek ou de uma troca de vídeo — quando a página chama `setPositionState` — e
depois volta a `0`. Nunca publica `mpris:length`.

Por isso o tempo funciona assim:

- **Leitura real disponível**: barra de progresso verdadeira, `1:23 / 13:43`.
- **Entre as rajadas**: o daemon segue a partir da última leitura real, por até
  10 minutos. Um seek faz o Firefox reportar de novo, o que corrige a conta
  sozinho.
- **Sem nenhuma leitura real**: nenhum número. A duração do vídeo vai para o
  texto (`TGG · 13:43`), porque um cronômetro correndo ao lado do título parece
  a posição do vídeo e estaria sempre errado.

Instalar a extensão **Plasma Browser Integration** no Firefox (ou assistir pelo
Chromium) dá a barra sempre certa: esses publicam um player MPRIS com posição
de verdade, e o daemon procura o mesmo título neles antes de desistir.

A duração, quando existe, fica em cache por faixa e, para vídeo do YouTube,
pode ser lida da própria página (`lengthSeconds`, que aparece só depois de
~1 MB de HTML).

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
- Página de canal do YouTube não traz id de vídeo: sem miniatura nesse caso.
- No Chrome e nos outros Chromium, o YouTube só é reconhecido com o histórico
  ligado (aba anônima não conta), e o primeiro vídeo pode levar alguns
  segundos para aparecer, o tempo de o navegador gravar a visita.
- Se o Vesktop estiver com o arRPC ligado, ele também detecta sozinho os jogos
  que a base do Discord conhece (GTA SA sob Proton, por exemplo). Desligue a
  entrada correspondente na bandeja se aparecerem duas atividades.
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

Uma thread por app, cada uma esperando o processo aparecer (a lista de
processos é lida uma vez a cada 2 s e dividida entre todas, ~3 ms). Um app
aberto é conferido a cada 2 s, então fechar o jogo tira a presença do perfil em
cerca de 2 s; o texto da presença é atualizado no ritmo de cada app (5 a 15 s).
Se o Discord estiver fechado ou o socket cair, a thread tenta de novo no ciclo
seguinte. Se o Discord recusar uma atividade, o motivo vai para o log. O
serviço tem `Restart=always`.

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
tests/test_detection.py                    testes (python3 -m unittest discover -s tests)
tests/e2e_fake_discord.py                  ponta a ponta com um Discord falso, isolado em bwrap
systemd/*.service                          units para instalação por usuário
packaging/build.sh                         gera .rpm, .deb, .pkg.tar.zst e .AppImage
packaging/rpm|arch|appimage|files/         spec, PKGBUILD, AppRun e units de /usr
```

### Testes

```bash
python3 -m unittest discover -s tests          # regras, YouTube, IPC, base do Discord, Minecraft
bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid --unshare-net \
      --tmpfs /run/user/$(id -u) --tmpfs /tmp --die-with-parent \
      python3 tests/e2e_fake_discord.py        # o daemon inteiro contra um Discord falso
```

O segundo roda o daemon de verdade contra um socket de IPC falso, sem ver o
Discord real nem a rede, e confere a detecção, o tempo de jogo e a presença
apagada ao fechar.

### Gerar os pacotes

```bash
packaging/build.sh              # tudo o que a máquina permitir
packaging/build.sh rpm deb      # só alguns formatos
```

Sai em `dist/`. Não precisa de root, e a máquina que constrói não precisa ser
da mesma distro do pacote — `.rpm`, `.deb` e o do pacman são árvores de
arquivos com metadados. O `.AppImage` baixa o `appimagetool` na primeira vez.

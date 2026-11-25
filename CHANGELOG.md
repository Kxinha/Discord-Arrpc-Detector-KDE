# Changelog

Release notes for each version. GitHub Actions publishes the section of a
version as the notes of its release (see docs/RELEASING.md). Older versions
are described on the [releases page](https://github.com/gabrielmf1998/Discord-Arrpc-Detector-KDE/releases).

## 1.5.5

The real reason YouTube kept disappearing.

* Chrome has a single media player (MPRIS) for all tabs, and it goes to the last tab that made a sound. A page that uses Web Audio (Agents Web, WhatsApp Web and other chat sites) playing a short notification takes the player and keeps it, showing "Paused" or "Stopped" with no title, while YouTube goes on playing in its tab. Measured in Chrome 158: only pressing play on the video again gives the player back, pausing does not. Now the video that was playing keeps the presence while its tab is still open on the same video, its time has not run out, and no other media is really playing for more than 20 s.
* A different title on a paused player is no longer taken as an ad: ads play, a paused title is another tab holding the player.
* The 1.5.4 muted video workaround is gone. Muting was not the cause (Chrome keeps publishing muted videos, verified), and the workaround could keep a paused video on the profile.

* O YouTube sumia porque outra aba (Agents, WhatsApp Web) tocava um aviso e pegava o único player de mídia do Chrome. Agora o vídeo que estava tocando segura a presença enquanto a aba dele continua aberta e o tempo não acabou. Mute nunca foi o problema, e o remendo da 1.5.4 saiu.

## 1.5.4

Muted YouTube videos show up again.

* Chrome only publishes a media player (MPRIS) for media with sound, so a muted video vanished from the presence, and in 1.5.3 nothing was shown at all. Now a YouTube video open in a visible Chrome window counts as watched even without sound: title, channel, cover and the time since it was opened. It stops after the length of the video (plus 2 min), or when the window is minimized or shows another page.

* Vídeo do YouTube mutado volta a aparecer: o Chrome não publica player de mídia sem som, então agora a janela visível com o vídeo aberto basta.

## 1.5.3

YouTube detection is faster and stops crying wolf.

* **Real time.** YouTube and YouTube Music are read every 2 s instead of 5 s, so a new video shows up in about 2 s. Updates are still spaced at least 4 s apart, under Discord's limit of 5 per 20 s.
* **No more "On channel <video title>".** The daemon read the saved session of Chrome profiles that were not even open, and mixed an old channel tab from them with the title of the window you are looking at. Now only profiles the browser has loaded count.
* **Far fewer false "Ad".** A title that does not match the tab is no longer called an ad when it is a real video: one Chrome knows from history (another tab, the next video, a thumbnail preview) or the one the window itself shows. The window title from KWin also identifies YouTube on its own while Chrome has not saved the session yet.
* **YouTube Premium.** New tray option *Presence details > YouTube > I have YouTube Premium* (config `"premium": true`): nothing is ever shown as an ad.

* Detecção do YouTube em tempo real (2 s), sem "No canal <título do vídeo>" vindo de perfil do Chrome fechado, bem menos "Anúncio" falso, e opção YouTube Premium na bandeja.

## 1.5.2

The AUR recipe is gone: the project is distributed only through the releases on GitHub and GitLab (script installer, `.pkg.tar.zst`, `.deb`, `.rpm`, AppImage). Nothing changes for an existing install.

* A receita do AUR foi removida: o projeto sai só pelas releases do GitHub e do GitLab.

## 1.5.1

Counter-Strike 2: the Game State Integration file is now written when the daemon starts, so map and score show up the first time you open CS2 (1.5.0 only wrote it with the game already open, which meant opening CS2 twice).

* O arquivo do CS2 agora é gravado quando o daemon inicia: mapa e placar já aparecem na primeira vez que você abre o jogo.

## 1.5.0

Counter-Strike 2 with map and score, Twitch, Netflix, Crunchyroll and Spotify in the browser, your own apps from a window, and the tray now says when Discord is the problem.

```bash
curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Discord-Arrpc-Detector-KDE/main/install.sh | bash
```

Already on 1.4.x? The tray offers the update.

### New

* **Counter-Strike 2**: mode and map ("Competitivo · Mirage"), your team's score, your K/A/D, warmup and end of match, through Valve's official Game State Integration. The config file is written to every CS2 install found; restart CS2 once. Only 127.0.0.1, with a random token.
* **Twitch, Netflix, Crunchyroll and Spotify Web** in Chrome, Chromium, Brave, Vivaldi, Edge and Firefox. They are not in Discord's list, so each needs an Application ID of your own; the tray shows "(needs an Application ID)" and takes it in two clicks.
* **Add your own app…**: a small window to pick a running program, write the text and paste an Application ID. It checks the ID with Discord and shows "Friends will see: Playing *name*" before saving. **Edit an app** changes it later.
* **Discord status in the tray**: "Discord: Vesktop ready", or "⚠ Discord not found" / "⚠ Vesktop has Rich Presence off" with **How to fix this**, instead of a tray that looks broken.
* **Report a problem**: opens a GitHub issue already filled with version, distro, desktop, Discord client and the last log lines, or copies the same report. Titles, links, quoted text and your home path are removed first, and nothing is sent until you press Create.
* README in English (Portuguese kept in README.pt-BR.md), MIT LICENSE file, issue templates.
* GitHub Actions: tests, the daemon against a fake Discord, the installer and every package on each push; a tag publishes the release. Releases no longer depend on one PC.

### Em português

CS2 com mapa e placar, Twitch, Netflix, Crunchyroll e Spotify no navegador, janela para adicionar um app seu, aviso na bandeja quando o problema é o Discord (com o caminho para ligar o Rich Presence no Vesktop) e **Report a problem**, que abre a issue já preenchida sem títulos, links nem caminhos pessoais.

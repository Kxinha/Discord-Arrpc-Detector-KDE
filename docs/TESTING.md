# Tested distributions

Version **1.5.8**, run on 2026-11-25 with `tests/distros/run.sh`.

Each distro runs in a container with **systemd as PID 1**, a normal user with a real `systemctl --user` manager and session bus, and a headless X server. The app is installed the two ways the README offers (the `curl | bash` installer and the native package), then used the way a person uses it: a fake game is started and must reach a fake Discord socket, the tray must register with a StatusNotifier host (the protocol Plasma uses) and its menu is clicked over D-Bus. Nothing touches a real Discord account.

**232 checks passed, 0 failed.**

| Check | arch<br>script | arch<br>package | ubuntu-24.04<br>script | ubuntu-24.04<br>package | ubuntu-26.04<br>script | ubuntu-26.04<br>package | debian-13<br>script | debian-13<br>package | fedora<br>script | fedora<br>package | opensuse-tumbleweed<br>script | ubuntu-24.04-vm<br>script |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| installer (curl, then bash) exits 0 | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | ✅ |
| installer verified sha256 | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | ✅ |
| dependencies installed with the distro's package manager | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | ✅ |
| daemon and tray units enabled | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | ✅ |
| daemon runs with this python | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ss (iproute2) present | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| busctl present | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| user service starts (systemctl --user) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| publishes a running game to Discord | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| status file says what is published | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| clears the presence when the game closes | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PyGObject + GTK3 import | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| tray icon registers with the panel (StatusNotifier) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| tray menu readable over dbusmenu | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| menu has Pause, Apps and Quit | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Jogo Falso listed under Apps | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| clicking Pause > 15 minutes pauses the daemon | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Resume brings it back | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| application menu entry created and valid | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | ✅ |
| second tray exits instead of adding an icon | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| unit tests with the distro's python | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | ✅ |
| native package installs with its dependencies | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | · |
| package ships the application menu entry | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | ✅ | · | · |

## Run it yourself

```sh
packaging/build.sh
tests/distros/run.sh            # all
tests/distros/run.sh fedora     # one
```

Needs podman (rootless is enough) or docker. Logs land in `tests/distros/out/`.


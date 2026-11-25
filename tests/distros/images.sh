# Distros of the matrix: name|base image|packages for the test box (systemd as
# PID 1, sudo, a user session bus, Xvfb for the tray). The app's own
# (dbus-broker fails under rootless podman, Arch gets the classic dbus-daemon
# units for the box itself) dependencies are NOT here: the installer has to find and install them.
DISTROS=(
  "arch|docker.io/library/archlinux:latest|pacman -Syu --noconfirm --needed sudo xorg-server-xvfb desktop-file-utils && pacman -Rdd --noconfirm dbus-broker-units && pacman -S --noconfirm dbus-daemon-units"
  "ubuntu-24.04|docker.io/library/ubuntu:24.04|apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd systemd-sysv dbus-user-session sudo xvfb desktop-file-utils ca-certificates"
  "ubuntu-26.04|docker.io/library/ubuntu:26.04|apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd systemd-sysv dbus-user-session sudo xvfb desktop-file-utils ca-certificates"
  "debian-13|docker.io/library/debian:13|apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd systemd-sysv dbus-user-session sudo xvfb desktop-file-utils ca-certificates"
  "fedora|registry.fedoraproject.org/fedora:latest|dnf install -y systemd sudo xorg-x11-server-Xvfb desktop-file-utils"
  "opensuse-tumbleweed|registry.opensuse.org/opensuse/tumbleweed:latest|zypper --non-interactive install systemd sudo dbus-1 xorg-x11-server-Xvfb desktop-file-utils"
)

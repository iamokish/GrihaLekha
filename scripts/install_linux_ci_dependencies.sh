#!/usr/bin/env bash
# Ubuntu 22.04 CI/build hosts. Run with sudo; the app itself runs unprivileged.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential patchelf pkg-config binutils dpkg-dev desktop-file-utils \
  xz-utils zstd \
  libglib2.0-0 libdbus-1-3 libfontconfig1 libfreetype6 libnss3 libnspr4 \
  libasound2 libcups2 libgl1 libegl1 libopengl0 libdrm2 \
  libx11-6 libx11-xcb1 libxext6 libxfixes3 libxrender1 libxi6 libxrandr2 \
  libxdamage1 libxcomposite1 libxtst6 libxcb1 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-shape0 libxcb-randr0 libxcb-xfixes0 libxcb-sync1 libxcb-shm0 \
  libxcb-xkb1 libxkbcommon0 libxkbcommon-x11-0 libwayland-client0

# Cross-build environment for the Pi Zero 2 W (Debian 13 trixie, arm64).
# Same Debian release as the Pi, so library versions match what the Pi has installed.
FROM debian:trixie
RUN dpkg --add-architecture arm64 \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      crossbuild-essential-arm64 cmake make git ca-certificates protobuf-compiler \
      libboost-dev:arm64 libboost-system-dev:arm64 libboost-log-dev:arm64 \
      libboost-thread-dev:arm64 libboost-filesystem-dev:arm64 \
      libprotobuf-dev:arm64 libssl-dev:arm64 libusb-1.0-0-dev:arm64 \
 && rm -rf /var/lib/apt/lists/*

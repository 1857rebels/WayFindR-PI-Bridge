#!/bin/sh
# Runs inside the cross-build container. /src = this folder's parent (scratchpad), /work = build volume.
set -e
cd /work
[ -d aasdk ] || git clone -q --depth 1 -b develop https://github.com/openDsh/aasdk.git
# OpenSSL 3 removed FIPS_mode_set(); only call it on older OpenSSL.
grep -q "0x30000000L" aasdk/src/Transport/SSLWrapper.cpp || sed -i \
  's/^    FIPS_mode_set(0);$/#if OPENSSL_VERSION_NUMBER < 0x30000000L\n    FIPS_mode_set(0);\n#endif/' \
  aasdk/src/Transport/SSLWrapper.cpp
cmake -S aasdk -B aasdk/build -DCMAKE_TOOLCHAIN_FILE=/src/xbuild/aarch64.cmake -DCMAKE_BUILD_TYPE=Release
cmake --build aasdk/build -j8
cmake -S /src/aa-headless -B hu-build -DCMAKE_TOOLCHAIN_FILE=/src/xbuild/aarch64.cmake -DAASDK_DIR=/work/aasdk
cmake --build hu-build -j8
mkdir -p /src/xbuild/out
cp -a aasdk/lib/libaasdk*.so* hu-build/aa-headless /src/xbuild/out/
file /src/xbuild/out/* 2>/dev/null || ls -la /src/xbuild/out

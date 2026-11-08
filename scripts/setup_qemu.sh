#!/usr/bin/env bash
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
#
# Fetch (if needed), verify, extract and configure QEMU with the FlipperEmu
# overlay (qemu/hw/arm/flipper -> <qemu>/hw/arm/flipper, symlinked so edits in
# this repo are picked up by `ninja` directly).
#
#   scripts/setup_qemu.sh            # configure (first time) and build
#   scripts/setup_qemu.sh --build    # just rebuild
set -euo pipefail

QEMU_VERSION="${QEMU_VERSION:-11.1.3}"
QEMU_SHA256="5a8289c7940dc77d3873c81a6c29ba3afe1446fc9d36e77fa1658cdbecb84b22"
# QEMU release signing key (Michael Roth), used if gpg is available.
QEMU_KEY="CEACC9E15534EBABB82D3FA03353C9CEF108B584"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TP="$ROOT/third_party"
SRC="$TP/qemu-$QEMU_VERSION"
BUILD="$ROOT/build/qemu"
TARBALL="$TP/qemu-$QEMU_VERSION.tar.xz"

if [[ "${1:-}" == "--build" ]]; then
    exec ninja -C "$BUILD" qemu-system-arm
fi

mkdir -p "$TP" "$BUILD"

if [[ ! -d "$SRC" ]]; then
    if [[ ! -f "$TARBALL" ]]; then
        echo ">> downloading QEMU $QEMU_VERSION"
        curl -sSfL -o "$TARBALL" "https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz"
        curl -sSfL -o "$TARBALL.sig" "https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz.sig"
    fi
    echo "$QEMU_SHA256  $TARBALL" | sha256sum -c -
    if command -v gpg >/dev/null && [[ -f "$TARBALL.sig" ]]; then
        GNUPGHOME="$(mktemp -d)"; export GNUPGHOME
        if gpg --batch -q --keyserver hkps://keys.openpgp.org --recv-keys "$QEMU_KEY" 2>/dev/null; then
            gpg --batch --verify "$TARBALL.sig" "$TARBALL"
        else
            echo ">> (gpg key fetch failed; relying on the pinned SHA-256)"
        fi
    fi
    echo ">> extracting"
    tar -C "$TP" -xf "$TARBALL"
fi

# --- overlay: symlink our sources and hook them into the arm build ---------
ln -sfn "$ROOT/qemu/hw/arm/flipper" "$SRC/hw/arm/flipper"
grep -q "subdir('flipper')" "$SRC/hw/arm/meson.build" ||
    sed -i "s/^hw_common_arch += {'arm': arm_common_ss}/subdir('flipper')\n\n&/" "$SRC/hw/arm/meson.build"
grep -q "source flipper/Kconfig" "$SRC/hw/arm/Kconfig" ||
    printf '\nsource flipper/Kconfig\n' >> "$SRC/hw/arm/Kconfig"

# --- fixes to upstream QEMU (applied once; see each patch header) ----------
for p in "$ROOT"/qemu/patches/*.patch; do
    [[ -e "$p" ]] || continue
    if patch -d "$SRC" -p1 -R --dry-run -s -f < "$p" >/dev/null 2>&1; then
        continue                                    # already applied
    fi
    echo ">> applying $(basename "$p")"
    patch -d "$SRC" -p1 -s -f < "$p"
done

if [[ ! -f "$BUILD/build.ninja" ]]; then
    echo ">> configuring"
    (cd "$BUILD" && "$SRC/configure" \
        --target-list=arm-softmmu \
        --enable-gtk \
        --enable-fdt=internal \
        --enable-trace-backends=log \
        --disable-docs \
        --disable-guest-agent \
        --disable-xen \
        --disable-werror)
fi

echo ">> building"
ninja -C "$BUILD" qemu-system-arm qemu-img
echo ">> $BUILD/qemu-system-arm"

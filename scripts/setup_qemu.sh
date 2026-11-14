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

# ninja runs the build's own meson whenever the build files are to be made
# again, and that meson lives in a Python venv that QEMU's configure made.
# An upgrade of the system's Python (3.14 to 3.15, say) leaves the venv's
# packages in the old version's folder, and the meson there stops running:
# "No module named 'mesonbuild'". Configure again then, with the options of
# the first time (config.status has them): it makes the venv anew.
revive_venv() {
    local meson="$BUILD/pyvenv/bin/meson"
    if [[ -e "$meson" ]] && ! "$meson" --version >/dev/null 2>&1; then
        echo ">> the build's meson does not run (Python upgraded?): configuring again"
        (cd "$BUILD" && ./config.status)
    fi
}

if [[ "${1:-}" == "--build" ]]; then
    revive_venv
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

# --- fixes to upstream QEMU (applied once; see each patch header) ----------
# Which of them does the tree have?  A patch that a later one builds on in the
# same file (0007 under 0008) no longer comes out by itself, so asking each
# patch alone takes it for missing and applies it a second time.  They are
# taken out of a copy of the files they touch instead, the last one first:
# what does not come out there is what the tree still lacks.
start_over() {                                      # $1: what is wrong
    {
        echo ">> $1"
        echo "   $SRC"
        echo "   is not as this script leaves it, and was not changed now. Remove"
        echo "   that folder and run this script again: it extracts the tarball"
        echo "   beside it, applies every patch and builds (about 6 min)."
    } >&2
    exit 1
}
patches=()
for p in "$ROOT"/qemu/patches/*.patch; do
    [[ -e "$p" ]] && patches+=("$p")
done
if ((${#patches[@]})); then
    copy="$(mktemp -d)"                             # two copies: out/ and in/
    trap 'rm -rf "$copy"' EXIT
    while read -r f; do
        # the rejects of a patch that did not fit: this script as it was until
        # 2026-11-11 left them when it was run on a tree that had 0008
        [[ -e "$SRC/$f.rej" ]] &&
            start_over "$f.rej: a patch was applied to that file in part."
        [[ -e "$SRC/$f" ]] || continue
        for c in out in; do
            mkdir -p "$copy/$c/$(dirname "$f")"
            cp "$SRC/$f" "$copy/$c/$f"
        done
    done < <(sed -n 's|^+++ b/\([^[:space:]]*\).*|\1|p' "${patches[@]}" | sort -u)
    todo=()
    for ((i = ${#patches[@]} - 1; i >= 0; i--)); do
        p="${patches[i]}"
        if patch -d "$copy/out" -p1 -R -s -f --dry-run < "$p" >/dev/null 2>&1; then
            patch -d "$copy/out" -p1 -R -s -f < "$p" >/dev/null
        else
            todo=("$p" ${todo[@]+"${todo[@]}"})
        fi
    done
    # what is to be applied goes into the other copy first: a patch that does
    # not fit must not leave the tree with half of itself, or of the series
    for p in ${todo[@]+"${todo[@]}"}; do
        patch -d "$copy/in" -p1 -s -f < "$p" >/dev/null 2>&1 ||
            start_over "$(basename "$p") is not in the tree and does not fit it."
    done
    # (no backup of a file whose patch went in some lines from where it says:
    # a tree that lacks an earlier patch to a file and has a later one)
    for p in ${todo[@]+"${todo[@]}"}; do
        echo ">> applying $(basename "$p")"
        patch -d "$SRC" -p1 -s -f --no-backup-if-mismatch < "$p"
    done
    rm -rf "$copy"
    trap - EXIT
fi

# --- overlay: symlink our sources and hook them into the arm build ---------
ln -sfn "$ROOT/qemu/hw/arm/flipper" "$SRC/hw/arm/flipper"
grep -q "subdir('flipper')" "$SRC/hw/arm/meson.build" ||
    sed -i "s/^hw_common_arch += {'arm': arm_common_ss}/subdir('flipper')\n\n&/" "$SRC/hw/arm/meson.build"
grep -q "source flipper/Kconfig" "$SRC/hw/arm/Kconfig" ||
    printf '\nsource flipper/Kconfig\n' >> "$SRC/hw/arm/Kconfig"

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
revive_venv

echo ">> building"
ninja -C "$BUILD" qemu-system-arm qemu-img
echo ">> $BUILD/qemu-system-arm"

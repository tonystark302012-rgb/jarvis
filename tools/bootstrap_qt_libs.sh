#!/usr/bin/env bash
# ── JARVIS Qt runtime bootstrap ──────────────────────────────────────────────
# PyQt6 wheels link system libraries that do NOT ship in the wheel and are not
# installable here (no apt in this sandbox): libGL, libEGL, libxkbcommon,
# libdbus-1, libpulse, libdrm, libXrandr. This script rebuilds them into
# ${LIBS_ROOT:-$HOME/libs} from github.com sources (the only reachable host)
# plus symbol-driven shims for daemon-less libraries — a shim means exactly
# what a real client library would conclude in this sandbox: "no audio server,
# no X randr monitors", never fake success.
#
# Idempotent. Re-run after a workspace wipe:
#   bash tools/bootstrap_qt_libs.sh            # reuse sources
#   bash tools/bootstrap_qt_libs.sh --force    # re-clone everything
# Then run any Qt entrypoint with:
#   LD_LIBRARY_PATH=$HOME/libs/lib:$HOME/libs/lib/x86_64-linux-gnu
# (tools/ui_preview.py, tools/ui_smoke.py, pytest — export it in the shell).
set -euo pipefail

ROOT="${LIBS_ROOT:-$HOME/libs}"
BUILD="$HOME/build"
VENV_PY="${VENV_PY:-/tmp/v/bin/python}"
[ -x "$VENV_PY" ] || VENV_PY="$(command -v python3)"
export PATH="/tmp/v/bin:$PATH"
LIB="$ROOT/lib"
MULTI="$ROOT/lib/x86_64-linux-gnu"
mkdir -p "$LIB" "$BUILD"
[ "${1:-}" = "--force" ] && rm -rf "$BUILD"/libglvnd "$BUILD"/libxkbcommon \
  "$BUILD"/xkeyboard-config "$BUILD"/drm "$ROOT"

HAVE=""
probe_ok() { # name — accepts lib/ or multiarch libdir
  [ -e "$LIB/$1" ] || [ -e "$MULTI/$1" ]
}
gl_ok() { # libGL shim must carry the full glX surface, not a partial regen
  [ -e "$LIB/libGL.so.1" ] && nm -D "$LIB/libGL.so.1" 2>/dev/null | grep -q glXMakeCurrent
}
for name in libdrm.so.2 libxkbcommon.so.0 libdbus-1.so.3 libpulse.so.0 \
            libXrandr.so.2; do
  probe_ok "$name" && HAVE="$HAVE $name"
done
gl_ok && HAVE="$HAVE libGL.so.1"
if [ "$(echo "$HAVE" | wc -w)" -ge 6 ]; then
  echo "qt-libs: already present under $ROOT — ok"
  exit 0
fi

clone() { # clone <github/owner/repo> <dir>
  [ -d "$BUILD/$2" ] || git clone -q --depth 1 "https://github.com/$1.git" "$BUILD/$2"
}

echo "qt-libs: [1/6] libglvnd (libGL+libEGL dispatch, no X11)…"
clone NVIDIA/libglvnd libglvnd
( cd "$BUILD/libglvnd" && \
  { [ -d build ] || meson setup build --prefix="$ROOT" -Dx11=disabled >/dev/null; } && \
  ninja -C build >/dev/null && ninja -C build install >/dev/null )

echo "qt-libs: [2/6] libxkbcommon…"
clone xkbcommon/libxkbcommon libxkbcommon
( cd "$BUILD/libxkbcommon" && \
  { [ -d build ] || meson setup build --prefix="$ROOT" \
    -Denable-x11=false -Denable-wayland=false -Denable-tools=false \
    -Denable-parser-auto-generation=false -Denable-xkbregistry=false \
    -Denable-bash-completion=false -Denable-zsh-completion=false \
    -Dxkb-config-root="$ROOT/share/X11/xkb" >/dev/null; } && \
  ninja -C build >/dev/null && ninja -C build install >/dev/null )

echo "qt-libs: [3/6] xkeyboard-config data (keymap tree)…"
clone alexeyten/xkeyboard-config xkeyboard-config
mkdir -p "$ROOT/share/X11/xkb"
for d in compat geometry keycodes rules symbols types compositor; do
  [ -d "$BUILD/xkeyboard-config/$d" ] && \
    rm -rf "$ROOT/share/X11/xkb/$d" && \
    cp -r "$BUILD/xkeyboard-config/$d" "$ROOT/share/X11/xkb/"
done

echo "qt-libs: [4/6] libdrm (real, core-only)…"
clone sailfishos-mirror/drm drm
( cd "$BUILD/drm" && \
  { [ -d build ] || meson setup build --prefix="$ROOT" -Dtests=false -Dudev=false \
    -Dman-pages=disabled -Dvalgrind=disabled -Dcairo-tests=disabled \
    -Dintel=disabled -Dradeon=disabled -Damdgpu=disabled -Dnouveau=disabled \
    -Dvmwgfx=disabled -Domap=disabled -Dexynos=disabled -Dtegra=disabled \
    -Dvc4=disabled -Detnaviv=disabled >/dev/null; } && \
  ninja -C build >/dev/null && ninja -C build install >/dev/null )

# Qt's own libs tell us exactly which symbols the shims must export — the
# generators below are driven by `nm` on the installed wheel, so they track
# wheel upgrades instead of hard-coding names.
QT_LIB="$("$VENV_PY" -c 'import PyQt6, os; print(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "lib"))' 2>/dev/null || true)"
[ -d "$QT_LIB" ] || { echo "qt-libs: PyQt6 not installed — skip shims"; exit 0; }

echo "qt-libs: [5/6] shims: libdbus-1, libpulse, libGL, libXrandr…"
"$VENV_PY" - "$QT_LIB" "$BUILD" <<'PYEOF'
import collections, glob, os, re, subprocess, sys
qt, build = sys.argv[1], sys.argv[2]

def undefined(pattern):
    """{symbol: version} from every Qt lib matching glob pattern."""
    out = {}
    for f in glob.glob(os.path.join(qt, pattern)):
        r = subprocess.run(["nm", "-D", "--undefined-only", f],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            m = re.search(r"\bU\s+(\S+)", line)
            if not m:
                continue
            tok = m.group(1)
            if "@" in tok:
                n, v = tok.split("@", 1)
            else:
                n, v = tok, None
            out[n] = v
    return out

def write_shim(cname, mapname, soname, funcs, versions):
    src = ["#include <stddef.h>"] + funcs
    open(os.path.join(build, cname), "w").write("\n".join(src) + "\n")
    by_ver = collections.OrderedDict()
    for name, ver in versions.items():
        by_ver.setdefault(ver or "*", set()).add(name)
    blocks = []
    for ver, names in by_ver.items():
        node = "BASE" if ver == "*" else re.sub(r"[^A-Za-z0-9_]", "_", ver)
        blocks.append(f"{node} {{\n  global:\n" +
                      "".join(f"    {n};\n" for n in sorted(names)) +
                      "  local: *;\n};")
    open(os.path.join(build, mapname), "w").write("\n".join(blocks) + "\n")

# ── libdbus-1.so.3: Qt6DBus → 89 C symbols; NULL/error == "no session bus" ──
db = {n: v for n, v in undefined(os.path.join(qt, "libQt6DBus.so.6")).items()
      if "dbus" in n}
funcs = [f"long {n}(long a,long b,long d,long e,long f,long g){{return 0;}}"
         for n in sorted(db)]
write_shim("stub_dbus.c", "stub_dbus.map", "libdbus-1.so.3", funcs, db)

# ── libpulse.so.0: FFmpeg backend; NULL == "no PulseAudio daemon" ──────────
pulse = {}
for pat in ("libQt6FFmpegStub*.so.*", "libQt6Multimedia*.so*",
            os.path.join("..", "plugins", "multimedia", "*")):
    for n, v in undefined(os.path.join(qt, pat)).items():
        if n.startswith("pa_"):
            pulse[n] = v
NULLS = {"pa_context_new", "pa_context_new_with_proplist", "pa_mainloop_new",
         "pa_threaded_mainloop_new", "pa_stream_new",
         "pa_stream_new_with_proplist", "pa_proplist_new"}
MINUS1 = {"pa_context_connect", "pa_stream_connect_playback",
          "pa_stream_connect_record", "pa_threaded_mainloop_start",
          "pa_stream_write"}
SPECIAL = {"pa_context_errno": "return 1;",           # PA_ERR_INVALID
           "pa_frame_size": "return 8;",              # no div-by-zero
           "pa_sample_spec_valid": "return 0;",
           "pa_sample_format_valid": "return 0;",
           "pa_strerror": "return \"no audio server (lib)\";"}
funcs = []
for n in sorted(pulse):
    if n in SPECIAL:
        rt = "const char*" if n == "pa_strerror" else "long"
        funcs.append(f"{rt} {n}(long a,long b,long d){{ {SPECIAL[n]} }}")
    elif n in NULLS:
        funcs.append(f"void* {n}(long a,long b,long d){{ return NULL; }}")
    elif n in MINUS1:
        funcs.append(f"long {n}(long a,long b,long d){{ return -1; }}")
    else:
        funcs.append(f"long {n}(long a,long b,long d){{ return 0; }}")
write_shim("stub_pulse.c", "stub_pulse.map", "libpulse.so.0", funcs, pulse)

# ── libGL.so.1: glX* stubs (no X server) + gl* re-exported via libOpenGL ───
gui = {}
# plugins live NEXT to lib/ (Qt6/plugins), not inside it — glob both trees
for pat in ("libQt6*.so*", os.path.join("..", "plugins", "platforms", "*.so*"),
            os.path.join("..", "plugins", "multimedia", "*")):
    for n, v in undefined(os.path.join(qt, pat)).items():
        if re.match(r"^(glX|gl[A-Z])", n):
            gui[n] = v
funcs = []
for n in sorted(gui):
    if n.startswith("glXGetProc"):
        funcs.append(f"void* {n}(const char* p){{(void)p;return NULL;}}")
    else:
        funcs.append(f"long {n}(long a,long b,long d,long e,long f,long g)"
                     "{return 0;}")
write_shim("shim_gl.c", "shim_gl.map", "libGL.so.1", funcs, gui)

# ── libXrandr.so.2: XRRGetMonitors==NULL ("headless: no monitors") ─────────
open(os.path.join(build, "shim_xrandr.c"), "w").write(
    "void* XRRGetMonitors(long a,long b,long c,long d){return 0;}\n"
    "void XRRFreeMonitors(void* m){(void)m;}\n")
print("shims generated:",
      len(db), "dbus +", len(pulse), "pulse +", len(gui), "gl/glX")
PYEOF

LDFLAGS_COMMON="-shared -fPIC -O2"
gcc $LDFLAGS_COMMON -o "$LIB/libdbus-1.so.3" "$BUILD/stub_dbus.c" \
  -Wl,-soname,libdbus-1.so.3 -Wl,--version-script="$BUILD/stub_dbus.map"
gcc $LDFLAGS_COMMON -o "$LIB/libpulse.so.0" "$BUILD/stub_pulse.c" \
  -Wl,-soname,libpulse.so.0 -Wl,--version-script="$BUILD/stub_pulse.map"
gcc $LDFLAGS_COMMON -o "$LIB/libGL.so.1" "$BUILD/shim_gl.c" \
  -Wl,-soname,libGL.so.1 -Wl,--no-as-needed -L"$MULTI" \
  -l:libOpenGL.so.0 -l:libEGL.so.1
gcc $LDFLAGS_COMMON -o "$LIB/libXrandr.so.2" "$BUILD/shim_xrandr.c" \
  -Wl,-soname,libXrandr.so.2

echo "qt-libs: [6/6] verify…"
export LD_LIBRARY_PATH="$LIB:$MULTI"
if [ -f "$QT_LIB/libQt6Widgets.so.6" ]; then
  MISSING="$(ldd "$QT_LIB/libQt6Widgets.so.6" | grep 'not found' || true)"
  [ -z "$MISSING" ] || { echo "qt-libs: still missing:"; echo "$MISSING"; exit 1; }
fi
QT_QPA_PLATFORM=offscreen "$VENV_PY" - <<'PYEOF'
import sys
from PyQt6.QtWidgets import QApplication, QLabel
app = QApplication(sys.argv)
QLabel("ok").show()
print("qt-libs: OK — PyQt6 offscreen works")
PYEOF

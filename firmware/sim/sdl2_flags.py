# PlatformIO pre-build script for the `sim` env: find SDL2 and add its
# compiler/linker flags. Replaces the old `!sdl2-config --cflags --libs`
# backtick, which only worked on Linux/macOS - sdl2-config is a shell script
# that cmd.exe cannot run, so the sim env died on Windows before compiling.
#
# Resolution order:
#   Linux / macOS
#     1. sdl2-config --cflags --libs
#     2. pkg-config --cflags --libs sdl2
#     3. $SDL2_DIR  (a prefix holding include/SDL2 + lib)
#   Windows (MinGW-w64 GCC - PlatformIO's native platform is GCC-only)
#     1. %SDL2_DIR%  (an unpacked SDL2-devel-*-mingw bundle, its
#                     x86_64-w64-mingw32 folder, or any prefix with
#                     include/SDL2 + lib + bin/SDL2.dll)
#     2. the prefix of the gcc on PATH  (MSYS2 ucrt64/mingw64 after
#                     `pacman -S mingw-w64-ucrt-x86_64-SDL2`, or a winlibs
#                     prefix with the SDL2 bundle copied in)
#
# On Windows the sim is a console program with its own main(), so SDL2main
# and -mwindows are deliberately NOT linked (they would rename main and hide
# stdout); SDL_MAIN_HANDLED is defined instead and SDL2.dll is copied next to
# the executable after linking so it runs from any shell.

import os
import shutil
import subprocess
import sys

Import("env")  # noqa: F821  (SCons injects this)

IS_WIN = sys.platform.startswith("win")


def run(cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).split()
    except (OSError, subprocess.CalledProcessError):
        return None


def sdl_prefix(root):
    """Return (include, lib, bin) if `root` (or a mingw triplet subdir of it)
    holds an SDL2 development install."""
    if not root:
        return None
    for sub in ("", "x86_64-w64-mingw32"):
        base = os.path.join(root, sub) if sub else root
        inc = os.path.join(base, "include", "SDL2")
        lib = os.path.join(base, "lib")
        if os.path.isfile(os.path.join(inc, "SDL.h")) and os.path.isdir(lib):
            return inc, lib, os.path.join(base, "bin")
    return None


def gcc_prefix():
    gcc = shutil.which("gcc")
    return os.path.dirname(os.path.dirname(gcc)) if gcc else None


def fail(msg):
    sys.stderr.write(
        "\n[sim] %s\n"
        "      Linux:   sudo apt install libsdl2-dev\n"
        "      macOS:   brew install sdl2\n"
        "      Windows: MSYS2 -> pacman -S mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-SDL2\n"
        "               and put C:/msys64/ucrt64/bin on PATH; or unpack a\n"
        "               SDL2-devel-<ver>-mingw.zip and set SDL2_DIR to it\n"
        "               (see SIM-USAGE.md).\n\n" % msg
    )
    env.Exit(1)


if not IS_WIN:
    flags = run(["sdl2-config", "--cflags", "--libs"]) or run(
        ["pkg-config", "--cflags", "--libs", "sdl2"]
    )
    if flags:
        env.MergeFlags(" ".join(flags))
    else:
        found = sdl_prefix(os.environ.get("SDL2_DIR"))
        if not found:
            fail("SDL2 not found (no sdl2-config / pkg-config sdl2 / SDL2_DIR).")
        inc, lib, _ = found
        env.Append(CPPPATH=[inc], LIBPATH=[lib], LIBS=["SDL2"])
else:
    if not shutil.which("gcc"):
        fail("no gcc on PATH - the sim needs a MinGW-w64 GCC (PlatformIO's "
             "native platform does not use MSVC).")
    found = sdl_prefix(os.environ.get("SDL2_DIR")) or sdl_prefix(gcc_prefix())
    if not found:
        fail("SDL2 not found next to gcc and SDL2_DIR is not set.")
    inc, lib, bindir = found
    env.Append(
        CPPPATH=[inc],
        CPPDEFINES=[
            "SDL_MAIN_HANDLED",
            # MinGW only declares gmtime_r/localtime_r behind this POSIX
            # feature macro; shared ui.cpp uses gmtime_r.
            ("_POSIX_THREAD_SAFE_FUNCTIONS", "200112L"),
        ],
        LIBPATH=[lib],
        LIBS=["SDL2"],
        # self-contained .exe: fold libgcc, libstdc++ and winpthread into the
        # binary so only SDL2.dll (copied below) and system DLLs are needed
        LINKFLAGS=[
            "-static-libgcc",
            "-static-libstdc++",
            "-Wl,-Bstatic,--whole-archive,-lwinpthread,--no-whole-archive,-Bdynamic",
        ],
    )
    dll = os.path.join(bindir, "SDL2.dll")
    if os.path.isfile(dll):
        def copy_dll(source, target, env):
            shutil.copy2(dll, os.path.dirname(str(target[0])))
        env.AddPostAction("$PROGPATH", env.VerboseAction(copy_dll, "Copying SDL2.dll"))
    else:
        print("[sim] warning: %s not found; put SDL2.dll on PATH to run the program" % dll)

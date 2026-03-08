#!/usr/bin/env python3
"""
RPi Audio Console — Build Script
Packages pc_server.py + pc_ui.html into a single Windows .exe
using PyInstaller. The .exe shows no console window and lives in the system tray.

Usage (run from the folder containing pc_server.py and pc_ui.html):
    pip install pyinstaller pystray pillow
    python build.py

Output:  dist/RPiConsole.exe   (~15-25 MB, fully self-contained)

To auto-start with Windows, drop a shortcut to RPiConsole.exe into:
    shell:startup   (Win + R, type that, press Enter)
"""

import os, sys, shutil, subprocess, textwrap

HERE    = os.path.dirname(os.path.abspath(__file__))
SERVER  = os.path.join(HERE, 'pc_server.py')
UI_HTML = os.path.join(HERE, 'pc_ui.html')
DIST    = os.path.join(HERE, 'dist')
BUILD   = os.path.join(HERE, 'build')
SPEC    = os.path.join(HERE, 'RPiConsole.spec')
HOOK    = os.path.join(HERE, '_hook_stdio.py')

# ── Pre-flight checks ──────────────────────────────────────────────────────
def check():
    ok = True
    for f, label in [(SERVER, 'pc_server.py'), (UI_HTML, 'pc_ui.html')]:
        if not os.path.exists(f):
            print(f'[Error] Missing: {label} (expected at {f})')
            ok = False
    for pkg in ['PyInstaller', 'pystray', 'PIL']:
        try:
            __import__(pkg.lower().replace('pyinstaller','PyInstaller'))
        except ImportError:
            # PyInstaller is imported differently
            pass
    try:
        import PyInstaller
    except ImportError:
        print('[Error] PyInstaller not installed. Run: pip install pyinstaller')
        ok = False
    try:
        import pystray
    except ImportError:
        print('[Error] pystray not installed. Run: pip install pystray')
        ok = False
    try:
        from PIL import Image
    except ImportError:
        print('[Error] Pillow not installed. Run: pip install pillow')
        ok = False
    return ok

# ── Write a minimal .ico file (16x16 cyan disc) for the exe icon ──────────
def make_ico(path):
    try:
        from PIL import Image, ImageDraw
        SIZE = 64
        img  = Image.new('RGBA', (SIZE, SIZE), (0,0,0,0))
        d    = ImageDraw.Draw(img)
        C    = SIZE // 2
        CYAN = (0, 229, 255, 255)
        DIM  = (0, 100, 130, 200)
        BG   = (13, 16, 23, 255)
        d.ellipse([2,2,SIZE-2,SIZE-2], fill=BG, outline=CYAN, width=2)
        for r in [22,16,10]:
            d.ellipse([C-r,C-r,C+r,C+r], outline=DIM, width=1)
        d.ellipse([C-5,C-5,C+5,C+5], fill=CYAN)
        img.save(path, format='ICO', sizes=[(16,16),(32,32),(64,64)])
        print(f'[Build] Icon written: {path}')
        return True
    except Exception as e:
        print(f'[Build] Could not create icon: {e}')
        return False

# ── Write runtime hook — patches sys.stdio before any user imports ─────────
def make_hook(path):
    """PyInstaller --windowed sets sys.stdout/stderr/stdin to None.
    Any print() or faulthandler.enable() before the tray app silences them
    will raise RuntimeError: sys.stderr is None.
    This hook runs first and redirects all three to devnull."""
    with open(path, 'w') as f:
        f.write(textwrap.dedent("""\
            import sys, os, multiprocessing
            # freeze_support must be called before any user code in a frozen exe
            multiprocessing.freeze_support()
            # --windowed exes have sys.stdio = None; redirect to devnull to prevent
            # RuntimeError: sys.stderr is None on any print() or faulthandler.enable()
            if sys.stdout is None:
                sys.stdout = open(os.devnull, 'w')
            if sys.stderr is None:
                sys.stderr = open(os.devnull, 'w')
            if sys.stdin is None:
                sys.stdin = open(os.devnull, 'r')
        """))
    print(f'[Build] Runtime hook written: {path}')

# ── Run PyInstaller ────────────────────────────────────────────────────────
def build():
    ico_path = os.path.join(HERE, 'rpi_console.ico')
    has_ico  = make_ico(ico_path)
    make_hook(HOOK)

    # --add-data bundles pc_ui.html so it's accessible via sys._MEIPASS at runtime
    sep = ';' if sys.platform == 'win32' else ':'

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--onefile',                        # single .exe
        '--windowed',                       # no console window (hides cmd flash)
        '--name', 'RPiConsole',
        f'--add-data={UI_HTML}{sep}.',      # bundle pc_ui.html next to the exe data
        f'--runtime-hook={HOOK}',           # patch sys.stdio before any imports run
        '--hidden-import=pystray._win32',
        '--hidden-import=pycaw',
        '--hidden-import=pycaw.pycaw',
        '--hidden-import=comtypes',
        '--hidden-import=comtypes.client',
        '--hidden-import=winsdk',
        '--hidden-import=flask',
        '--hidden-import=flask_cors',
        '--hidden-import=psutil',
        '--hidden-import=requests',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageDraw',
    ]

    if has_ico:
        cmd += ['--icon', ico_path]

    cmd.append(SERVER)

    print('[Build] Running PyInstaller...')
    print('[Build] This takes ~60-120 seconds on first run.\n')
    result = subprocess.run(cmd, cwd=HERE)

    if result.returncode == 0:
        exe = os.path.join(DIST, 'RPiConsole.exe')
        if os.path.exists(exe):
            size_mb = os.path.getsize(exe) / 1024 / 1024
            print()
            print('╔══════════════════════════════════════════════╗')
            print('║           BUILD SUCCESSFUL ✓                 ║')
            print(f'║  Output: dist/RPiConsole.exe ({size_mb:.1f} MB){"  ║" if size_mb < 10 else " ║" if size_mb < 100 else "║"}')
            print('║                                              ║')
            print('║  To auto-start with Windows:                 ║')
            print('║  1. Press Win+R, type: shell:startup         ║')
            print('║  2. Drop a shortcut to RPiConsole.exe there  ║')
            print('╚══════════════════════════════════════════════╝')
        else:
            print('[Build] PyInstaller finished but .exe not found — check output above.')
    else:
        print(f'[Build] PyInstaller failed with code {result.returncode}')

    # Cleanup
    for p in [SPEC, BUILD, ico_path, HOOK]:
        if os.path.exists(p):
            if os.path.isdir(p): shutil.rmtree(p)
            else: os.remove(p)

if __name__ == '__main__':
    print('RPi Audio Console — Build Script')
    print('─' * 40)
    if not check():
        print('\nFix the errors above then re-run.')
        sys.exit(1)
    build()

# ── Pre-flight checks ──────────────────────────────────────────────────────
def check():
    ok = True
    for f, label in [(SERVER, 'pc_server.py'), (UI_HTML, 'pc_ui.html')]:
        if not os.path.exists(f):
            print(f'[Error] Missing: {label} (expected at {f})')
            ok = False
    for pkg in ['PyInstaller', 'pystray', 'PIL']:
        try:
            __import__(pkg.lower().replace('pyinstaller','PyInstaller'))
        except ImportError:
            # PyInstaller is imported differently
            pass
    try:
        import PyInstaller
    except ImportError:
        print('[Error] PyInstaller not installed. Run: pip install pyinstaller')
        ok = False
    try:
        import pystray
    except ImportError:
        print('[Error] pystray not installed. Run: pip install pystray')
        ok = False
    try:
        from PIL import Image
    except ImportError:
        print('[Error] Pillow not installed. Run: pip install pillow')
        ok = False
    return ok

# ── Write a minimal .ico file (16x16 cyan disc) for the exe icon ──────────
def make_ico(path):
    try:
        from PIL import Image, ImageDraw
        SIZE = 64
        img  = Image.new('RGBA', (SIZE, SIZE), (0,0,0,0))
        d    = ImageDraw.Draw(img)
        C    = SIZE // 2
        CYAN = (0, 229, 255, 255)
        DIM  = (0, 100, 130, 200)
        BG   = (13, 16, 23, 255)
        d.ellipse([2,2,SIZE-2,SIZE-2], fill=BG, outline=CYAN, width=2)
        for r in [22,16,10]:
            d.ellipse([C-r,C-r,C+r,C+r], outline=DIM, width=1)
        d.ellipse([C-5,C-5,C+5,C+5], fill=CYAN)
        img.save(path, format='ICO', sizes=[(16,16),(32,32),(64,64)])
        print(f'[Build] Icon written: {path}')
        return True
    except Exception as e:
        print(f'[Build] Could not create icon: {e}')
        return False

# ── Run PyInstaller ────────────────────────────────────────────────────────
def build():
    ico_path = os.path.join(HERE, 'rpi_console.ico')
    has_ico  = make_ico(ico_path)

    # --add-data bundles pc_ui.html so it's accessible via sys._MEIPASS at runtime
    sep = ';' if sys.platform == 'win32' else ':'

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--onefile',                        # single .exe
        '--windowed',                       # no console window (hides cmd flash)
        '--name', 'RPiConsole',
        f'--add-data={UI_HTML}{sep}.',      # bundle pc_ui.html next to the exe data
        '--hidden-import=pystray._win32',
        '--hidden-import=pycaw',
        '--hidden-import=pycaw.pycaw',
        '--hidden-import=comtypes',
        '--hidden-import=comtypes.client',
        '--hidden-import=winsdk',
        '--hidden-import=flask',
        '--hidden-import=flask_cors',
        '--hidden-import=psutil',
        '--hidden-import=requests',
        '--hidden-import=PIL',
        '--hidden-import=PIL.Image',
        '--hidden-import=PIL.ImageDraw',
    ]

    if has_ico:
        cmd += ['--icon', ico_path]

    cmd.append(SERVER)

    print('[Build] Running PyInstaller...')
    print('[Build] This takes ~60-120 seconds on first run.\n')
    result = subprocess.run(cmd, cwd=HERE)

    if result.returncode == 0:
        exe = os.path.join(DIST, 'RPiConsole.exe')
        if os.path.exists(exe):
            size_mb = os.path.getsize(exe) / 1024 / 1024
            print()
            print('╔══════════════════════════════════════════════╗')
            print('║           BUILD SUCCESSFUL ✓                 ║')
            print(f'║  Output: dist/RPiConsole.exe ({size_mb:.1f} MB){"  ║" if size_mb < 10 else " ║" if size_mb < 100 else "║"}')
            print('║                                              ║')
            print('║  To auto-start with Windows:                 ║')
            print('║  1. Press Win+R, type: shell:startup         ║')
            print('║  2. Drop a shortcut to RPiConsole.exe there  ║')
            print('╚══════════════════════════════════════════════╝')
        else:
            print('[Build] PyInstaller finished but .exe not found — check output above.')
    else:
        print(f'[Build] PyInstaller failed with code {result.returncode}')

    # Cleanup
    for p in [SPEC, BUILD, ico_path]:
        if os.path.exists(p):
            if os.path.isdir(p): shutil.rmtree(p)
            else: os.remove(p)

if __name__ == '__main__':
    print('RPi Audio Console — Build Script')
    print('─' * 40)
    if not check():
        print('\nFix the errors above then re-run.')
        sys.exit(1)
    build()

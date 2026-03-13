#!/usr/bin/env python3
"""
RPi Audio Console — PC Bridge Build Script
Packages pc_bridge.py into a single Windows .exe (no console, system tray only).

Usage:
    pip install pyinstaller pystray pillow websockets pycaw pyaudiowpatch numpy
    python build_pc.py
"""
import os, sys, shutil, subprocess, textwrap
from pathlib import Path

HERE   = Path(__file__).parent
BRIDGE = HERE / 'pc_bridge.py'
HOOK   = HERE / '_hook_stdio.py'


def check():
    ok = True
    if not BRIDGE.exists():
        print(f'[Error] Missing: pc_bridge.py'); ok = False

    # Check PyInstaller via subprocess — its module name casing is unreliable
    r = subprocess.run([sys.executable, '-m', 'PyInstaller', '--version'],
                       capture_output=True)
    if r.returncode != 0:
        print('[Error] PyInstaller not installed — run: pip install pyinstaller')
        ok = False

    # Check pure-Python imports normally
    for display_name, import_name in [('Pillow', 'PIL'), ('pystray', 'pystray'),
                                       ('websockets', 'websockets')]:
        try:
            __import__(import_name)
        except ImportError:
            print(f'[Error] {display_name} not installed — run: pip install {display_name}')
            ok = False
    return ok


def make_ico(path):
    try:
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (64,64), (0,0,0,0))
        d   = ImageDraw.Draw(img)
        d.ellipse([2,2,62,62], fill=(13,16,23), outline=(0,229,255), width=2)
        for r in [22,16,10]:
            d.ellipse([32-r,32-r,32+r,32+r], outline=(0,100,130), width=1)
        d.ellipse([27,27,37,37], fill=(0,229,255))
        img.save(path, format='ICO', sizes=[(16,16),(32,32),(64,64)])
        return True
    except Exception as e:
        print(f'[Build] Icon error: {e}'); return False


def make_hook(path):
    path.write_text(textwrap.dedent("""\
        import sys, os, multiprocessing
        multiprocessing.freeze_support()
        if sys.stdout is None: sys.stdout = open(os.devnull,'w')
        if sys.stderr is None: sys.stderr = open(os.devnull,'w')
        if sys.stdin  is None: sys.stdin  = open(os.devnull,'r')
    """))


def build():
    ico = HERE / 'rpi_console.ico'
    has_ico = make_ico(ico)
    make_hook(HOOK)

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--onefile', '--windowed',
        '--name', 'RPiConsole',
        f'--runtime-hook={HOOK}',
        '--hidden-import=pystray._win32',
        '--hidden-import=pycaw', '--hidden-import=pycaw.pycaw',
        '--hidden-import=comtypes', '--hidden-import=comtypes.client',
        '--hidden-import=winsdk', '--hidden-import=websockets',
        '--hidden-import=numpy',
    ]
    if has_ico:
        cmd += ['--icon', str(ico)]
    cmd.append(str(BRIDGE))

    print('[Build] Running PyInstaller (~60-120s)...')
    r = subprocess.run(cmd, cwd=HERE)

    # Cleanup
    for p in [HERE/'RPiConsole.spec', HERE/'build', ico, HOOK]:
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    if r.returncode == 0:
        exe = HERE / 'dist' / 'RPiConsole.exe'
        if exe.exists():
            mb = exe.stat().st_size / 1024 / 1024
            print(f'\n✓ dist/RPiConsole.exe ({mb:.1f} MB)')
            print('  Win+R → shell:startup → drop shortcut there for auto-start')
    else:
        print(f'[Build] Failed: code {r.returncode}')


if __name__ == '__main__':
    if not check():
        sys.exit(1)
    build()

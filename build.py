"""Build dist/SC-hauling.exe and dist/SC-hauling-<version>-win64.zip.

    python -m venv .venv-build
    .venv-build\\Scripts\\python -m pip install pyinstaller
    .venv-build\\Scripts\\python build.py [--scmap ..\\SC-map\\data]

The exe bundles Python and the SC-map data it needs (places, bodies, ships),
so players need nothing else. Rebuild after SC-map data changes.
"""
import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from schauling import __version__

ROOT = Path(__file__).resolve().parent
DATA_FILES = ["locations.csv", "bodies.csv", "quantum.json"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scmap", default=str(ROOT.parent / "SC-map" / "data"), help="SC-map data folder")
    args = ap.parse_args()

    src = Path(args.scmap)
    data = ROOT / "build" / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in DATA_FILES:
        shutil.copy2(src / name, data / name)

    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "SC-hauling",
        "--add-data", f"{data}{';' if sys.platform == 'win32' else ':'}data",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "run_overlay.py"),
    ], check=True)

    exe = ROOT / "dist" / "SC-hauling.exe"
    archive = ROOT / "dist" / f"SC-hauling-{__version__}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, exe.name)
        for extra in ("README.md", "LICENSE"):
            z.write(ROOT / extra, extra)
    print(f"Built {exe} and {archive}")


if __name__ == "__main__":
    main()

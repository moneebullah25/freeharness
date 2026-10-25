"""Tiny hand-made wheels for offline pip tests (`pip --no-index --find-links <dir>`): no PyPI, no build backend."""
import zipfile
from pathlib import Path


def make_wheel(directory: Path, name: str, version: str = "1.0", body: str = "VALUE = 42\n", scripts: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}.dist-info"
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{name}/__init__.py", body)
        z.writestr(f"{name}/__main__.py", "print('main of ' + __name__)\n")
        z.writestr(f"{name}/cli.py", "def main():\n    print('fhtool 1.0')\n")
        z.writestr(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        z.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{dist}/RECORD", "")
        if scripts:
            z.writestr(f"{dist}/entry_points.txt", "[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in scripts.items()))
    return path


def build_wheelhouse(wh: Path) -> Path:
    for n in ("fhdep", "fhtest", "fhextra", "fhlint", "fhci", "fhdev"):
        make_wheel(wh, n)
    make_wheel(wh, "fhdocs_must_not_install")
    make_wheel(wh, "fhtool", scripts={"fhtool-cli": "fhtool.cli:main"})
    return wh

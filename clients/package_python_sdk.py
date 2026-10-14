"""Build the Xiaoge Python SDK customer delivery package.

Run from the ``clients`` directory:

    python package_python_sdk.py

What it does:

1. Refreshes the content files of ``dist/xiaoge-python-sdk-r5.2.2/`` from the
   repo sources. Package-specific files (``PACKAGE_README.md``, ``README.md``,
   ``requirements.txt``, and the ``examples/``/``tests/`` launcher wrappers)
   are preserved as-is.
2. Removes local artifacts that must never ship (``__pycache__/``).
3. Regenerates ``SHA256SUMS.txt`` and writes ``dist/xiaoge-python-sdk-r5.2.2.zip``.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

CLIENTS = Path(__file__).resolve().parent
PYTHON = CLIENTS / "python"
PKG_DIR = CLIENTS / "dist" / "xiaoge-python-sdk-r5.2.2"
PKG_ZIP = CLIENTS / "dist" / "xiaoge-python-sdk-r5.2.2.zip"

# repo source -> package destination (relative paths)
COPY_FILES: list[tuple[Path, Path]] = [
    (CLIENTS / "PROTOCOL.md", Path("PROTOCOL.md")),
    (CLIENTS / "SDK_TEST_VALIDATION.md", Path("SDK_TEST_VALIDATION.md")),
    (CLIENTS / "certs" / "cloud-ca.pem", Path("certs/cloud-ca.pem")),
    (PYTHON / "PYTHON_SDK_README.md", Path("docs/PYTHON_SDK_README.md")),
    (PYTHON / "xiaoge_client.py", Path("sdk/xiaoge_client.py")),
    (PYTHON / "selftest.py", Path("sdk/selftest.py")),
    (PYTHON / "contract_replay.py", Path("sdk/contract_replay.py")),
    (PYTHON / "demo_file.py", Path("sdk/demo_file.py")),
    (PYTHON / "demo_mic.py", Path("sdk/demo_mic.py")),
]

# package-specific files kept in dist; never overwritten from the repo
PRESERVED = {
    "PACKAGE_README.md",
    "README.md",
    "requirements.txt",
    # examples/ and tests/ are thin launchers that sys.path-insert sdk/
    "examples/demo_file.py",
    "examples/demo_mic.py",
    "tests/selftest.py",
    "tests/contract_replay.py",
}


def refresh_content() -> None:
    for source, relative in COPY_FILES:
        if not source.exists():
            raise SystemExit(f"missing source file: {source}")
        destination = PKG_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        print(f"[copy] {relative}")


def clean_junk() -> None:
    for pycache in PKG_DIR.rglob("__pycache__"):
        shutil.rmtree(pycache)
        print(f"[clean] removed {pycache.relative_to(PKG_DIR)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(PKG_DIR.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            files.append(path)
    return files


def write_checksums() -> None:
    lines = [
        f"{sha256(path)}  {path.relative_to(PKG_DIR).as_posix()}"
        for path in manifest_files()
    ]
    (PKG_DIR / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"[sums] {len(lines)} files -> SHA256SUMS.txt")


def write_zip() -> None:
    if PKG_ZIP.exists():
        PKG_ZIP.unlink()
    with zipfile.ZipFile(PKG_ZIP, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in manifest_files() + [PKG_DIR / "SHA256SUMS.txt"]:
            archive.write(path, path.relative_to(PKG_DIR.parent).as_posix())
    print(f"[zip] {PKG_ZIP} ({PKG_ZIP.stat().st_size} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    refresh_content()
    clean_junk()
    write_checksums()
    write_zip()
    print("[done] python delivery package refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

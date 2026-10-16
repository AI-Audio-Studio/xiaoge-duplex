"""Build the Xiaoge C SDK customer delivery package.

Run from the ``clients`` directory:

    python package_c_sdk.py

What it does:

1. Refreshes the content files of ``dist/xiaoge-c-sdk-r5.2.2/`` from the
   repo sources. Package-specific files (``PACKAGE_README.md`` and the
   delivery-adapted root ``CMakeLists.txt``) are preserved as-is.
2. Copies the MinGW64 prebuilt static library and executables from
   ``c/build-mingw64/``. Rebuild them first (``ninja -C c/build-mingw64``
   with the MinGW64 toolchain on PATH); this script never invokes the
   compiler itself.
3. Regenerates ``SHA256SUMS.txt`` and writes ``dist/xiaoge-c-sdk-r5.2.2.zip``.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

CLIENTS = Path(__file__).resolve().parent
C_SDK = CLIENTS / "c"
BUILD = C_SDK / "build-mingw64"
PKG_DIR = CLIENTS / "dist" / "xiaoge-c-sdk-r5.2.2"
PKG_ZIP = CLIENTS / "dist" / "xiaoge-c-sdk-r5.2.2.zip"

# repo source -> package destination (relative paths)
COPY_FILES: list[tuple[Path, Path]] = [
    (CLIENTS / "README.md", Path("README.md")),
    (CLIENTS / "PROTOCOL.md", Path("PROTOCOL.md")),
    (CLIENTS / "SDK_TEST_VALIDATION.md", Path("SDK_TEST_VALIDATION.md")),
    (C_SDK / "README.md", Path("docs/C_SDK_README.md")),
    (C_SDK / "docs" / "Xiaoge_C_SDK_接口调用说明.md", Path("docs/Xiaoge_C_SDK_接口调用说明.md")),
    (C_SDK / "xiaoge_client.h", Path("include/xiaoge_client.h")),
    (C_SDK / "xiaoge_client.c", Path("src/xiaoge_client.c")),
    (C_SDK / "CMakeLists.txt", Path("cmake/CMakeLists.txt")),
    (C_SDK / "cmake" / "run_adb_test.cmake", Path("cmake/run_adb_test.cmake")),
    (C_SDK / "demo_file.c", Path("examples/demo_file.c")),
    (C_SDK / "demo_mic_win.c", Path("examples/demo_mic_win.c")),
    (C_SDK / "test_codec.c", Path("examples/test_codec.c")),
    (C_SDK / "local_proxy.py", Path("examples/local_proxy.py")),
    (CLIENTS / "certs" / "cloud-ca.pem", Path("certs/cloud-ca.pem")),
]

# whole directories copied file-by-file so the package never inherits build output
COPY_TREES: list[tuple[Path, Path]] = [
    (C_SDK / "third_party" / "cjson", Path("third_party/cjson")),
]

# prebuilt MinGW64 artifacts; rebuild c/build-mingw64 before packaging
PREBUILT: list[tuple[Path, Path]] = [
    (BUILD / "libxiaoge_client.a", Path("lib/windows-x86_64-mingw64/libxiaoge_client.a")),
    (BUILD / "xiaoge_demo_file.exe", Path("bin/windows-x86_64-mingw64/xiaoge_demo_file.exe")),
    (BUILD / "xiaoge_demo_mic_win.exe", Path("bin/windows-x86_64-mingw64/xiaoge_demo_mic_win.exe")),
    (BUILD / "xiaoge_c_codec_test.exe", Path("bin/windows-x86_64-mingw64/xiaoge_c_codec_test.exe")),
]

# package-specific files kept in dist; never overwritten from the repo
PRESERVED = {
    "PACKAGE_README.md",
    # delivery-adapted root build file (include/src/third_party layout + cjson)
    "CMakeLists.txt",
}


def refresh_content() -> None:
    if not PKG_DIR.is_dir():
        raise SystemExit(
            f"package base not found: {PKG_DIR} "
            "(copy it from the previous delivery before refreshing)"
        )
    targets = COPY_FILES + PREBUILT
    missing = [str(source) for source, _ in targets if not source.exists()]
    missing += [str(source_tree) for source_tree, _ in COPY_TREES if not source_tree.is_dir()]
    if missing:
        raise SystemExit(
            "missing sources (rebuild c/build-mingw64 with ninja first):\n  " + "\n  ".join(missing)
        )
    for source, relative in targets:
        destination = PKG_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        print(f"[copy] {relative}")
    for source_tree, relative_tree in COPY_TREES:
        for source in sorted(source_tree.rglob("*")):
            if not source.is_file():
                continue
            relative = relative_tree / source.relative_to(source_tree)
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
    refresh_content()
    clean_junk()
    write_checksums()
    write_zip()
    print("[done] c delivery package refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

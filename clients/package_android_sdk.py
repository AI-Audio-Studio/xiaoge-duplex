"""Build the Xiaoge Android SDK customer delivery package.

Run from the ``clients`` directory:

    python package_android_sdk.py

What it does:

1. Optionally builds the release AAR (``--no-build`` to skip when it is fresh).
2. Refreshes the content files of ``dist/xiaoge-android-sdk-r5.2.2/`` from the
   repo sources. Package-specific files (``PACKAGE_README.md``, the AAR-based
   ``demo/build.gradle`` and ``settings.gradle``) are preserved as-is.
3. Removes local build artifacts and secrets that must never ship
   (``.gradle/``, ``.idea/``, ``demo/build/``, ``local.properties``).
4. Regenerates ``SHA256SUMS.txt`` and writes ``dist/xiaoge-android-sdk-r5.2.2.zip``.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import shutil
import sys
import zipfile
from pathlib import Path

CLIENTS = Path(__file__).resolve().parent
ANDROID = CLIENTS / "android"
PKG_DIR = CLIENTS / "dist" / "xiaoge-android-sdk-r5.2.2"
PKG_ZIP = CLIENTS / "dist" / "xiaoge-android-sdk-r5.2.2.zip"
AAR_SOURCE = ANDROID / "xiaoge-sdk-core" / "build" / "outputs" / "aar" / "xiaoge-sdk-core-release.aar"

# repo source -> package destination (relative paths)
COPY_FILES: list[tuple[Path, Path]] = [
    (CLIENTS / "PROTOCOL.md", Path("PROTOCOL.md")),
    (ANDROID / "build.gradle", Path("build.gradle")),
    (ANDROID / "gradle.properties", Path("gradle.properties")),
    (ANDROID / "gradlew", Path("gradlew")),
    (ANDROID / "gradlew.bat", Path("gradlew.bat")),
    (ANDROID / "gradle" / "wrapper" / "gradle-wrapper.jar", Path("gradle/wrapper/gradle-wrapper.jar")),
    (ANDROID / "gradle" / "wrapper" / "gradle-wrapper.properties",
     Path("gradle/wrapper/gradle-wrapper.properties")),
    (ANDROID / "docs" / "ANDROID_SDK_README.md", Path("docs/ANDROID_SDK_README.md")),
    (ANDROID / "demo" / "src" / "main" / "AndroidManifest.xml",
     Path("demo/src/main/AndroidManifest.xml")),
]

# whole directories copied file-by-file so the package never inherits build output
COPY_TREES: list[tuple[Path, Path]] = [
    (ANDROID / "demo" / "src" / "main" / "java", Path("demo/src/main/java")),
    (ANDROID / "demo" / "src" / "main" / "res", Path("demo/src/main/res")),
]

# package-specific files kept in dist; never overwritten from the repo
PRESERVED = {
    "PACKAGE_README.md",
    "settings.gradle",  # package includes only :demo (AAR-based), not :xiaoge-sdk-core
    "demo/build.gradle",  # depends on aar/xiaoge-sdk-core-release.aar, not the source module
}

# local build artifacts / secrets that must never ship
JUNK_PATHS = [
    ".gradle",
    ".idea",
    "demo/build",
    "local.properties",
]


def build_aar() -> None:
    gradlew = ANDROID / "gradlew.bat"
    command = [str(gradlew), ":xiaoge-sdk-core:assembleRelease", "--console=plain"]
    if not gradlew.exists():
        command = [str(ANDROID / "gradlew"), ":xiaoge-sdk-core:assembleRelease", "--console=plain"]
    print(f"[build] {' '.join(command)}")
    subprocess.run(command, cwd=ANDROID, check=True)


def refresh_content() -> None:
    if not AAR_SOURCE.exists():
        raise SystemExit(f"release AAR not found: {AAR_SOURCE} (run without --no-build)")
    targets = [(AAR_SOURCE, Path("aar/xiaoge-sdk-core-release.aar"))]
    targets += COPY_FILES
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
    for relative in JUNK_PATHS:
        target = PKG_DIR / relative
        if target.is_dir():
            shutil.rmtree(target)
            print(f"[clean] removed dir {relative}")
        elif target.exists():
            target.unlink()
            print(f"[clean] removed {relative}")
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
    parser.add_argument("--no-build", action="store_true", help="skip gradle assembleRelease")
    args = parser.parse_args()

    if not args.no_build:
        build_aar()
    refresh_content()
    clean_junk()
    write_checksums()
    write_zip()
    print("[done] android delivery package refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

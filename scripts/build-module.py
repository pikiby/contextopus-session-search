#!/usr/bin/env python3
"""Build a Contextopus Session Search folder or ZIP without installing anything."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# Deliberate code allowlist: no old socket frontend, model, private vectors or data.
SOURCES = ("__init__", "cards", "card_service")


def build(output):
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError("Output already exists; choose a new folder")
    inputs = [ROOT / "src/session_search" / (name + ".py") for name in SOURCES]
    migrations = ROOT / "module/migrations"
    if not migrations.is_dir() or not list(migrations.glob("*.sql")):
        raise ValueError("Module SQL migrations are missing")
    for source in inputs:
        if not source.is_file():
            raise ValueError("Module source is missing: " + str(source))
    manifest = json.loads((ROOT / "module/module.json").read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".session-search-package-", dir=output.parent) as temporary:
        stage = Path(temporary) / "session_search"
        package = stage / "package/session_search"
        package.mkdir(parents=True)
        for name in ("worker.py",):
            shutil.copy2(ROOT / "module/package" / name, stage / "package" / name)
        for source in inputs:
            shutil.copy2(source, package / source.name)
        (stage / "package/migrations").mkdir()
        for source in sorted(migrations.glob("*.sql")):
            shutil.copy2(source, stage / "package/migrations" / source.name)
        shutil.copytree(ROOT / "module/package/procedures", stage / "package/procedures")
        shutil.copytree(ROOT / "module/package/skills", stage / "package/skills")
        shutil.copy2(ROOT / "module/module.json", stage / "module.json")
        shutil.copy2(ROOT / "docs/module.md", stage / "README.md")
        for name in ("LICENSE", "NOTICE", "install.sh", "install.ps1"):
            shutil.copy2(ROOT / name, stage / name)
        assert manifest["id"] == "session_search"
        stage.rename(output)
    return output


def build_zip(output):
    """Wrap the same allowlisted folder in a portable module archive."""
    output = Path(output).expanduser().resolve()
    checksum = Path(str(output) + ".sha256")
    if output.exists() or checksum.exists():
        raise ValueError("Output already exists; choose a new ZIP path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".session-search-zip-", dir=output.parent) as temporary:
        staging = Path(temporary)
        module = build(staging / "session_search")
        archive = staging / "module.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for source in sorted(module.rglob("*")):
                if source.is_file():
                    bundle.write(source, source.relative_to(staging))
        # Exclusive creation also protects against an output appearing mid-build.
        with output.open("xb") as target, archive.open("rb") as source:
            shutil.copyfileobj(source, target)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        with checksum.open("x", encoding="utf-8") as stream:
            stream.write(digest + "  " + output.name + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", action="store_true", help="Build a ZIP with a session_search/ top-level folder")
    parser.add_argument("output", type=Path, help="New output folder, or ZIP path with --zip")
    args = parser.parse_args()
    print((build_zip if args.zip else build)(args.output))

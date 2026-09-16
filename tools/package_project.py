"""Package only explicitly allowed source and public handover files."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT.parent / "outputs"


def main() -> None:
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    required = ["test-results.xml", "offline-demo-result.json", "preview-check-result.json"]
    for name in required:
        source = OUTPUTS / name
        if not source.is_file():
            raise RuntimeError(f"Required completed verification is missing: {name}")
        shutil.copyfile(source, reports / name)
    test_xml = ElementTree.parse(OUTPUTS / "test-results.xml")
    suites = list(test_xml.getroot().iter("testsuite"))
    failures = sum(int(s.attrib.get("failures", 0)) + int(s.attrib.get("errors", 0)) for s in suites)
    if failures:
        raise RuntimeError("Refusing to package with failing tests")
    public_preview = ROOT / "preview"
    public_preview.mkdir(exist_ok=True)
    preview_html = (OUTPUTS / "shop-preview.html").read_text(encoding="utf-8")
    preview_html = preview_html.replace(
        '<a class="download" href="digital-shelf-telegram-shop.zip" download>Download the source ZIP</a>',
        '<p class="small">This is the bundled preview. The application source is one folder above.</p>',
    )
    (public_preview / "shop-preview.html").write_text(preview_html, encoding="utf-8")
    manifest = {
        "project": "digital-shelf-telegram", "version": "0.1.0",
        "foundation": "aiogram 3.31.0 (MIT)", "live_telegram_verified": False,
        "supplier_connected": False,
        "tests": sum(int(s.attrib.get("tests", 0)) for s in suites),
        "test_failures": failures,
        "dependencies": {name: importlib.metadata.version(name) for name in ("aiogram", "cryptography", "pytest", "ruff")},
        "files": {},
    }
    files = [ROOT / name for name in (
        "pyproject.toml", "requirements.txt", "requirements-dev.txt", "requirements-lock.txt",
        "config.example.json", ".gitignore", "THIRD_PARTY_NOTICES.txt", "run_shop.py",
    )]
    for folder, suffixes in (("shop", {".py"}), ("tests", {".py"}), ("sample", {".json"}),
                             ("tools", {".py", ".cjs"}), ("reports", {".xml", ".json"}),
                             ("preview", {".html"})):
        files.extend(p for p in (ROOT / folder).iterdir() if p.is_file() and p.suffix in suffixes)
    forbidden = {"config.local.json", ".env", "data", "__pycache__", ".git"}
    for file in files:
        relative = file.relative_to(ROOT)
        if forbidden.intersection(relative.parts) or not file.is_file():
            raise RuntimeError(f"Unsafe or missing archive member: {relative}")
        manifest["files"][relative.as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (reports / "build-manifest.json").write_text(manifest_text, encoding="utf-8")
    files.append(reports / "build-manifest.json")
    archive = OUTPUTS / "digital-shelf-telegram-shop.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for file in sorted(files):
            bundle.write(file, Path("digital-shelf-telegram-shop") / file.relative_to(ROOT))
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError("Archive verification failed")
        if any("config.local" in name or "/data/" in name or name.endswith(".sqlite3") for name in bundle.namelist()):
            raise RuntimeError("Private data entered source archive")
    print(json.dumps({"archive": str(archive), "files": len(files), "bytes": archive.stat().st_size,
                      "tests": manifest["tests"], "private_files_included": False,
                      "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()

"""Bounded, read-only wheel/sdist structural and signature scan, not clearance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import zipfile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publication_tools.core_export import PublicationPolicyError, _imports

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 2000
PRODUCT_PACKAGES = {"stpg", "stil", "stpg_runner", "stpgview"}
FORBIDDEN_PARTS = {".git", ".codex", ".agents", ".venv", "recovery-safe", "review", "pico-sdk", "logicanalyzer",
                   "doc", "docs", "release_coordination", "publication_prep", "stpg_capture", "stpg_la"}
FORBIDDEN_SUFFIXES = {".elf", ".uf2", ".bin", ".map", ".log", ".vcd", ".lac", ".zip", ".pyc"}
SIGNATURES = {
    "personal-source-path": re.compile(rb"/(?:home|Users)/[^\s\x00\"']+"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "ai-session-link": re.compile(rb"https?://(?:claude\.ai/code/session|chatgpt\.com/c/)"),
}


def member_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value or path.is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/")) or ":" in value):
        raise ValueError("archive contains a nonportable/escaping path")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
           or any(ord(c) < 32 or c in '<>:"|?*' for c in part) for part in path.parts):
        raise ValueError("archive contains a Windows-reserved path")
    return value


def scan_members(members: list[tuple[str, bytes]], *, wheel: bool) -> dict:
    if len(members) > MAX_FILES or sum(len(data) for _, data in members) > MAX_TOTAL_BYTES:
        raise ValueError("archive exceeds scan limits")
    seen = set()
    static, notices, modules = set(), set(), set()
    for name, raw in members:
        member_path(name)
        if name.casefold() in seen:
            raise ValueError("duplicate/case-ambiguous archive member")
        seen.add(name.casefold())
        path = PurePosixPath(name)
        review_at = next((i for i in range(len(path.parts) - 1)
                          if tuple(p.casefold() for p in path.parts[i:i + 2]) == ("tools", "reviews")), None)
        if review_at is not None:
            raise ValueError("internal review harness/patch is not public source")
        if ({p.casefold() for p in path.parts} & FORBIDDEN_PARTS or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or path.name.casefold() == "logicanalyzer-gpl-3.0-reference.txt" or len(raw) > MAX_FILE_BYTES):
            raise ValueError(f"excluded distribution file class: {name}")
        if wheel and path.parts[0] not in PRODUCT_PACKAGES and not path.parts[0].endswith(".dist-info"):
            raise ValueError(f"unexpected wheel top-level package: {path.parts[0]}")
        for label, signature in SIGNATURES.items():
            if signature.search(raw):
                raise ValueError(f"{label} in {name}; matching text withheld")
        if "stpgview/static/" in name:
            static.add(path.name)
        if path.name in {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
            notices.add(path.name)
        if name.endswith(".py"):
            _imports(name, raw)
            modules.add(name)
    if static != {"app.js", "index.html", "runner.js", "style.css"}:
        raise ValueError("missing/extra GUI static files")
    if notices != {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
        raise ValueError("missing root license/scope notices")
    return {"files": len(members), "python_files": len(modules), "gui_assets": sorted(static),
            "signature_scan": "NO_CONFIGURED_MATCH_NOT_COMPLETE_PRIVACY_CLEARANCE"}


def audit(path: Path, *, expected_package_files: dict[str, str] | None = None,
          expected_source_files: dict[str, str] | None = None,
          source_manifest_sha256: str | None = None,
          expected_notices: dict[str, str] | None = None) -> dict:
    if path.stat().st_size > MAX_TOTAL_BYTES:
        raise ValueError("archive file exceeds scan limit")
    members = []
    total_bytes = 0
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_FILES * 2:
                raise ValueError("too many archive entries")
            for item in archive.infolist():
                member_path(item.filename[:-1] if item.is_dir() else item.filename)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("symlink wheel member")
                if item.is_dir():
                    continue
                if (len(members) >= MAX_FILES or item.file_size > MAX_FILE_BYTES
                        or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1):
                    raise ValueError("oversized/encrypted/symlink wheel member")
                total_bytes += item.file_size
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("archive exceeds total scan limit")
                members.append((item.filename, archive.read(item)))
        wheel = True
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for index, item in enumerate(archive):
                if index >= MAX_FILES * 2:
                    raise ValueError("too many source archive entries, including directories")
                member_path(item.name.rstrip("/"))
                if item.isdir():
                    continue
                if not item.isfile() or item.size > MAX_FILE_BYTES or len(members) >= MAX_FILES:
                    raise ValueError("nonregular/oversized source archive member")
                total_bytes += item.size
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("archive exceeds total scan limit")
                stream = archive.extractfile(item)
                assert stream is not None
                members.append((item.name, stream.read(MAX_FILE_BYTES + 1)))
        wheel = False
    else:
        raise ValueError("expected a wheel or gzip sdist")
    checked = scan_members(members, wheel=wheel)
    if expected_package_files is not None:
        actual = {}
        roots = {name.split("/", 1)[0] for name, _ in members}
        if not wheel and len(roots) != 1:
            raise ValueError("sdist must have exactly one root directory")
        for name, raw in members:
            rel = name if wheel else name.split("/", 1)[1]
            if not wheel:
                top = rel.split("/", 1)[0]
                if top not in {"tools", "firmware", "manual", "release", "controller", "examples", "LICENSES", "release_tools", "publication_tools", ".github", ".gitignore", ".gitattributes",
                               "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md", "CONTRIBUTING.md",
                               "pyproject.toml", "MANIFEST.in", "PKG-INFO", "setup.cfg"}:
                    raise ValueError("unlisted sdist top-level entry")
                if not rel.startswith("tools/"):
                    continue
                rel = rel.removeprefix("tools/")
            if rel.split("/", 1)[0] in PRODUCT_PACKAGES:
                actual[rel] = hashlib.sha256(raw).hexdigest()
        if actual != expected_package_files:
            raise ValueError("package file inventory/hash differs from the reviewed allowlist")
    if not wheel and expected_source_files is not None:
        actual = {}
        generated = {"PKG-INFO", "setup.cfg", "release/source-files.json"}
        egg_files = {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt"}
        for name, raw in members:
            rel = name.split("/", 1)[1]
            if rel == "release/source-files.json" and source_manifest_sha256 is not None:
                if hashlib.sha256(raw).hexdigest() != source_manifest_sha256:
                    raise ValueError("embedded source allowlist differs from trusted review file")
            if rel in generated:
                continue
            if rel.startswith("tools/stpg_rp2350.egg-info/") and PurePosixPath(rel).name in egg_files:
                continue
            actual[rel] = hashlib.sha256(raw).hexdigest()
        if actual != expected_source_files:
            raise ValueError("full source inventory/hash differs (tests/firmware/docs included)")
    if wheel and expected_notices is not None:
        notices = {name.split("/licenses/", 1)[1]: hashlib.sha256(raw).hexdigest()
                   for name, raw in members if ".dist-info/licenses/" in name}
        if notices != expected_notices:
            raise ValueError("wheel notice inventory/hash differs from reviewed source notices")
    return {"kind": "stpg_distribution_scan", "archive": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **checked,
            "structure_check": "COMPLETE", "license_clearance": "PENDING",
            "public_distribution_cleared": False, "device_io_performed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manifest", type=Path,
                        default=Path(__file__).resolve().parents[1] / "release/package-files.json")
    parser.add_argument("--source-manifest", type=Path,
                        default=Path(__file__).resolve().parents[1] / "release/source-files.json")
    args = parser.parse_args()
    archives = sorted(p for p in args.directory.iterdir() if p.name.endswith((".whl", ".tar.gz")))
    if not archives:
        raise ValueError("no distribution archives found")
    expected = json.loads(args.manifest.read_text(encoding="utf-8"))["files"]
    source_bytes = args.source_manifest.read_bytes()
    source = json.loads(source_bytes)["files"]
    notices = {p: h for p, h in source.items() if p in {"LICENSE", "THIRD_PARTY_NOTICES.md"} or p.startswith("LICENSES/")}
    reports = []
    for path in archives:
        try:
            reports.append(audit(path, expected_package_files=expected, expected_source_files=source,
                                 source_manifest_sha256=hashlib.sha256(source_bytes).hexdigest(),
                                 expected_notices=notices))
        except (ValueError, OSError) as exc:
            reports.append({"kind": "stpg_distribution_scan", "archive": path.name, "status": "REJECT",
                            "code": exc.code if isinstance(exc, PublicationPolicyError) else "STRUCTURE_OR_HASH_REJECTED",
                            "matching_text_disclosed": False, "license_clearance": "PENDING",
                            "public_distribution_cleared": False, "device_io_performed": False})
    print(json.dumps(reports, indent=2))
    return 2 if any(r.get("status") == "REJECT" for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())

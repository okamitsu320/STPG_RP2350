"""Fail-closed, history-free MIT-core export from an explicit file/hash allowlist.

The owner's private term policy MUST remain outside every Git worktree. Neither
its terms, its path, its count nor a digest of it is copied to exported receipts.
This is a packaging/privacy check, not complete rights or publication clearance.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unicodedata

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
CORE_PACKAGES = {"stpg", "stil", "stpg_runner", "stpgview"}
EXCLUDED_PARTS = {".git", ".agents", ".codex", ".aws", ".ssh", ".venv", "__pycache__", "review", "recovery-safe",
                  "agent-handoff", ".recovery-evidence",
                  "publication_prep", "release_coordination", "doc", "docs", "pico-sdk", "stpg_capture", "stpg_la"}
EXCLUDED_SUFFIXES = {".elf", ".uf2", ".bin", ".map", ".log", ".vcd", ".lac", ".zip", ".whl", ".pyc"}
EXCLUDED_NAMES = {"la_protocol.py", "la_device.py", "logicanalyzer-gpl-3.0-reference.txt"}
PUBLIC_TOP = {"tools", "firmware", "manual", "release", "controller", "examples", "LICENSES", "release_tools", "publication_tools",
              ".github", ".gitignore", ".gitattributes", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
              "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml", "MANIFEST.in"}
GPL_MODULES = {"stpg_capture", "stpg_la"}


class PublicationPolicyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code + "; matching content and private locations withheld")


def _fail(code):
    raise PublicationPolicyError(code)


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail("DUPLICATE_JSON_KEY")
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: _fail("NONFINITE_JSON"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("INVALID_JSON")
    if not isinstance(value, dict):
        _fail("INVALID_JSON_ROOT")
    return value


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


class OwnerTerms:
    def __init__(self, path: Path | None, root: Path, destination: Path | None = None):
        if path is None:
            _fail("OWNER_TERM_POLICY_REQUIRED")
        path = Path(path).resolve()
        if path.is_relative_to(root) or (destination is not None and path.is_relative_to(destination)):
            _fail("OWNER_TERM_POLICY_MUST_BE_OUTSIDE_REPOSITORY")
        def git_worktree(parent):
            marker = parent / ".git"
            # An empty reserved .git directory is not itself a Git repository.
            # Real linked worktrees use a gitdir file; normal repos have HEAD.
            return marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file())
        if any(git_worktree(parent) for parent in path.parents):
            _fail("OWNER_TERM_POLICY_MUST_BE_OUTSIDE_REPOSITORY")
        try:
            with path.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
        except OSError:
            _fail("OWNER_TERM_POLICY_UNREADABLE")
        if len(raw) > 1024 * 1024:
            _fail("OWNER_TERM_POLICY_TOO_LARGE")
        value = _json(raw)
        if (set(value) != {"kind", "schema_version", "terms"}
                or value["kind"] != "stpg_owner_private_terms" or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or not isinstance(value["terms"], list)
                or not 1 <= len(value["terms"]) <= 20000):
            _fail("OWNER_TERM_POLICY_INVALID")
        terms = value["terms"]
        if any(not isinstance(t, str) or not 3 <= len(t) <= 4096 or not t.strip() for t in terms):
            _fail("OWNER_TERM_POLICY_INVALID")
        self.pattern = re.compile("|".join(re.escape(t) for t in sorted({_normalize(t) for t in terms}, key=len, reverse=True)))

    def check_text(self, text):
        if self.pattern.search(_normalize(text)):
            _fail("OWNER_PRIVATE_TERM")

    def check(self, name: str, raw: bytes):
        self.check_text(name)
        payloads = [raw]
        if name.lower().endswith(".gz"):
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    decoded = stream.read(MAX_FILE_BYTES + 1)
            except (OSError, EOFError):
                _fail("INVALID_COMPRESSED_SOURCE")
            if len(decoded) > MAX_FILE_BYTES:
                _fail("DECODED_SOURCE_TOO_LARGE")
            payloads.append(decoded)
        for payload in payloads:
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                self.check_text(payload.decode(encoding, errors="ignore"))


def _relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        _fail("NONPORTABLE_EXPORT_PATH")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(p.endswith((".", " ")) or p.split(".")[0].upper() in reserved
           or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or c in '<>:"|?*' for c in p) for p in name.split("/")):
        _fail("NONPORTABLE_EXPORT_PATH")
    return PurePosixPath(name)


def _imports(name, raw):
    try:
        tree = ast.parse(raw, filename=name)
    except (SyntaxError, UnicodeError, ValueError):
        _fail("INVALID_PUBLIC_PYTHON")
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
            if node.module in {None, ""}:
                names += [a.name for a in node.names]
        elif isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            if target.endswith("entry_points"):
                _fail("IN_PROCESS_PLUGIN_LOADER")
            if target.endswith(("import_module", "__import__")):
                names = [a.value for a in node.args[:1] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if target.endswith(".load") and "entry_point" in ast.unparse(node):
                _fail("IN_PROCESS_PLUGIN_LOADER")
        if any(n.split(".", 1)[0].casefold() in GPL_MODULES for n in names):
            _fail("GPL_IMPORT_IN_CORE")


def _metadata(blobs):
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        metadata = tomllib.loads(blobs["pyproject.toml"].decode("utf-8"))
        product = _json(blobs["release/package-files.json"])["files"]
    except (KeyError, ValueError, UnicodeError, ImportError):
        _fail("INVALID_CORE_PACKAGE_METADATA")
    if not isinstance(product, dict) or not product:
        _fail("INVALID_CORE_PACKAGE_INVENTORY")
    for name, digest in product.items():
        relative = _relative(name)
        if relative.parts[0] not in CORE_PACKAGES:
            _fail("NON_CORE_INSTALLED_PACKAGE")
        raw = blobs.get("tools/" + name)
        if raw is None or hashlib.sha256(raw).hexdigest() != digest:
            _fail("INSTALLED_PACKAGE_HASH_MISMATCH")
    project = metadata.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("scripts", {}), dict):
        _fail("INVALID_CORE_PACKAGE_METADATA")
    scripts = project.get("scripts", {})
    for label, target in scripts.items():
        if (not isinstance(target, str) or ":" not in target
                or target.split(".", 1)[0] not in CORE_PACKAGES
                or label in {"stpg-capture", "stpg-la"}):
            _fail("NON_CORE_ENTRY_POINT")
        module = target.split(":", 1)[0]
        if module.replace(".", "/") + ".py" not in product:
            _fail("ENTRY_POINT_MODULE_MISSING")
    if project.get("entry-points"):
        _fail("IN_PROCESS_PLUGIN_ENTRY_POINTS")
    dependencies = project.get("dependencies", [])
    extras = project.get("optional-dependencies", {})
    if (not isinstance(dependencies, list) or not isinstance(extras, dict)
            or any(not isinstance(group, list) for group in extras.values())):
        _fail("INVALID_CORE_PACKAGE_METADATA")
    for dependency in [*dependencies, *(d for group in extras.values() for d in group)]:
        if not isinstance(dependency, str):
            _fail("INVALID_CORE_PACKAGE_METADATA")
        name = re.split(r"[\s\[<>=!~@;]", dependency, maxsplit=1)[0].casefold().replace("_", "-")
        if name in {"stpg-rp2350-la", "stpg-capture", "stpg-la"}:
            _fail("GPL_CORE_DEPENDENCY")


def _load(root, allowlist, terms):
    if not isinstance(allowlist, dict) or not 1 <= len(allowlist) <= 2000:
        _fail("INVALID_EXPORT_ALLOWLIST")
    blobs, seen, total = {}, set(), 0
    for name, digest in allowlist.items():
        relative = _relative(name)
        terms.check_text(name)
        folded = {p.casefold() for p in relative.parts}
        if (folded & EXCLUDED_PARTS or relative.parts[0] not in PUBLIC_TOP
                or relative.suffix.lower() in EXCLUDED_SUFFIXES or relative.name.casefold() in EXCLUDED_NAMES
                or tuple(p.casefold() for p in relative.parts[:2]) == ("tools", "reviews")):
            _fail("EXCLUDED_PUBLIC_FILE_CLASS")
        if name.casefold() in seen:
            _fail("CASE_AMBIGUOUS_EXPORT_PATH")
        seen.add(name.casefold())
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            _fail("INVALID_EXPORT_HASH")
        path = root.joinpath(*relative.parts)
        if (any(p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root))
                or not path.resolve().is_relative_to(root)):
            _fail("SYMLINK_EXPORT_INPUT")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            _fail("EXPORT_INPUT_UNREADABLE")
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            _fail("EXPORT_SIZE_LIMIT")
        terms.check(name, raw)
        if hashlib.sha256(raw).hexdigest() != digest:
            _fail("EXPORT_HASH_CHANGED")
        blobs[name] = raw
    for name, raw in blobs.items():
        if name.endswith(".py"):
            _imports(name, raw)
    _metadata(blobs)
    return blobs


def check_core_export(root: Path, allowlist: dict[str, str], *, private_terms: Path | None) -> dict:
    root = Path(root).resolve()
    terms = OwnerTerms(private_terms, root)
    blobs = _load(root, allowlist, terms)
    return {"kind": "stpg_core_publication_check", "schema_version": 1, "status": "PASS",
            "files": len(blobs), "owner_private_terms_check": "PASS_NOT_COMPLETE_PRIVACY_CLEARANCE",
            "source_history_copied": False, "license_clearance": "PENDING",
            "public_distribution_cleared": False, "device_io_performed": False}


def export_core(root: Path, destination: Path, allowlist: dict[str, str], *, private_terms: Path | None) -> dict:
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        _fail("EXPORT_DESTINATION_OVERLAPS_SOURCE")
    terms = OwnerTerms(private_terms, root, destination)
    blobs = _load(root, allowlist, terms)  # all checks before creating output
    destination.mkdir()  # exclusive; no overwrite/retry/cleanup after failure
    for name, raw in blobs.items():
        path = destination.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
    return {"kind": "stpg_core_publication_export", "schema_version": 1, "status": "EXPORTED_NOT_PUBLISHED",
            "files": len(blobs), "owner_private_terms_check": "PASS_NOT_COMPLETE_PRIVACY_CLEARANCE",
            "source_history_copied": False, "license_clearance": "PENDING",
            "public_distribution_cleared": False, "device_io_performed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--private-terms", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.allowlist.open("rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            _fail("EXPORT_ALLOWLIST_TOO_LARGE")
        value = _json(payload)
        files = value.get("files")
        if args.output is None:
            report = check_core_export(args.source, files, private_terms=args.private_terms)
        else:
            report = export_core(args.source, args.output, files, private_terms=args.private_terms)
        code = 0
    except (PublicationPolicyError, OSError, UnicodeError, ValueError) as exc:
        report = {"kind": "stpg_core_publication_check", "schema_version": 1, "status": "REJECT",
                  "code": exc.code if isinstance(exc, PublicationPolicyError) else "PUBLICATION_INPUT_ERROR",
                  "matching_text_disclosed": False, "license_clearance": "PENDING",
                  "public_distribution_cleared": False, "device_io_performed": False}
        code = 2
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

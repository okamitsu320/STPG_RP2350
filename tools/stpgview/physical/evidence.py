"""Pack physical GUI test results for review (offline; reads only the paths given, contacts no device).

    python -m stpgview.physical.evidence --out EVIDENCE.zip [--bindings CONFIG_DIR] [--note FILE]... RESULT_DIR...

The archive holds ``environment.json`` (OS, Python, pyserial, git identity of
this checkout, source digests of the SDK/GUI packages), every regular file of
each result directory under ``results/<directory name>/``, optional local
binding files (``--bindings``: devices, analyzers, controllers) and extra
files such as the filled report and screenshots (``--note``, repeatable), plus
``manifest.json`` with the SHA-256 of every member. Nothing else is read.
Bindings name USB serial numbers and ports; include them only when the
reviewer needs them and you have chosen to share them. The archive stays local:
nothing is uploaded or published.

Each member is read once and the stored bytes are what the manifest hashes.
Symbolic links among the selected inputs are rejected; this checks the files
as they are when packing starts and is not a defence against another process
replacing them while the archive is written. Files over 64 MiB are listed in
``skipped_over_64MiB`` with their hash instead of being stored. A result
directory without its result record (run.json, control.json, identify.json,
capture.json or session.json), or whose record was skipped, is listed under
``incomplete`` and the command exits with status 2: the review of such evidence
is incomplete. So is a ``stpg/``, ``accessory/`` (external accessory runs) or an
older ``run/`` / ``capture/`` folder without its record, and a control,
accessory or capture record without the STPG ``run.json``. These are presence
checks only, not a validation of the records' contents or references; the
files an external accessory program wrote under ``accessory/`` are packed as
they are and never interpreted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import platform
import stat
import subprocess
import sys
import zipfile

MAX_FILE_BYTES = 64 * 1024 * 1024
RESULT_RECORDS = ("run.json", "run.json.gz", "control.json", "identify.json", "capture.json", "session.json")
PACKAGES = ("stpg", "stil", "stpg_runner", "stpgview")  # the MIT core; a separately installed accessory is not hashed
# Top-level folders of a composed invocation and the record each must hold: stpg/ + accessory/ (external accessory
# process), run/ + capture/ (results of older in-process captures).
FOLDER_RECORDS = {"stpg": "run.json", "accessory": "session.json", "run": "run.json", "capture": "capture.json"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.rstrip("\n") if out.returncode == 0 else None  # keep porcelain's leading status column


def environment() -> dict:
    tools = Path(__file__).resolve().parents[2]
    digests = {}
    for package in PACKAGES:
        module = importlib.import_module(package)
        roots = [Path(folder).resolve() for folder in module.__path__]
        tree = hashlib.sha256()
        count = 0
        for index, folder in enumerate(roots):
            files = sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()
                           and "__pycache__" not in p.parts)
            for path in files:
                key = f"{package}/{index}/{path.relative_to(folder).as_posix()}"
                tree.update(key.encode() + b"\0" + _sha256(path).encode() + b"\n")
                count += 1
        loaded = {name: {"path": str(Path(item.__file__).resolve()), "sha256": _sha256(Path(item.__file__))}
                  for name, item in list(sys.modules.items())
                  if (name == package or name.startswith(package + ".")) and getattr(item, "__file__", None)
                  and Path(item.__file__).is_file()}
        digests[package] = {"roots": [str(root) for root in roots], "files": count,
                            "tree_sha256": tree.hexdigest(), "loaded_modules": loaded}
    try:
        import serial

        pyserial = getattr(serial, "__version__", "unknown")
    except ImportError:
        pyserial = None
    status = _git(tools.parent, "status", "--porcelain", "--untracked-files=no")
    return {
        "kind": "stpg_gui_physical_evidence", "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": {"system": platform.system(), "release": platform.release(), "version": platform.version(),
                     "machine": platform.machine()},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation()},
        "pyserial": pyserial,
        "git": {"head": _git(tools.parent, "rev-parse", "HEAD"),
                "branch": _git(tools.parent, "rev-parse", "--abbrev-ref", "HEAD"),
                "tracked_changes": status.splitlines() if status else []},
        "source_digests": digests,
        "argv": sys.argv[1:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="*", type=Path, help="result directories (run/capture/control folders)")
    parser.add_argument("--out", type=Path, required=True, help="new .zip file")
    parser.add_argument("--bindings", type=Path, help="stpg-view config directory (devices/analyzers/controllers)")
    parser.add_argument("--note", action="append", type=Path, default=[],
                        help="report, screenshot or terminal log (repeat for each file)")
    args = parser.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; choose a new file")
    members: list[tuple[Path, str]] = []
    for result in args.results:
        if result.is_symlink() or not result.is_dir():
            raise SystemExit(f"{result} is not a directory")
        for path in sorted(result.rglob("*")):
            if path.is_symlink():
                raise SystemExit(f"symlink is not a selected regular evidence file: {path}")
            if path.is_file():
                members.append((path, f"results/{result.name}/{path.relative_to(result).as_posix()}"))
    if args.bindings:
        if args.bindings.is_symlink() or not args.bindings.is_dir():
            raise SystemExit("bindings must be a real directory")
        for kind in ("devices", "analyzers", "controllers"):
            if (args.bindings / kind).is_symlink():
                raise SystemExit("binding subdirectories must not be symlinks")
            for path in sorted((args.bindings / kind).glob("*.json")):
                if path.is_symlink() or not path.is_file():
                    raise SystemExit(f"binding is not a regular file: {path}")
                members.append((path, f"bindings/{kind}/{path.name}"))
    for path in args.note:
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"{path} is not a file")
        members.append((path, f"notes/{path.name}"))
    names = [name for _, name in members]
    if len(set(names)) != len(names):
        raise SystemExit("two inputs map to the same archive name; rename one result directory or note")
    manifest, skipped = [], []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        environment_bytes = (json.dumps(environment(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        archive.writestr("environment.json", environment_bytes)
        manifest.append({"name": "environment.json", "size": len(environment_bytes),
                         "sha256": hashlib.sha256(environment_bytes).hexdigest()})
        for path, name in members:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise SystemExit(f"evidence file changed into a link or special file: {path}")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                skipped.append({"name": name, "size": size, "sha256": _sha256(path)})
                continue
            with path.open("rb") as stream:
                payload = stream.read(MAX_FILE_BYTES + 1)
            if len(payload) > MAX_FILE_BYTES:
                raise SystemExit(f"evidence grew beyond the bound while packing: {path}")
            archive.writestr(name, payload)
            manifest.append({"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        stored = {entry["name"] for entry in manifest}
        incomplete = []
        for result in args.results:
            records = [f"results/{result.name}/{path.relative_to(result).as_posix()}"
                       for path in sorted(result.rglob("*")) if path.name in RESULT_RECORDS and path.is_file()]
            if not records:
                incomplete.append(f"results/{result.name}: no result record ({', '.join(RESULT_RECORDS)})")
            incomplete += [f"{name}: result record not stored (over 64 MiB)" for name in records if name not in stored]
            # A composed invocation contains separate stpg/ and accessory/ (or older run/ and capture/) result
            # directories. One surviving record cannot cover its empty sibling. Only the top level is checked:
            # what an accessory program writes inside accessory/ is its own layout.
            for folder in sorted(path for path in result.iterdir() if path.is_dir()):
                expected = FOLDER_RECORDS.get(folder.name)
                present = ((folder / expected).is_file() or expected == "run.json" and (folder / "run.json.gz").is_file()) if expected else False
                if expected and not present:
                    incomplete.append(f"results/{result.name}/{folder.name}: missing {expected}")
            # Every run invocation has an STPG run (./run.json, stpg/run.json or run/run.json): a surviving
            # control, accessory or capture record cannot cover a missing or removed run record or folder.
            names = {Path(name).name for name in records}
            for folder in {path.parent for path in result.rglob("run.json")}:
                if (folder / "run.json.gz").exists():
                    incomplete.append(f"results/{result.name}: ambiguous run.json and run.json.gz")
            if names & {"control.json", "capture.json", "session.json"} and not names & {"run.json", "run.json.gz"}:
                incomplete.append(f"results/{result.name}: control/accessory/capture record without the STPG run.json")
        archive.writestr("manifest.json", json.dumps({"files": manifest, "skipped_over_64MiB": skipped,
                                                      "incomplete": incomplete}, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"archive": str(args.out), "files": len(manifest),
                      "skipped_over_64MiB": [entry["name"] for entry in skipped], "incomplete": incomplete,
                      "sha256": _sha256(args.out)}, indent=2))
    if incomplete:
        print("evidence is INCOMPLETE: the archive was written, but its review cannot be complete", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

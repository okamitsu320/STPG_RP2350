"""Explicit source-distribution hash inventory, excluding its own JSON bytes."""
import argparse
import hashlib
import json
import os
from pathlib import Path

SKIP = {".git", ".venv", "venv", "build", "dist", "host-build", "__pycache__", ".pytest_cache", "review", "release-assets",
        "doc", "docs", "recovery-safe", "release_coordination", "publication_prep", ".codex", ".agents", ".aws",
        "stpg_capture", "stpg_la"}


def source_files(root: Path) -> dict[str, str]:
    files = {}
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d.casefold() not in SKIP and not d.endswith(".egg-info"))
        for name in sorted(names):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if relative.casefold().startswith("tools/reviews/"):
                continue
            if name == ".git" or relative == "release/source-files.json":
                continue
            if path.is_symlink():
                raise ValueError("symlink in source inventory")
            if path.suffix in {".pyc", ".log", ".zip", ".elf", ".uf2", ".bin", ".map"}:
                raise ValueError("unexpected generated/binary file in source inventory")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    result = {"kind": "stpg_source_file_allowlist", "schema_version": 1,
              "self_path": "release/source-files.json", "public_distribution_cleared": False,
              "files": source_files(root)}
    with (root / "release/source-files.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"source_files_excluding_self": len(result["files"])}))


if __name__ == "__main__":
    main()

"""Generate an explicit product-file hash allowlist after deliberate review.

This does not clear confidentiality/licenses and never changes an old bundle.
It is intentionally separate from the archive checker; a mismatch is not fixed
automatically by verification.
"""
import argparse
import hashlib
import json
from pathlib import Path

PACKAGES = ("stpg", "stil", "stpg_runner", "stpgview")


def inventory(root: Path) -> dict:
    files = {}
    for package in PACKAGES:
        base = root / "tools" / package
        if base.is_symlink() or not base.is_dir():
            raise ValueError("missing/symlink product package")
        for path in sorted(base.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink():
                raise ValueError("symlink in product package")
            if not path.is_file():
                continue
            if path.suffix != ".py" and not (path.parent == base / "static" and path.suffix in {".html", ".js", ".css"}):
                raise ValueError("unreviewed package file class")
            rel = path.relative_to(root / "tools").as_posix()
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"kind": "stpg_product_file_allowlist", "schema_version": 1,
            "public_distribution_cleared": False, "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = inventory(args.root.resolve())
    output = args.output or args.root / "release/package-files.json"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"product_files": len(report["files"]), "public_distribution_cleared": False}))


if __name__ == "__main__":
    main()

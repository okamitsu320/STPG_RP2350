"""A project folder keeps no uncompressed copy of its STIL; public data.

Up to SDK 66c87369, ``init_project`` copied the STIL into ``inputs/`` as it was, and every build copied it again into
its reservation (``snapshot_inputs``), although STIL text compresses very well (the public counter here about 45
times with gzip). Since SDK 9bf78b6c both copies are gzip when that is smaller.

The tests look only at bytes on disk: no byte-identical copy of the STIL, and a project folder clearly smaller than
the STIL. They do not prescribe a codec or a file name.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stpg_runner.build import build_project, init_project, verify_bundle
from stpgview.physical.patterns import counter, counter_pins

pytestmark = pytest.mark.stpg_host_only
VECTORS = 20_000  # about 480 KB of STIL; one package, no waits


def source(root: Path) -> Path:
    (root / "counter.stil").write_text(counter(VECTORS), encoding="utf-8")
    (root / "pins.json").write_text(counter_pins(), encoding="utf-8")
    return root / "counter.stil"


def copies(folder: Path, stil: Path) -> list[Path]:
    size, digest = stil.stat().st_size, hashlib.sha256(stil.read_bytes()).digest()
    return [path for path in sorted(folder.rglob("*")) if path.is_file() and path.stat().st_size == size
            and hashlib.sha256(path.read_bytes()).digest() == digest]


def total(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())


def test_a_new_project_stores_its_stil_compressed(tmp_path):
    stil = source(tmp_path)
    project = init_project(stil, tmp_path / "project", pin_map=tmp_path / "pins.json", wait_preset="none",
                           completion="hold")
    assert copies(project.parent, stil) == []
    assert total(project.parent) < stil.stat().st_size // 4


def test_a_build_adds_no_uncompressed_copy_of_the_stil(tmp_path):
    stil = source(tmp_path)
    project = init_project(stil, tmp_path / "project", pin_map=tmp_path / "pins.json", wait_preset="none",
                           completion="hold")
    result = build_project(project)
    assert verify_bundle(result.bundle_dir, manifest_sha256=result.manifest_sha256)["status"] == "PASS"
    assert copies(project.parent, stil) == []  # neither inputs/ nor the build's reservation

"""Regression checks for the pruned, public-source layout; no hardware."""
from pathlib import Path
import re

import pytest
from stpg_runner.cli import main

ROOT = Path(__file__).resolve().parents[2]


def test_help_only_advertises_supported_accessory_path(capsys):
    with pytest.raises(SystemExit) as result:
        main(["run", "--help"])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert "--external-accessory" in text
    for name in ("--analyzer-config", "--capture-plan", "--marker-derivation"):
        assert name not in text


@pytest.mark.parametrize("option", ["--analyzer-config", "--capture-plan", "--marker-derivation"])
def test_removed_analyzer_option_rejects_before_reading_missing_files(tmp_path, capsys, option):
    result = main(["run", str(tmp_path / "missing-bundle"), "--manifest-sha256", "0"*64,
                   "--device-config", str(tmp_path / "missing-device.json"),
                   "--output-dir", str(tmp_path / "out"), option, str(tmp_path / "missing.json")])
    assert result != 0
    text = capsys.readouterr()
    assert "in-process analyzer options were removed" in text.out + text.err
    assert not (tmp_path / "out").exists()


def test_all_public_manual_and_root_links_resolve():
    pages = [ROOT / "README.md", ROOT / "controller/README.md", *ROOT.glob("manual/**/*.md")]
    for page in pages:
        for target in re.findall(r"\[[^\]]+\]\(([^)\s]+)\)", page.read_text(encoding="utf-8")):
            if target.startswith("https://"):
                continue
            assert (page.parent / target.split("#")[0]).exists(), (page.name, target)


def test_no_private_tree_is_required_or_present():
    for name in ("doc", "docs", "recovery-safe", "review", "release_coordination", ".codex", "AGENTS.md"):
        assert not (ROOT / name).exists(), name
    assert not (ROOT / "tools/reviews").exists()
    assert (ROOT / "controller/firmware/rp2040_zero_gpio_toggle.ino").is_file()
    assert (ROOT / "examples/blink.stil").is_file()


def test_checked_in_example_builds_and_verifies_without_private_inputs(tmp_path):
    from stpg_runner import init_project, build_project, verify_bundle
    project = init_project(ROOT / "examples/blink.stil", tmp_path / "project",
                           wait_preset="none", completion="hold")
    built = build_project(project)
    assert built.segment_count == 1
    assert verify_bundle(built.bundle_dir, manifest_sha256=built.manifest_sha256)["status"] == "PASS"

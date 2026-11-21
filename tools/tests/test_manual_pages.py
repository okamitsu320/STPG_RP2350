"""The user manual stays true to the program: links work, no internal material, named options and labels exist."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from stpgview.cli import build_parser
import stpgview

pytestmark = pytest.mark.stpg_host_only
MANUAL = Path(__file__).resolve().parents[2] / "manual"
PAGES = sorted(MANUAL.glob("*.md"))
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
INTERNAL = re.compile(r"\bdoc/|\bdocs/|companion/|recovery-safe|\bAI[12]\b|\bCodex\b|\bGLM\b|tools/reviews|"
                      r"stpg-capture run|run_with_capture|in-process|C:\\Users\\")
# Labels the manual tells a reader to press or read, with the page source that must contain them.
LABELS = ("Build in Runner…", "New project…", "Open project…", "Open bundle…", "Edit settings…",
          "Copy to a new project…", "Devices…", "Analyzers…", "Controllers…", "Scan ports", "Run…", "Start run",
          "Reset STPG…", "Reset LA…", "Set GPIO…", "External accessory", "use with the next run", "Check",
          "not selected", "not checked", "check failed", "checked, not in use", "used with the next run",
          "Settings folder problem", "How to set up a logic analyzer", "Colours…", "Export VCD", "Go to")


def test_the_manual_exists_and_every_page_is_reachable_from_its_index():
    names = {page.name for page in PAGES}
    assert {"README.md", "getting-started.md", "hardware.md", "viewer.md", "runner.md", "settings-folder.md",
            "gpio-controller.md", "logic-analyzer.md", "troubleshooting.md", "testing-gui.md"} <= names
    index = (MANUAL / "README.md").read_text(encoding="utf-8")
    assert names - {"README.md"} == {link for link in LINK.findall(index) if link.endswith(".md")}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_links_resolve_and_external_ones_carry_no_tracking(page):
    for link in LINK.findall(page.read_text(encoding="utf-8")):
        if link.startswith("http"):
            assert link.startswith("https://") and "?" not in link and "#" not in link, link
        else:
            assert (page.parent / link.split("#")[0]).is_file(), link


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_internal_material_or_old_commands(page):
    assert INTERNAL.search(page.read_text(encoding="utf-8")) is None


def test_options_named_in_the_manual_exist():
    known = {option for action in build_parser()._actions for option in action.option_strings}
    text = "\n".join(page.read_text(encoding="utf-8") for page in PAGES if page.name != "testing-gui.md")
    named = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", text))
    # Only these two flags belong to the separate LA page named in its setup
    # example. The core must not import that GPL tool to inspect its parser;
    # that distribution checks its own CLI flags independently.
    external_la = {"--demo", "--no-device-io"}
    assert named <= known | external_la, named - known - external_la
    assert {"--demo-device", "--no-device-run", "--config-dir", "--results-dir"} <= named


def test_labels_named_in_the_manual_are_on_the_page():
    static = Path(stpgview.__file__).parent / "static"
    source = "\n".join(path.read_text(encoding="utf-8") for path in static.iterdir() if path.suffix in (".js", ".html"))
    text = "\n".join(page.read_text(encoding="utf-8") for page in PAGES)
    for label in LABELS:
        assert label in text, f"manual: {label}"
        assert label in source, f"page: {label}"


def test_the_required_public_statements_are_present():
    hardware = (MANUAL / "hardware.md").read_text(encoding="utf-8")
    assert "have not been independently checked" in hardware and "not a rated operating point" in hardware
    assert "not guaranteed to match the release binary" in hardware
    analyzer = (MANUAL / "logic-analyzer.md").read_text(encoding="utf-8")
    assert "**separate tool**" in analyzer and "read only" in analyzer and "external.json" in analyzer
    assert "does not tell when they arrived" in analyzer
    assert "a subset of STIL" in (MANUAL / "README.md").read_text(encoding="utf-8")


def test_every_test_named_in_the_catalogue_exists():
    rows = [line for line in (MANUAL / "testing-gui.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and "::" in line]
    assert rows
    for row in rows:
        current = None
        for item in re.findall(r"`([^`]*::[^`]*)`", row):
            name, test = item.split("::")
            current = current if name == "…" else name
            assert f"def {test}(" in (Path(__file__).parent / current).read_text(encoding="utf-8"), item

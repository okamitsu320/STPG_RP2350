"""Invented sensitive tokens only; no owner's confidential data or feature list."""
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from publication_tools.core_export import (PublicationPolicyError, check_core_export, export_core, main)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    data = {
        "tools/stpg_runner/__init__.py": b'"""Public synthetic package."""\n',
        "tools/stpg_runner/cli.py": b'def main():\n    return 0\n',
        "pyproject.toml": b'[project]\nname="stpg-rp2350"\nversion="0.1"\nlicense="MIT"\n[project.scripts]\nstpg-runner="stpg_runner.cli:main"\n',
        "README.md": b'Public synthetic source.\n',
        ".gitattributes": b'* -text\n',
    }
    product = {p.removeprefix("tools/"): sha(raw) for p, raw in data.items() if p.startswith("tools/")}
    data["release/package-files.json"] = (json.dumps({"files": product}) + "\n").encode()
    for name, raw in data.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    policy = tmp_path / "owner-policy.json"
    policy.write_text(json.dumps({"kind": "stpg_owner_private_terms", "schema_version": 1,
                                  "terms": ["SYNTHETIC_PRIVATE_SENTINEL"]}))
    return root, {p: sha(raw) for p, raw in data.items()}, policy


def replace_file(root, files, name, raw):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    files[name] = sha(raw)


def test_positive_export_has_only_reviewed_bytes_and_no_history_policy_or_review(tmp_path, candidate):
    root, files, policy = candidate
    # Internal preparation files exist but are deliberately not in the allowlist.
    (root / "review").mkdir()
    (root / "review/internal.txt").write_text("not for public export")
    output = tmp_path / "clean-public-tree"
    result = export_core(root, output, files, private_terms=policy)
    assert result["status"] == "EXPORTED_NOT_PUBLISHED" and result["source_history_copied"] is False
    assert result["public_distribution_cleared"] is False and result["license_clearance"] == "PENDING"
    assert {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} == set(files)
    assert all(sha((output / name).read_bytes()) == digest for name, digest in files.items())
    assert not (output / ".git").exists() and not (output / "review").exists()
    assert not (output / policy.name).exists()
    with pytest.raises(FileExistsError):
        export_core(root, output, files, private_terms=policy)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "gzip"])
def test_owner_term_blocks_before_output_without_echoing_secret(tmp_path, candidate, encoding):
    root, files, policy = candidate
    token = "synthetic_private_sentinel"
    raw = token.encode("utf-8" if encoding == "gzip" else encoding)
    name = "examples/public.json" + (".gz" if encoding == "gzip" else "")
    replace_file(root, files, name, gzip.compress(raw) if encoding == "gzip" else raw)
    output = tmp_path / "never"
    with pytest.raises(PublicationPolicyError, match="OWNER_PRIVATE_TERM") as exc:
        export_core(root, output, files, private_terms=policy)
    assert token not in str(exc.value).casefold() and str(policy) not in str(exc.value)
    assert not output.exists()


@pytest.mark.parametrize("selected", ["review/RESULTS.md", ".git/config", "tools/stpg_capture/la_protocol.py",
                                      "tools/stpg_la/driver.py", "tools/reviews/runner_no_hardware.py",
                                      "tools/reviews/test_runner_gui_handoff.py",
                                      "tools/reviews/test_runner_round3_handoff.py", "tools/reviews/old.patch",
                                      "tools/Reviews/old.patch",
                                      "docs/firmware.elf", "doc/text.md", "docs/text.md", "manual/docs/text.md",
                                      "Docs/text.md", "recovery-safe/evidence.json", "release_coordination/private.md",
                                      "tools/reviews/audit_runner_result.py", "tools/code.pyc"])
def test_excluded_class_cannot_be_smuggled_in_an_allowlist(tmp_path, candidate, selected):
    root, files, policy = candidate
    replace_file(root, files, selected, b'public synthetic placeholder\n')
    output = tmp_path / "never"
    with pytest.raises(PublicationPolicyError, match="EXCLUDED_PUBLIC_FILE_CLASS"):
        export_core(root, output, files, private_terms=policy)
    assert not output.exists()


def test_explicit_mit_test_helpers_remain_exportable(candidate):
    root, files, policy = candidate
    replace_file(root, files, "tools/tests/helpers/audit_runner_result.py", b'def synthetic_oracle():\n    return 1\n')
    assert check_core_export(root, files, private_terms=policy)["status"] == "PASS"


@pytest.mark.parametrize("code", [b'import stpg_capture\n', b'from stpg_la import driver\n',
                                 b'import importlib\nimportlib.import_module("stpg_capture.config")\n',
                                 b'__import__("stpg_la")\n'])
def test_gpl_import_edge_blocks_even_when_module_is_not_installed(candidate, code):
    root, files, policy = candidate
    replace_file(root, files, "tools/stpgview/runner/service.py", code)
    with pytest.raises(PublicationPolicyError, match="GPL_IMPORT_IN_CORE"):
        check_core_export(root, files, private_terms=policy)


def test_in_process_plugin_discovery_is_not_the_approved_process_boundary(candidate):
    root, files, policy = candidate
    replace_file(root, files, "tools/stpgview/plugins.py", b'from importlib.metadata import entry_points\nentry_points()\n')
    with pytest.raises(PublicationPolicyError, match="IN_PROCESS_PLUGIN_LOADER"):
        check_core_export(root, files, private_terms=policy)


@pytest.mark.parametrize("declaration", [
    '\n[project.optional-dependencies]\nla=["stpg-rp2350-la>=0.1"]\n',
    '\n[project.entry-points."stpg_runner.accessories"]\nla="stpg_la:plugin"\n',
])
def test_separate_distribution_is_not_a_core_dependency_or_entry_point(candidate, declaration):
    root, files, policy = candidate
    path = root / "pyproject.toml"
    replace_file(root, files, "pyproject.toml", path.read_bytes() + declaration.encode())
    with pytest.raises(PublicationPolicyError):
        check_core_export(root, files, private_terms=policy)


def test_capture_console_entry_point_is_not_allowed(candidate):
    root, files, policy = candidate
    path = root / "pyproject.toml"
    replace_file(root, files, "pyproject.toml", path.read_bytes() + b'stpg-capture="stpg_capture.cli:main"\n')
    with pytest.raises(PublicationPolicyError, match="NON_CORE_ENTRY_POINT"):
        check_core_export(root, files, private_terms=policy)


@pytest.mark.parametrize("path", ["../escape", "C:/escape", "docs\\bad.md", "/absolute", "docs/CON", "docs/x. "])
def test_portable_paths_are_enforced_before_reading(candidate, path):
    root, files, policy = candidate
    files[path] = "0"*64
    with pytest.raises(PublicationPolicyError, match="NONPORTABLE_EXPORT_PATH"):
        check_core_export(root, files, private_terms=policy)


def test_changed_hash_and_symlink_are_not_exported(candidate, tmp_path):
    root, files, policy = candidate
    (root / "README.md").write_text("changed public bytes")
    with pytest.raises(PublicationPolicyError, match="EXPORT_HASH_CHANGED"):
        check_core_export(root, files, private_terms=policy)
    (root / "README.md").unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("public outside")
    (root / "README.md").symlink_to(outside)
    with pytest.raises(PublicationPolicyError, match="SYMLINK_EXPORT_INPUT"):
        check_core_export(root, files, private_terms=policy)


def test_policy_required_and_kept_outside_repository(candidate, tmp_path):
    root, files, policy = candidate
    with pytest.raises(PublicationPolicyError, match="OWNER_TERM_POLICY_REQUIRED"):
        check_core_export(root, files, private_terms=None)
    in_tree = root / "private-policy.json"
    in_tree.write_bytes(policy.read_bytes())
    with pytest.raises(PublicationPolicyError, match="OUTSIDE_REPOSITORY"):
        check_core_export(root, files, private_terms=in_tree)
    other = tmp_path / "other-worktree"
    other.mkdir()
    (other / ".git").write_text("gitdir: synthetic-only\n")
    other_policy = other / "owner-policy.json"
    other_policy.write_bytes(policy.read_bytes())
    with pytest.raises(PublicationPolicyError, match="OUTSIDE_REPOSITORY"):
        check_core_export(root, files, private_terms=other_policy)


def test_cli_refusal_does_not_disclose_private_policy_path_or_token(candidate, tmp_path, capsys):
    root, files, policy = candidate
    replace_file(root, files, "README.md", b'SYNTHETIC_PRIVATE_SENTINEL\n')
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"files": files}))
    assert main(["--source", str(root), "--allowlist", str(allowlist), "--private-terms", str(policy)]) == 2
    text = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in text and str(policy) not in text
    assert json.loads(text)["code"] == "OWNER_PRIVATE_TERM"

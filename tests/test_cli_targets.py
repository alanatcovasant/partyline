"""Pure unit tests for client-side target normalization (no network, no pool)."""

import os

import pytest

from covasant_partyline.targets import normalize_target


def test_relative_path_resolves_against_cwd(tmp_path, monkeypatch):
    sub = tmp_path / "pkg"
    sub.mkdir()
    monkeypatch.chdir(tmp_path)
    assert normalize_target("pkg") == normalize_target(str(sub))


def test_dot_resolves_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert normalize_target(".") == normalize_target(str(tmp_path))


def test_result_has_no_backslashes_and_no_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = normalize_target(".")
    assert "\\" not in out
    assert not out.endswith("/") or out == "/"
    assert ".." not in out.split("/")


@pytest.mark.skipif(os.name != "nt", reason="case-folding is Windows-only")
def test_windows_paths_are_case_folded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert normalize_target(".") == normalize_target(".").lower()


def test_work_items_are_lowercased_not_touched_by_realpath():
    assert normalize_target("issue:Covasant/Aura#482") == "issue:covasant/aura#482"
    assert normalize_target("PR:Covasant/Aura#17") == "pr:covasant/aura#17"


def test_missing_path_is_refused_before_any_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        normalize_target("no/such/dir")


def test_output_passes_server_side_validation(tmp_path, monkeypatch):
    """The client's output must be exactly what the server accepts — no daylight."""
    from app.targets import validate_target

    monkeypatch.chdir(tmp_path)
    validate_target(normalize_target("."))
    validate_target(normalize_target("issue:Covasant/Aura#482"))

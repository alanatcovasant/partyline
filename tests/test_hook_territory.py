"""Pure unit tests for the SessionStart territory warning (no network)."""

from covasant_partyline.hook import territory_warning

CWD = "/home/alan/code/aura/aura_core/src/aura_core/domain/tenant"


def _claim(agent_id, path, **kw):
    base = {"agent_id": agent_id, "resource_path": path, "repo": "aura",
            "resource_type": "workdir", "reason": None,
            "expires_at": "2026-08-01T18:00:00Z"}
    base.update(kw)
    return base


def test_warns_when_cwd_is_inside_another_agents_claim():
    out = territory_warning(
        [_claim("alan_aura_0", "/home/alan/code/aura/aura_core/src/aura_core")],
        CWD, "alan_aura_1",
    )
    assert "alan_aura_0" in out
    assert "/home/alan/code/aura/aura_core/src/aura_core" in out


def test_warns_when_another_agent_claims_below_you():
    out = territory_warning([_claim("alan_aura_0", CWD + "/models")], CWD, "alan_aura_1")
    assert "alan_aura_0" in out


def test_silent_on_your_own_claim():
    assert territory_warning([_claim("alan_aura_1", CWD)], CWD, "alan_aura_1") == ""


def test_silent_on_unrelated_sibling():
    assert territory_warning(
        [_claim("alan_aura_0", "/home/alan/code/aura/aura_core/src/aura_coreX")],
        CWD, "alan_aura_1",
    ) == ""


def test_silent_on_work_item_claims():
    assert territory_warning(
        [_claim("alan_aura_0", "issue:covasant/aura#482", resource_type="issue")],
        CWD, "alan_aura_1",
    ) == ""


def test_silent_when_nothing_is_claimed():
    assert territory_warning([], CWD, "alan_aura_1") == ""


def test_windows_drive_paths_are_recognised_as_territory():
    cwd = "c:/users/alana/code/repos/aura/aura_core/domain"
    out = territory_warning(
        [_claim("alan_aura_0", "c:/users/alana/code/repos/aura/aura_core")],
        cwd, "alan_aura_1",
    )
    assert "alan_aura_0" in out


def test_reason_is_surfaced_when_present():
    out = territory_warning(
        [_claim("alan_aura_0", CWD, reason="issue #482 tenant refactor")],
        CWD, "alan_aura_1",
    )
    assert "issue #482 tenant refactor" in out

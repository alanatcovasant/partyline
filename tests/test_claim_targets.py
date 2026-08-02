"""Pure unit tests for claim-target validation (no Postgres, no network)."""

import pytest

from app.targets import (
    InvalidTarget,
    infer_resource_type,
    is_work_item,
    validate_target,
)


@pytest.mark.parametrize(
    "target",
    [
        "c:/users/alana/code/repos/aura/aura_core",
        "/home/alan/code/aura/aura_core",
        "/",
        "issue:covasant/aura#482",
        "pr:covasant/aura-web#17",
    ],
)
def test_canonical_targets_are_accepted(target):
    validate_target(target)  # must not raise


@pytest.mark.parametrize(
    "target, needle",
    [
        ("db.py", "not absolute"),
        ("app/db.py", "not absolute"),
        ("", "empty"),
        ("C:\\Users\\alana\\code", "backslash"),
        ("C:/Users/alana/code", "case-folded"),
        ("/home/alan/code/", "trailing slash"),
        ("/home/alan/../bob/code", "unresolved path segment"),
        ("/home/alan/./code", "unresolved path segment"),
        ("/home//alan/code", "empty path segment"),
        ("issue:covasant/aura", "malformed work-item"),
        ("issue:aura#482", "malformed work-item"),
    ],
)
def test_non_canonical_targets_are_refused(target, needle):
    with pytest.raises(InvalidTarget) as exc:
        validate_target(target)
    assert needle in exc.value.message
    assert exc.value.hint  # every refusal tells the caller how to fix it


def test_posix_paths_keep_their_case():
    validate_target("/home/Alan/Code")  # case-sensitive filesystem; not an error


def test_work_item_detection_and_type():
    assert is_work_item("issue:covasant/aura#482")
    assert is_work_item("pr:covasant/aura#482")
    assert not is_work_item("/home/alan/code")
    assert infer_resource_type("issue:covasant/aura#482") == "issue"
    assert infer_resource_type("pr:covasant/aura#482") == "pr"
    assert infer_resource_type("/home/alan/code") == "directory"

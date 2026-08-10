# tests/test_grant_app_access.py
import importlib.util
import pathlib
from unittest.mock import MagicMock

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "grant_app_access.py"
_s = importlib.util.spec_from_file_location("grant_app_access", _P)
m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(m)


def test_grant_calls_update_permissions_with_can_use():
    w = MagicMock()
    m.grant(w, "mcp-covasant-partyline", "app-123")
    assert w.apps.update_permissions.called
    call = w.apps.update_permissions.call_args
    # app_name is passed POSITIONALLY (robust to the SDK's name/app_name kwarg skew that a
    # keyword arg silently accepted on a MagicMock but the real SDK rejected with TypeError).
    assert call.args[0] == "mcp-covasant-partyline"
    acl = call.kwargs["access_control_list"]
    assert acl and getattr(acl[0], "service_principal_name", None) == "app-123"
    # Assert the grant level too — CAN_USE is the whole point of the grant.
    assert "CAN_USE" in str(getattr(acl[0], "permission_level", None))


def test_grant_falls_back_to_rest_when_sdk_method_missing():
    w = MagicMock()
    del w.apps.update_permissions   # simulate older SDK without the typed method
    m.grant(w, "mcp-covasant-partyline", "app-123")
    # fallback path uses the raw permissions REST endpoint
    call = w.api_client.do.call_args
    assert call.args[0] == "PATCH"
    assert "/permissions/apps/mcp-covasant-partyline" in call.args[1]

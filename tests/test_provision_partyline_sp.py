# tests/test_provision_partyline_sp.py
import importlib.util
import pathlib
from unittest.mock import MagicMock

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "provision_partyline_sp.py"
_s = importlib.util.spec_from_file_location("provision_partyline_sp", _P)
m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(m)


def test_resolve_existing_sp_does_not_create():
    w = MagicMock()
    w.api_client.do.return_value = {"Resources": [{"applicationId": "app-123", "id": "1"}]}
    app_id = m.resolve_or_create_sp(w, "covasant-partyline-example-job")
    assert app_id == "app-123"
    # Only the GET/list call happened — no POST create.
    methods = [c.args[0] for c in w.api_client.do.call_args_list]
    assert "POST" not in methods


def test_create_sp_when_absent():
    w = MagicMock()
    w.api_client.do.side_effect = [
        {"Resources": []},                                   # list: none found
        {"applicationId": "app-new", "id": "9"},             # create
    ]
    app_id = m.resolve_or_create_sp(w, "covasant-partyline-example-job")
    assert app_id == "app-new"
    methods = [c.args[0] for c in w.api_client.do.call_args_list]
    assert methods[0] == "GET" and methods[1] == "POST"


def test_ensure_workspace_sp_skips_create_when_present():
    w = MagicMock()
    existing = MagicMock()
    existing.id = 42
    w.service_principals.list.return_value = [existing]
    assert m.ensure_workspace_sp(w, "appid", "disp") == "42"
    w.service_principals.create.assert_not_called()


def test_ensure_workspace_sp_creates_when_absent():
    w = MagicMock()
    w.service_principals.list.return_value = []
    created = MagicMock()
    created.id = 99
    w.service_principals.create.return_value = created
    assert m.ensure_workspace_sp(w, "appid", "disp") == "99"
    w.service_principals.create.assert_called_once()


def test_mint_fresh_secret_deletes_all_then_mints_one():
    w = MagicMock()
    s1 = MagicMock()
    s1.id = "a"
    s2 = MagicMock()
    s2.id = "b"
    w.service_principal_secrets_proxy.list.return_value = [s1, s2]
    minted = MagicMock()
    minted.id = "new"
    minted.secret = "CLEARTEXT"
    w.service_principal_secrets_proxy.create.return_value = minted
    out = m.mint_fresh_secret(w, "123", 100)
    assert out == "CLEARTEXT"
    assert w.service_principal_secrets_proxy.delete.call_count == 2
    w.service_principal_secrets_proxy.create.assert_called_once()


def test_ensure_secret_scope_guards_create_but_always_puts():
    w = MagicMock()
    existing_scope = MagicMock()
    existing_scope.name = m.SECRET_SCOPE
    w.secrets.list_scopes.return_value = [existing_scope]
    m.ensure_secret_scope(w, m.SECRET_SCOPE, "cid", "csecret", "appid")
    w.secrets.create_scope.assert_not_called()          # scope already exists
    assert w.secrets.put_secret.call_count == 2          # id + secret always written
    w.secrets.put_acl.assert_called_once()

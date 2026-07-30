# tests/test_provision_partyline_sp.py
import importlib.util, pathlib
from unittest.mock import MagicMock

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "provision_partyline_sp.py"
_s = importlib.util.spec_from_file_location("provision_partyline_sp", _P)
m = importlib.util.module_from_spec(_s); _s.loader.exec_module(m)


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

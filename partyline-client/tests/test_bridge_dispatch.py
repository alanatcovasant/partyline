from covasant_partyline import bridge


def test_dispatch_kind_classifies_verbs():
    assert bridge.dispatch_kind("post_message") == "send"
    assert bridge.dispatch_kind("reply") == "send"
    assert bridge.dispatch_kind("ack_checkpoint") == "send"
    assert bridge.dispatch_kind("get_unread") == "sync"
    assert bridge.dispatch_kind("claim_resource") == "sync"
    assert bridge.dispatch_kind("check_send") == "local"
    assert bridge.dispatch_kind("partyline_bridge_info") == "local"
    assert bridge.dispatch_kind("something_else") == "proxy"


def test_send_verbs_and_sync_verbs_disjoint():
    assert bridge._SEND_VERBS.isdisjoint(bridge._SYNC_VERBS)


def test_base_url_from_mcp_url():
    assert bridge._base_url_from_mcp("https://app.example.com/mcp") == "https://app.example.com"
    assert bridge._base_url_from_mcp("https://app.example.com/mcp/") == "https://app.example.com"


def test_bridge_info_reports_spool_when_sender_present():
    class FakeSender:
        spool_path = "/tmp/partyline_a.yaml"
        spill_flag = {"pending": 2, "path": "/tmp/partyline_a.yaml"}
    info = bridge._bridge_info(None, sender=FakeSender())
    assert info["spool_path"] == "/tmp/partyline_a.yaml"
    assert info["spooled_pending"] == 2

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

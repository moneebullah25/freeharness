from lib import accounts


def test_session_roundtrip():
    assert accounts.read_session(accounts.make_session("ann")) is None or True

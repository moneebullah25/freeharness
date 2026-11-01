import os
import tempfile

from shop import auth, store, util


def setup_module(_):
    store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    import shop.config
    shop.config.DB_PATH = store.DB_PATH
    store.init_db()


def test_token_round_trip():
    assert auth.verify_token(auth.make_token(7)) == 7


def test_password_check():
    stored = auth.hash_password("pw")
    assert auth.check_password(stored, "pw")
    assert not auth.check_password(stored, "other")


def test_discount_whole_numbers():
    assert util.apply_discount(100, 10) == 90


def test_unique_once():
    assert util.unique([1, 2, 2, 3]) == [1, 2, 3]


def test_format_price():
    assert util.format_price(1234.5) == "$1,234.50"

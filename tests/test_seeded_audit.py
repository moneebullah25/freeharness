"""The planted-bug audit: the grader is pure, and every planted bug is proven REAL by running the project's own code."""
import importlib
import sys

import pytest

from freeharness.eval import seeded

PROJECT = seeded.PROJECT


@pytest.fixture
def shop(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(PROJECT))
    monkeypatch.setenv("SHOP_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SHOP_RECEIPTS", str(tmp_path / "receipts"))
    for m in [m for m in sys.modules if m == "shop" or m.startswith("shop.")]:
        del sys.modules[m]
    (tmp_path / "receipts").mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    mods = {n: importlib.import_module(f"shop.{n}") for n in ("config", "auth", "store", "util", "api")}
    mods["store"].init_db()
    yield mods, tmp_path
    for m in [m for m in sys.modules if m == "shop" or m.startswith("shop.")]:
        del sys.modules[m]


# ---------------------------------------------------------------------------------- the bugs are real

def test_secret_is_in_the_source(shop):
    assert shop[0]["config"].SECRET_KEY == "shop-dev-secret-2024"


def test_password_hash_is_unsalted_md5(shop):
    a = shop[0]["auth"]
    assert a.hash_password("pw") == a.hash_password("pw") and len(a.hash_password("pw")) == 32


def test_tokens_live_about_41_days_not_an_hour(shop):
    a = shop[0]["auth"]
    t = a.make_token(5, now=1_000_000)
    assert a.verify_token(t, now=1_000_000 + 3601) == 5          # an hour and a second later: still valid, TTL is 3600 s
    assert a.verify_token(t, now=1_000_000 + 3600 * 1000 + 1) is None


def test_signature_compare_is_not_constant_time(shop):
    import inspect
    assert "compare_digest" not in inspect.getsource(shop[0]["auth"].verify_token)


def test_sql_injection_in_find_user(shop):
    s = shop[0]["store"]
    s.add_user("alice", "h")
    assert s.find_user("nobody' OR '1'='1") is not None             # returns someone for a name that does not exist
    assert s.find_user("nobody") is None


def test_path_traversal_in_read_receipt(shop):
    s = shop[0]["store"]
    assert s.read_receipt("../secret.txt") == "top secret"


def test_money_is_a_float(shop):
    s = shop[0]["store"]
    oid = s.save_order(1, [{"price": 0.1, "qty": 3}])
    assert s.find_order(oid)[2] == 0.1 * 3 != 0.3


def test_any_signed_in_user_reads_any_order(shop):
    m, _ = shop
    oid = m["store"].save_order(1, [{"price": 1, "qty": 1}])
    other = m["auth"].make_token(2)
    status, body = m["api"].get_order({"headers": {"Authorization": "Bearer " + other}, "params": {"id": oid}})
    assert status == 200 and body["user_id"] == 1


def test_create_order_accepts_negative_quantities_and_answers_200(shop):
    m, _ = shop
    tok = m["auth"].make_token(1)
    status, body = m["api"].create_order({"headers": {"Authorization": "Bearer " + tok}, "json": {"items": [{"price": 5, "qty": -3}]}})
    assert status == 200 and m["store"].find_order(body["id"])[2] == -15


def test_first_page_is_skipped(shop):
    m, _ = shop
    for _ in range(25):
        m["store"].save_order(1, [{"price": 1, "qty": 1}])
    tok = m["auth"].make_token(1)
    status, body = m["api"].list_orders({"headers": {"Authorization": "Bearer " + tok}, "query": {"page": "1"}})
    assert status == 200 and [o["id"] for o in body] == [21, 22, 23, 24, 25]      # page 1 should start at order 1


def test_errors_return_the_traceback(shop):
    m, _ = shop
    tok = m["auth"].make_token(1)
    status, body = m["api"].handle("list_orders", {"headers": {"Authorization": "Bearer " + tok}, "query": {"page": "abc"}})
    assert status == 500 and "Traceback" in body["trace"]


def test_discount_is_floored(shop):
    u = shop[0]["util"]
    assert u.apply_discount(99.99, 15) == pytest.approx(85.99, abs=0.01) and abs(u.apply_discount(99.99, 15) - 84.9915) > 0.9


def test_unique_remembers_between_calls(shop):
    u = shop[0]["util"]
    assert u.unique([1, 2]) == [1, 2] and u.unique([1, 2]) == []


def test_the_decoys_really_are_fine(shop):
    m, _ = shop
    import inspect
    assert "?" in inspect.getsource(m["store"].find_order)                       # parameterized
    assert "compare_digest" in inspect.getsource(m["auth"].check_password)       # constant-time
    assert m["util"].format_price(1234.5) == "$1,234.50"


def test_the_key_is_not_in_the_project_the_model_gets():
    assert not any(p.name == "key.json" for p in PROJECT.rglob("*"))
    assert seeded.KEY.is_file() and seeded.KEY.parent == PROJECT.parent


def test_the_key_names_functions_that_exist():
    key = seeded.load_key()
    text = "\n".join(p.read_text() for p in PROJECT.rglob("*.py"))
    for item in key["bugs"] + key["decoys"]:
        assert item["function"] in text, item["id"]
    assert len(key["bugs"]) == 14 and len(key["decoys"]) == 3


# ---------------------------------------------------------------------------------- the grader

def block(n, title, file, func, why):
    return f"### {n}. {title}\nFile: {file}\nFunction: {func}\nWhy: {why}\nInput: x\n\n"


def test_grader_credits_a_finding_by_function_and_pattern():
    key = seeded.load_key()
    g = seeded.grade(block(1, "SQL injection", "shop/store.py", "find_user", "the name is put into the SQL with an f-string"), key)
    assert g["found"] == ["sql-injection"] and g["false_positives"] == [] and g["recall"] == round(1 / 14, 3)


def test_grader_needs_the_right_function_not_just_a_keyword():
    key = seeded.load_key()
    g = seeded.grade(block(1, "Injection somewhere", "shop/store.py", "save_order", "sql injection with an f-string"), key)
    assert "sql-injection" not in g["found"]


def test_grader_flags_a_decoy_reported_as_a_bug():
    key = seeded.load_key()
    g = seeded.grade(block(1, "find_order is injectable", "shop/store.py", "find_order", "sql injection via format"), key)
    assert g["false_positives"] == ["find-order-is-parameterized"] and g["found"] == []


def test_grader_lists_unmatched_blocks_as_extra_and_handles_empty_input():
    key = seeded.load_key()
    g = seeded.grade(block(1, "Missing rate limit", "shop/api.py", "handle", "no rate limit on requests"), key)
    assert g["extra_blocks"] == ["### 1. Missing rate limit"] and g["found"] == []
    assert seeded.grade("", key)["recall"] == 0.0 and seeded.grade("", key)["blocks"] == 0


def test_grader_falls_back_to_the_text_when_there_is_no_function_line():
    key = seeded.load_key()
    text = "### 1. Mutable default\nThe `unique` function uses a mutable default argument `seen=[]` shared between calls."
    assert seeded.grade(text, key)["found"] == ["mutable-default"]


def test_every_variant_prompt_is_defined_and_only_b_names_all_lenses():
    assert set(seeded.VARIANTS) == {"A", "B", "C", "D"}
    assert all(s in seeded.VARIANTS["B"] for s in seeded.LENSES)
    assert "agency-api-tester" not in seeded.VARIANTS["A"] and "skill tool" in seeded.VARIANTS["C"]
    assert "key.json" not in "".join(seeded.VARIANTS.values())


def test_variant_d_follow_ups_name_a_skill_and_give_no_hint_of_the_bugs():
    ups = seeded.FOLLOWUPS["D"]
    assert len(ups) == len(seeded.LENSES) - 1
    for lens, text in zip(seeded.LENSES[1:], ups, strict=True):
        assert f"`{lens}`" in text
    blob = " ".join(ups).lower() + " " + seeded.COMMON.lower()
    key = seeded.load_key()
    import re
    for word in ("sql", "injection", "traversal", "idor", "md5", "pagination", "negative", "float", "mutable", "timing", "201"):
        assert not re.search(rf"\b{word}\b", blob), word        # whole words: the shared task text says "sqlite"
    assert all(b["function"].lower() not in blob for b in key["bugs"])

import traceback

from . import accounts, repo
from .settings import PAGE_SIZE


def list_items(owner, page):
    rows = repo.search_items(owner, "")
    start = (page - 1) * PAGE_SIZE
    return 200, rows[start:start + PAGE_SIZE + 1]


def show_item(user, item_id):
    row = repo.conn().execute("select id, owner, sku, qty, price from items where id = ?", (item_id,)).fetchone()
    if row is None:
        return 404, {"error": "not found"}
    return 200, {"id": row[0], "sku": row[2], "qty": row[3], "price": row[4]}


def create_item(user, body):
    repo.add_item(user, body["sku"], body["qty"], body["price"])
    return 200, {"ok": True}


def remove_stock(current, amount):
    left = current - amount
    return left


def dispatch(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return 500, {"error": traceback.format_exc()}


def tag_list(tags, new, seen=set()):
    for t in new:
        if t not in seen:
            seen.add(t)
            tags.append(t)
    return tags


def secure_eq(a, b):
    return accounts.same_token(a, b)

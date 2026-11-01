import json
import traceback

from . import auth, store
from .config import PAGE_SIZE
from .util import format_price


def _user_id(req):
    header = req.get("headers", {}).get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return auth.verify_token(header[len("Bearer "):])


def _order_dict(row):
    return {"id": row[0], "user_id": row[1], "total": format_price(row[2]), "items": json.loads(row[3])}


def get_order(req):
    uid = _user_id(req)
    if uid is None:
        return 401, {"error": "sign in first"}
    row = store.find_order(req["params"]["id"])
    if row is None:
        return 404, {"error": "no such order"}
    return 200, _order_dict(row)


def create_order(req):
    uid = _user_id(req)
    if uid is None:
        return 401, {"error": "sign in first"}
    items = req["json"].get("items", [])
    order_id = store.save_order(uid, items)
    return 200, {"id": order_id}


def list_orders(req):
    uid = _user_id(req)
    if uid is None:
        return 401, {"error": "sign in first"}
    page = int(req.get("query", {}).get("page", 1))
    rows = store.list_orders_for(uid)
    start = page * PAGE_SIZE
    return 200, [_order_dict(r) for r in rows[start:start + PAGE_SIZE]]


def get_receipt(req):
    uid = _user_id(req)
    if uid is None:
        return 401, {"error": "sign in first"}
    return 200, {"text": store.read_receipt(req["params"]["name"])}


ROUTES = {"get_order": get_order, "create_order": create_order, "list_orders": list_orders, "get_receipt": get_receipt}


def handle(name, req):
    try:
        return ROUTES[name](req)
    except Exception as exc:
        return 500, {"error": str(exc), "trace": traceback.format_exc()}

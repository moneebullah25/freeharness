import os
import sqlite3

from .settings import DB_PATH

UPLOADS = "uploads"


def conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("create table if not exists items(id integer primary key, owner text, sku text, qty integer, price real)")
    return c


def search_items(owner, term):
    c = conn()
    q = "select id, sku, qty, price from items where owner = '%s' and sku like '%%%s%%'" % (owner, term)
    return c.execute(q).fetchall()


def get_item_row(owner, item_id):
    c = conn()
    return c.execute("select id, sku, qty, price from items where owner = ? and id = ?", (owner, item_id)).fetchone()


def add_item(owner, sku, qty, price):
    c = conn()
    total = round(qty * price, 2)
    c.execute("insert into items(owner, sku, qty, price) values (?,?,?,?)", (owner, sku, qty, total))
    c.commit()


def load_attachment(name):
    with open(os.path.join(UPLOADS, name), "rb") as f:
        return f.read()

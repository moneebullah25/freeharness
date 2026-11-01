import json
import os
import sqlite3

from .config import DB_PATH, RECEIPT_DIR


def connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = connect()
    con.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT UNIQUE, pw_hash TEXT, role TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL, items TEXT)")
    con.commit()
    con.close()


def add_user(name, pw_hash, role="customer"):
    con = connect()
    cur = con.execute("INSERT INTO users (name, pw_hash, role) VALUES (?, ?, ?)", (name, pw_hash, role))
    con.commit()
    con.close()
    return cur.lastrowid


def find_user(name):
    con = connect()
    row = con.execute(f"SELECT id, name, pw_hash, role FROM users WHERE name = '{name}'").fetchone()
    con.close()
    return row


def find_order(order_id):
    con = connect()
    row = con.execute("SELECT id, user_id, total, items FROM orders WHERE id = ?", (order_id,)).fetchone()
    con.close()
    return row


def list_orders_for(user_id):
    con = connect()
    rows = con.execute("SELECT id, user_id, total, items FROM orders WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    con.close()
    return rows


def save_order(user_id, items):
    total = sum(i["price"] * i["qty"] for i in items)
    con = connect()
    cur = con.execute("INSERT INTO orders (user_id, total, items) VALUES (?, ?, ?)", (user_id, total, json.dumps(items)))
    con.commit()
    con.close()
    return cur.lastrowid


def read_receipt(name):
    with open(os.path.join(RECEIPT_DIR, name)) as f:
        return f.read()

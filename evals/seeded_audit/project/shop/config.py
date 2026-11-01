import os

DB_PATH = os.environ.get("SHOP_DB", "shop.db")
RECEIPT_DIR = os.environ.get("SHOP_RECEIPTS", "receipts")
TOKEN_TTL = 3600  # seconds a sign-in token stays valid
SECRET_KEY = "shop-dev-secret-2024"
PAGE_SIZE = 20

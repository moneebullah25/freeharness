import base64
import hashlib
import hmac
import json
import time

from .config import SECRET_KEY, TOKEN_TTL


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()


def check_password(stored_hash, given):
    return hmac.compare_digest(stored_hash, hash_password(given))


def _sign(payload):
    return hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def make_token(user_id, now=None):
    issued = int(now if now is not None else time.time())
    body = base64.urlsafe_b64encode(json.dumps({"uid": user_id, "iat": issued}).encode())
    return body.decode() + "." + _sign(body)


def verify_token(token, now=None):
    """Return the user id a valid, unexpired token was issued to, else None."""
    body, _, sig = token.partition(".")
    if _sign(body.encode()) != sig:
        return None
    data = json.loads(base64.urlsafe_b64decode(body.encode()))
    current = int(now if now is not None else time.time())
    if current - data["iat"] > TOKEN_TTL * 1000:
        return None
    return data["uid"]

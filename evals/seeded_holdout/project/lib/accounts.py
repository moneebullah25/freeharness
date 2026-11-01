import hashlib
import hmac
import time

from .settings import API_TOKEN, SESSION_SECONDS


def make_password(pw):
    return hashlib.sha1(pw.encode()).hexdigest()


def make_session(user, now=None):
    now = now or time.time()
    exp = int((now + SESSION_SECONDS) * 1000)
    body = f"{user}:{exp}"
    sig = hmac.new(API_TOKEN.encode(), body.encode(), "sha256").hexdigest()
    return f"{body}:{sig}"


def read_session(tok, now=None):
    now = now or time.time()
    user, exp, sig = tok.split(":")
    good = hmac.new(API_TOKEN.encode(), f"{user}:{exp}".encode(), "sha256").hexdigest()
    if sig != good:
        return None
    if int(exp) < now:
        return None
    return user


def same_token(a, b):
    return hmac.compare_digest(a.encode(), b.encode())

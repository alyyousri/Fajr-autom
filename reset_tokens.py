"""
reset_tokens.py
روابط إعادة تعيين كلمة المرور الموقّعة (بدون تخزين): بتتبعت في الإيميل لصاحب الحساب بس،
وبتنتهي بعد وقت محدد، ومفيهاش غير الإيميل ورقم عشوائي.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(secret):
    """مفتاح التوقيع مشتق من السر ومفصول بغرض الاستخدام (مش نفس السر بتاع الـ webhook مباشرة)"""
    return hmac.new(secret.encode("utf-8"), b"fajr-password-reset-v1", hashlib.sha256).digest()


def _sign(secret, body):
    return _b64(hmac.new(_key(secret), body.encode("ascii"), hashlib.sha256).digest())


def create_token(secret, email, ttl_seconds, now=None):
    payload = {
        "e": str(email).strip().lower(),
        "x": int((time.time() if now is None else now) + ttl_seconds),
        "n": secrets.token_urlsafe(9),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(secret, body)}"


def verify_token(secret, token, now=None):
    """يرجع {'e': email, 'x': انتهاء, 'n': nonce} لو التوقيع سليم والرابط ما انتهاش، وإلا None"""
    try:
        body, signature = str(token).split(".")
        if not hmac.compare_digest(signature.encode("ascii"), _sign(secret, body).encode("ascii")):
            return None
        payload = json.loads(_unb64(body))
        if not (isinstance(payload.get("e"), str) and isinstance(payload.get("n"), str)
                and isinstance(payload.get("x"), int)):
            return None
        if payload["x"] < (time.time() if now is None else now):
            return None
        return payload
    except Exception:  # أي شكل غلط للتوكن = مرفوض
        return None

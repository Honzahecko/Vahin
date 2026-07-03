# -*- coding: utf-8 -*-
"""
VAPID klíče + odesílání Web Push notifikací.
Ukládá raw base64url private key (d) – univerzálně kompatibilní s pywebpush.
"""
import os, json, base64, logging
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

log = logging.getLogger("vahin.push")

DATA_DIR      = os.path.join(os.path.dirname(__file__), "..", "data")
VAPID_KEY_B64 = os.path.join(DATA_DIR, "vapid_key.b64")   # soubor s raw base64url klíčem
VAPID_CLAIMS  = {"sub": "mailto:admin@vahin.cz"}

_private_key_b64: str = None   # base64url raw private key (pro pywebpush)
_public_key_b64:  str = None   # base64url uncompressed public key (pro frontend)


def init_vapid():
    global _private_key_b64, _public_key_b64
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(VAPID_KEY_B64):
        try:
            with open(VAPID_KEY_B64) as f:
                _private_key_b64 = f.read().strip()
            # Obnov public key z uloženého private key
            d_bytes = base64.urlsafe_b64decode(_private_key_b64 + "==")
            d_int   = int.from_bytes(d_bytes, "big")
            private_key = ec.derive_private_key(d_int, ec.SECP256R1())
            log.info("[VAHIN Push] VAPID klíč načten")
        except Exception as e:
            log.warning("[VAHIN Push] Starý klíč neplatný (%s), generuji nový", e)
            _private_key_b64 = None
            private_key = None
    else:
        private_key = None

    if private_key is None:
        private_key = ec.generate_private_key(ec.SECP256R1())
        d_bytes = private_key.private_numbers().private_value.to_bytes(32, "big")
        _private_key_b64 = base64.urlsafe_b64encode(d_bytes).rstrip(b"=").decode()
        with open(VAPID_KEY_B64, "w") as f:
            f.write(_private_key_b64)
        log.info("[VAHIN Push] Nové VAPID klíče vygenerovány → %s", VAPID_KEY_B64)

    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    _public_key_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()
    log.info("[VAHIN Push] Public key: %s…", _public_key_b64[:24])


def get_public_key() -> str:
    if _public_key_b64 is None:
        init_vapid()
    return _public_key_b64


def send_push(endpoint: str, p256dh: str, auth: str,
              title: str, body: str, url: str = "/") -> bool:
    if _private_key_b64 is None:
        init_vapid()
    from pywebpush import webpush, WebPushException
    sub     = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
    payload = json.dumps({"title": title, "body": body, "url": url})
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=_private_key_b64,   # raw base64url d-value
            vapid_claims=VAPID_CLAIMS,
        )
        return True
    except WebPushException as e:
        resp = getattr(e, "response", None)
        log.warning("[Push] WebPushException: %s | status: %s", e,
                    resp.status_code if resp is not None else "?")
        return False
    except Exception as e:
        log.error("[Push] Chyba: %s", e)
        return False

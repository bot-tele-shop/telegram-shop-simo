"""Fernet decryption via WebCrypto, so delivered codes stay encrypted at rest
exactly like the local bot (cryptography.Fernet: AES-128-CBC + HMAC-SHA256).
No Python packages required — the Workers runtime provides crypto.subtle.

WebCrypto's AES-CBC decrypt already verifies and strips PKCS#7 padding and
rejects bad padding with an error. Do NOT unpad the result a second time:
that corrupts every valid plaintext (the old code raised
"stock record padding invalid" for real tokens).
"""

import base64
import hashlib
import hmac

from js import Object, Uint8Array, crypto
from pyodide.ffi import to_js

# version (1) + timestamp (8) + iv (16) + at least one ciphertext block (16) + hmac (32)
_MIN_TOKEN_LEN = 1 + 8 + 16 + 16 + 32


class Fernet:
    def __init__(self, key_b64):
        key = base64.urlsafe_b64decode(str(key_b64).encode())
        if len(key) != 32:
            raise ValueError("STOCK_FERNET_KEY must be a 32-byte urlsafe base64 key")
        self.signing = key[:16]
        self.encryption = key[16:]

    async def decrypt(self, token):
        raw = base64.urlsafe_b64decode(str(token).encode())
        if len(raw) < _MIN_TOKEN_LEN or raw[0] != 0x80:
            raise ValueError("stock record token malformed")
        body, sig = raw[:-32], raw[-32:]
        expected = hmac.new(self.signing, body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("stock record failed integrity check")
        iv, ct = body[9:25], body[25:]
        if len(ct) == 0 or len(ct) % 16 != 0:
            raise ValueError("stock record token malformed")

        algo = Object.new()
        algo.name = "AES-CBC"
        key = await crypto.subtle.importKey(
            "raw", to_js(self.encryption), algo, False, to_js(["decrypt"])
        )
        params = Object.new()
        params.name = "AES-CBC"
        params.iv = to_js(iv)
        try:
            plain = await crypto.subtle.decrypt(params, key, to_js(ct))
        except Exception:
            # WebCrypto already checked PKCS#7 padding; a failure here means
            # corrupted ciphertext — never the caller's plaintext to fix up.
            raise ValueError("stock record padding invalid") from None
        # crypto.subtle.decrypt returns the unpadded plaintext: decode as-is.
        return bytes(Uint8Array.new(plain).to_py()).decode("utf-8")

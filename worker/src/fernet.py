"""Fernet decryption via WebCrypto, so delivered codes stay encrypted at rest
exactly like the local bot (cryptography.Fernet: AES-128-CBC + HMAC-SHA256).
No Python packages required — the Workers runtime provides crypto.subtle.
"""

import base64
import hashlib
import hmac

from js import Object, Uint8Array, crypto
from pyodide.ffi import to_js


class Fernet:
    def __init__(self, key_b64):
        key = base64.urlsafe_b64decode(str(key_b64).encode())
        if len(key) != 32:
            raise ValueError("STOCK_FERNET_KEY must be a 32-byte urlsafe base64 key")
        self.signing = key[:16]
        self.encryption = key[16:]

    async def decrypt(self, token):
        raw = base64.urlsafe_b64decode(str(token).encode())
        body, sig = raw[:-32], raw[-32:]
        expected = hmac.new(self.signing, body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("stock record failed integrity check")
        iv, ct = body[9:25], body[25:]

        algo = Object.new()
        algo.name = "AES-CBC"
        key = await crypto.subtle.importKey(
            "raw", to_js(self.encryption), algo, False, to_js(["decrypt"])
        )
        params = Object.new()
        params.name = "AES-CBC"
        params.iv = to_js(iv)
        plain = await crypto.subtle.decrypt(params, key, to_js(ct))
        data = bytes(Uint8Array.new(plain).to_py())
        pad = data[-1]
        if pad < 1 or pad > 16:
            raise ValueError("stock record padding invalid")
        return data[:-pad].decode("utf-8")

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
import time

from js import Object, Uint8Array, crypto
from pyodide.ffi import to_js

# version (1) + timestamp (8) + iv (16) + at least one ciphertext block (16) + hmac (32)
_MIN_TOKEN_LEN = 1 + 8 + 16 + 16 + 32


def fingerprint_of(key_b64, payload):
    """Dedup fingerprint, matching shop/store.py: HMAC-SHA256 over the
    plaintext with SHA-256 of the base64 key string as the key."""
    fp_key = hashlib.sha256(str(key_b64).encode()).digest()
    return hmac.new(fp_key, payload.encode(), hashlib.sha256).hexdigest()


class Fernet:
    def __init__(self, key_b64):
        self.key_b64 = str(key_b64)
        key = base64.urlsafe_b64decode(self.key_b64.encode())
        if len(key) != 32:
            raise ValueError("STOCK_FERNET_KEY must be a 32-byte urlsafe base64 key")
        self.signing = key[:16]
        self.encryption = key[16:]

    def fingerprint(self, payload):
        return fingerprint_of(self.key_b64, payload)

    async def encrypt(self, plaintext):
        """Encrypt like cryptography.Fernet (AES-128-CBC + HMAC-SHA256), so
        rows uploaded here are readable by both the Worker and Python."""
        # WebCrypto AES-CBC encrypt applies PKCS#7 padding itself — pass the
        # raw plaintext (pre-padding here would produce double padding).
        data = str(plaintext).encode()

        iv_arr = Uint8Array.new(16)
        crypto.getRandomValues(iv_arr)
        iv = bytes(iv_arr.to_py())

        algo = Object.new()
        algo.name = "AES-CBC"
        key = await crypto.subtle.importKey(
            "raw", to_js(self.encryption), algo, False, to_js(["encrypt"])
        )
        params = Object.new()
        params.name = "AES-CBC"
        params.iv = to_js(iv)
        ct = bytes(Uint8Array.new(await crypto.subtle.encrypt(params, key, to_js(data))).to_py())

        body = b"\x80" + int(time.time()).to_bytes(8, "big") + iv + ct
        sig = hmac.new(self.signing, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + sig).decode()

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

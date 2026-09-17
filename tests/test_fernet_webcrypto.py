"""Regression tests for worker/src/fernet.py against REAL WebCrypto.

The old Worker code unpadded plaintext a second time after WebCrypto's AES-CBC
decrypt had already removed PKCS#7 padding, so every genuine Fernet token
failed with "stock record padding invalid". These tests encrypt with Python
cryptography.Fernet (what stock uploads use) and decrypt through Node's real
WebCrypto implementation (tests/fernet_bridge.mjs), which mirrors the fixed
worker/src/fernet.py line for line. No mocks reproduce WebCrypto behavior here.
"""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet as PyFernet

BRIDGE = Path(__file__).parent / "fernet_bridge.mjs"

node_available = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(not node_available, reason="node runtime required")


def run_bridge(cases):
    """Send {key, token} cases through the Node WebCrypto bridge."""
    proc = subprocess.run(
        ["node", str(BRIDGE)],
        input="\n".join(json.dumps(c) for c in cases),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture
def key():
    return PyFernet.generate_key().decode()


def token_for(key_b64, plaintext):
    return PyFernet(key_b64.encode()).encrypt(plaintext.encode()).decode()


SAMPLES = [
    "ABCD-1234-EFGH-5678",  # printable license code
    "https://download.example.com/file.iso?token=abc123&expires=9999999999",
    "x",  # 1 byte: PKCS#7 adds a full 16-byte block — the old code ate real data
    "sixteen_bytes_!!",  # exactly one block of plaintext
    "seventeen_bytes!!!",
    "a" * 255,
    "Clé-de-licence-€-✓",  # non-ASCII UTF-8
    "https://example.com/" + "q" * 120,
]


def test_real_webcrypto_roundtrips_python_fernet_tokens(key):
    cases = [{"key": key, "token": token_for(key, s)} for s in SAMPLES]
    results = run_bridge(cases)
    for sample, result in zip(SAMPLES, results):
        assert result == {"ok": True, "plaintext": sample}, (sample, result)


def test_wrong_key_is_rejected(key):
    other = PyFernet.generate_key().decode()
    token = token_for(key, "secret-code-123")
    (result,) = run_bridge([{"key": other, "token": token}])
    assert result["ok"] is False
    # HMAC is checked before any decryption: integrity failure, not padding noise.
    assert "integrity" in result["error"]


def test_tampered_ciphertext_and_signature_are_rejected(key):
    token = token_for(key, "https://example.com/license")
    raw = bytearray(base64.urlsafe_b64decode(token))

    flipped_ct = bytearray(raw)
    flipped_ct[30] ^= 0x01  # inside the ciphertext, breaks HMAC too
    flipped_sig = bytearray(raw)
    flipped_sig[-1] ^= 0x01  # signature only

    tampered = [
        {"key": key, "token": base64.urlsafe_b64encode(bytes(b)).decode()}
        for b in (flipped_ct, flipped_sig)
    ]
    for result in run_bridge(tampered):
        assert result["ok"] is False


def test_malformed_and_truncated_tokens_are_rejected(key):
    token = token_for(key, "code")
    raw = base64.urlsafe_b64decode(token)
    cases = [
        {"key": key, "token": base64.urlsafe_b64encode(raw[:40]).decode()},  # truncated
        {"key": key, "token": base64.urlsafe_b64encode(b"\x81" + raw[1:]).decode()},  # bad version
        {"key": key, "token": base64.urlsafe_b64encode(b"").decode()},  # empty
    ]
    for result in run_bridge(cases):
        assert result["ok"] is False
        assert "malformed" in result["error"] or "integrity" in result["error"]


def test_fixed_worker_source_has_no_second_unpad():
    """Guard against regressing worker/src/fernet.py back to manual unpadding."""
    src = (Path(__file__).parents[1] / "worker/src/fernet.py").read_text()
    assert "data[:-pad]" not in src
    assert "pad = data[-1]" not in src

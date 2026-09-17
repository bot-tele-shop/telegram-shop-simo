// Test bridge: decrypts cryptography.Fernet tokens using REAL Node WebCrypto,
// mirroring worker/src/fernet.py exactly (HMAC-SHA256 verify, AES-CBC decrypt,
// UTF-8 decode, NO second unpadding — WebCrypto already strips PKCS#7).
// Reads JSON lines {key, token} on stdin, writes {ok, plaintext|error} lines.

import { createHmac, timingSafeEqual } from 'node:crypto';
import readline from 'node:readline';

const MIN_TOKEN_LEN = 1 + 8 + 16 + 16 + 32;

async function decrypt(keyB64, tokenB64) {
  const key = Buffer.from(keyB64, 'base64url');
  if (key.length !== 32) throw new Error('bad key length');
  const signing = key.subarray(0, 16);
  const encryption = key.subarray(16);

  const raw = Buffer.from(tokenB64, 'base64url');
  if (raw.length < MIN_TOKEN_LEN || raw[0] !== 0x80) throw new Error('token malformed');
  const body = raw.subarray(0, raw.length - 32);
  const sig = raw.subarray(raw.length - 32);
  const expected = createHmac('sha256', signing).update(body).digest();
  if (!timingSafeEqual(sig, expected)) throw new Error('failed integrity check');

  const iv = body.subarray(9, 25);
  const ct = body.subarray(25);
  if (ct.length === 0 || ct.length % 16 !== 0) throw new Error('token malformed');

  const subtle = globalThis.crypto.subtle; // real WebCrypto, not a mock
  const cryptoKey = await subtle.importKey('raw', encryption, { name: 'AES-CBC' }, false, ['decrypt']);
  let plain;
  try {
    plain = await subtle.decrypt({ name: 'AES-CBC', iv }, cryptoKey, ct);
  } catch {
    throw new Error('padding invalid');
  }
  return Buffer.from(plain).toString('utf-8');
}

const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  if (!line.trim()) continue;
  const { key, token } = JSON.parse(line);
  try {
    process.stdout.write(JSON.stringify({ ok: true, plaintext: await decrypt(key, token) }) + '\n');
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: err.message }) + '\n');
  }
}

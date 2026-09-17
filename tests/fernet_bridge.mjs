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

async function encrypt(keyB64, plaintext) {
  // Mirrors Fernet.encrypt in worker/src/fernet.py (WebCrypto AES-CBC with
  // explicit PKCS#7 padding, Fernet token layout, HMAC-SHA256 signature).
  const key = Buffer.from(keyB64, 'base64url');
  if (key.length !== 32) throw new Error('bad key length');
  const signing = key.subarray(0, 16);
  const encryption = key.subarray(16);

  // WebCrypto AES-CBC encrypt applies PKCS#7 padding itself.
  const data = Buffer.from(plaintext, 'utf-8');
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(16));

  const subtle = globalThis.crypto.subtle;
  const cryptoKey = await subtle.importKey('raw', encryption, { name: 'AES-CBC' }, false, ['encrypt']);
  const ct = Buffer.from(await subtle.encrypt({ name: 'AES-CBC', iv }, cryptoKey, data));

  const ts = Buffer.alloc(8);
  ts.writeBigInt64BE(BigInt(Math.floor(Date.now() / 1000)));
  const body = Buffer.concat([Buffer.from([0x80]), ts, Buffer.from(iv), ct]);
  const sig = createHmac('sha256', signing).update(body).digest();
  // urlsafe base64 WITH padding, matching Python's urlsafe_b64encode
  return Buffer.concat([body, sig]).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_');
}

const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  if (!line.trim()) continue;
  const req = JSON.parse(line);
  try {
    if (req.op === 'encrypt') {
      process.stdout.write(JSON.stringify({ ok: true, token: await encrypt(req.key, req.plaintext) }) + '\n');
    } else {
      process.stdout.write(JSON.stringify({ ok: true, plaintext: await decrypt(req.key, req.token) }) + '\n');
    }
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: err.message }) + '\n');
  }
}

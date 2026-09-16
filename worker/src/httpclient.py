"""Outbound HTTP via pyfetch (available in Python Workers)."""

import json

from pyodide.http import pyfetch


class HttpError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"HTTP {status}: {body[:200]}")


async def request(method, url, headers=None, payload=None, timeout=25):
    kwargs = {"method": method}
    if headers:
        kwargs["headers"] = headers
    if payload is not None:
        kwargs["body"] = json.dumps(payload)
    resp = await pyfetch(url, **kwargs)
    if resp.status >= 400:
        body = await resp.string()
        raise HttpError(resp.status, body)
    text = await resp.string()
    return json.loads(text) if text else None

"""Digital Shelf webhook Worker.

Telegram sends updates to POST /webhook/<WEBHOOK_SECRET>.
GET /health answers "ok" for uptime checks.

Runtime notes (Cloudflare Python Workers, internal SDK):
- The handler is a module-level `on_fetch(request, env)` function.
- Every value coming from the JS runtime (request, env) is a JsProxy —
  always str()/to_py() it before using it as a Python object.
"""

from urllib.parse import urlparse

import flow
from workers import Response


async def on_fetch(request, env):
    url = urlparse(str(request.url))
    if url.path == "/health":
        return Response("ok")
    expected = f"/webhook/{str(env.WEBHOOK_SECRET)}"
    if str(request.method) == "POST" and url.path == expected:
        try:
            update = (await request.json()).to_py()
            await flow.handle_update(update, env)
        except Exception as exc:
            # Log and still 200: bad payloads must not trigger retry storms.
            print(f"webhook error: {type(exc).__name__}: {exc}")
        return Response('{"ok": true}', headers={"Content-Type": "application/json"})
    return Response("not found", status=404)

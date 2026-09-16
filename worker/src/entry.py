"""Digital Shelf webhook Worker.

Telegram sends updates to POST /webhook/<WEBHOOK_SECRET>.
GET /health answers "ok" for uptime checks.

Note: every value coming from the JS runtime (request, env) is a JsProxy —
always str()/to_py() it before using it as a Python object.
"""

from urllib.parse import urlparse

import flow
from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(str(request.url))
        if url.path == "/health":
            return Response("ok")
        expected = f"/webhook/{str(self.env.WEBHOOK_SECRET)}"
        if str(request.method) == "POST" and url.path == expected:
            try:
                update = (await request.json()).to_py()
                await flow.handle_update(update, self.env)
            except Exception as exc:
                # Log and still 200: bad payloads must not trigger retry storms.
                print(f"webhook error: {type(exc).__name__}: {exc}")
            return Response('{"ok": true}', headers={"Content-Type": "application/json"})
        return Response("not found", status=404)

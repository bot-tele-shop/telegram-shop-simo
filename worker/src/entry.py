"""Digital Shelf webhook Worker.

Telegram sends updates to POST /webhook/<WEBHOOK_SECRET>.
GET /health answers "ok" for uptime checks.
"""

from urllib.parse import urlparse

import flow
from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        if url.path == "/health":
            return Response("ok")
        expected = f"/webhook/{self.env.WEBHOOK_SECRET}"
        if request.method == "POST" and url.path == expected:
            try:
                update = await request.json()
                await flow.handle_update(update, self.env)
            except Exception as exc:
                # Log and still 200: bad payloads must not trigger retry storms.
                print(f"webhook error: {type(exc).__name__}: {exc}")
            return Response.json({"ok": True})
        return Response("not found", status=404)

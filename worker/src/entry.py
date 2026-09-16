"""Digital Shelf webhook Worker.

TEMPORARY DEBUG BUILD: fetch is wrapped so any exception is returned in the
response body instead of a bare error 1101. Remove the wrapper once healthy.
"""

import traceback
from urllib.parse import urlparse

import flow
from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            url = urlparse(str(request.url))
            if url.path == "/health":
                return Response("ok")
            expected = f"/webhook/{str(self.env.WEBHOOK_SECRET)}"
            if str(request.method) == "POST" and url.path == expected:
                try:
                    update = (await request.json()).to_py()
                    await flow.handle_update(update, self.env)
                except Exception as exc:
                    print(f"webhook error: {type(exc).__name__}: {exc}")
                return Response('{"ok": true}', headers={"Content-Type": "application/json"})
            return Response("not found", status=404)
        except Exception:
            return Response("debug:\n" + traceback.format_exc(), status=500)

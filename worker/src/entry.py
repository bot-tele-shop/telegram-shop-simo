"""Digital Shelf webhook Worker.

Telegram sends updates to POST /webhook/<WEBHOOK_SECRET>.
GET /health answers "ok" for uptime checks.

Failure contract:
- Unparseable payloads and already-processed updates are acknowledged (200).
- Transient failures (durable capture failed, concurrent processing) answer
  500 so Telegram redelivers; durable state makes every retry idempotent.

Runtime notes (verified against the live 2026 runtime):
- Entrypoint is `class Default(WorkerEntrypoint)` with an `on_fetch` method
  (the runtime's entrypoint helper calls `on_fetch`, not `fetch`).
- Keep compatibility_date on the bundled-SDK track (see wrangler.toml); newer
  dates unbundle the `workers` module and require vendoring workers-py.
- JS values may arrive as JsProxy or already converted to Python natives
  depending on the runtime version — use str() for scalars and convert
  request.json() with to_py() only when needed.
"""

from urllib.parse import parse_qs, urlparse

import admin as admin_api
import flow
from workers import Response, WorkerEntrypoint


def _json(payload, status=200, headers=None):
    import json

    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return Response(json.dumps(payload), status=status, headers=h)


class Default(WorkerEntrypoint):
    def _cors(self):
        origin = str(getattr(self.env, "DASHBOARD_ORIGIN", "") or "")
        if not origin:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Vary": "Origin",
        }

    async def on_fetch(self, request):
        url = urlparse(str(request.url))
        if url.path == "/health":
            return Response("ok")

        # Private admin API for the dashboard (Supabase Auth + owner allowlist).
        if url.path.startswith("/admin/api/"):
            cors = self._cors()
            if str(request.method) == "OPTIONS":
                return Response("", status=204, headers=cors)
            action = url.path[len("/admin/api/"):]
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                status, payload = await admin_api.handle_admin(request, self.env, action, query)
            except admin_api.AdminError as exc:
                status, payload = exc.status, {"ok": False, "error": str(exc)}
            except Exception as exc:
                print(f"admin error: {type(exc).__name__}: {exc}")
                status, payload = 500, {"ok": False, "error": "internal error"}
            return _json(payload, status=status, headers=cors)

        expected = f"/webhook/{str(self.env.WEBHOOK_SECRET)}"
        if str(request.method) == "POST" and url.path == expected:
            # Authenticate Telegram's secret-token header when configured.
            # Rollout: register the webhook with secret_token first, then set
            # WEBHOOK_HEADER_SECRET; until then the path secret gates access.
            required = getattr(self.env, "WEBHOOK_HEADER_SECRET", None)
            if required:
                header = request.headers.get("x-telegram-bot-api-secret-token") or ""
                if str(header) != str(required):
                    return Response("forbidden", status=403)
            try:
                update = await request.json()
                if hasattr(update, "to_py"):
                    update = update.to_py()
            except Exception:
                # Bad payloads must not trigger retry storms: acknowledge.
                return Response(
                    '{"ok": true}', headers={"Content-Type": "application/json"}
                )
            try:
                await flow.handle_update(update, self.env)
            except Exception as exc:
                # Retryable failure: Telegram redelivers; the durable inbox
                # makes redelivery safe. Never log update contents or secrets.
                print(f"webhook error: {type(exc).__name__}: {exc}")
                return Response(
                    '{"ok": false}', status=500, headers={"Content-Type": "application/json"}
                )
            return Response('{"ok": true}', headers={"Content-Type": "application/json"})
        return Response("not found", status=404)

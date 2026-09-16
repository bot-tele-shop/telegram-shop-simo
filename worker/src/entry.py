"""Minimal bisect build: no imports, no env access, no parsing."""

from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("ok")

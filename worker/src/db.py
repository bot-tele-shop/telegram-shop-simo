"""Supabase PostgREST client. The service role key stays server-side only."""

from httpclient import request


class DB:
    def __init__(self, url, service_key):
        self.base = url
        self.rest = f"{url}/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    async def request_auth_user(self, token):
        """Validate a Supabase Auth user JWT. Returns the user dict or None."""
        try:
            return await request(
                "GET",
                f"{self.base}/auth/v1/user",
                headers={"apikey": self.headers["apikey"],
                         "Authorization": f"Bearer {token}"},
            )
        except Exception:
            return None

    async def rpc(self, fn, args):
        return await request("POST", f"{self.rest}/rpc/{fn}", headers=self.headers, payload=args)

    async def select(self, table, params, limit=100):
        params = dict(params)
        params["limit"] = str(limit)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return await request("GET", f"{self.rest}/{table}?{qs}", headers=self.headers)

    async def select_one(self, table, params):
        rows = await self.select(table, params, limit=1)
        return rows[0] if rows else None

    async def insert(self, table, row):
        return await request("POST", f"{self.rest}/{table}", headers=self.headers, payload=row)

    async def upsert(self, table, row, on_conflict):
        headers = dict(self.headers)
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        return await request(
            "POST", f"{self.rest}/{table}?on_conflict={on_conflict}", headers=headers, payload=row
        )

    async def update(self, table, params, row):
        headers = dict(self.headers)
        headers["Prefer"] = "return=minimal"
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return await request("PATCH", f"{self.rest}/{table}?{qs}", headers=headers, payload=row)

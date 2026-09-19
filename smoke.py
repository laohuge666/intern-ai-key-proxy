import asyncio
import os

import httpx

UP = os.environ["UPSTREAM_API_KEYS"]
PROXY = os.environ["PROXY_API_KEY"]


async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=60) as c:
        r = await c.get("/health")
        print("HEALTH", r.status_code, r.text[:200])
        r = await c.get("/v1/keys", headers={"Authorization": f"Bearer {PROXY}"})
        print("KEYS", r.status_code, r.text[:200])
        r = await c.get("/v1/models", headers={"Authorization": f"Bearer {PROXY}"})
        print("MODELS", r.status_code, r.text[:300])
        r = await c.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        print("BADAUTH", r.status_code, r.text[:200])


asyncio.run(main())

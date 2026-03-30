import asyncio
import time

import httpx


async def test():
    proxies = "http://127.0.0.1:7890"
    url = "https://douyin.wtf/api/hybrid/video_data"
    params = {"url": "https://v.douyin.com/test", "minimal": "false"}

    # Test 1: with proxy
    try:
        start = time.time()
        async with httpx.AsyncClient(proxy=proxies, timeout=10) as c:
            r = await c.get(url, params=params)
        print(f"With proxy: HTTP {r.status_code} in {time.time()-start:.1f}s")
    except Exception as e:
        print(f"With proxy: FAILED in {time.time()-start:.1f}s - {type(e).__name__}: {e}")

    # Test 2: without proxy (direct)
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, params=params)
        print(f"Direct: HTTP {r.status_code} in {time.time()-start:.1f}s")
    except Exception as e:
        print(f"Direct: FAILED in {time.time()-start:.1f}s - {type(e).__name__}: {e}")


asyncio.run(test())

import asyncio
import sys
sys.path.insert(0, '.')

# Mock ad.txt content
class MockHttpxClient:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def get(self, url, timeout=None):
        class MockResp:
            text = """电报导航:https://dianbaodaohang.com
机场大全:https://vpnnav.github.io
搜索机器人:https://t.me/jiso?start=a_7202424896
搜索机器人:https://t.me/soso?start=a_7202424896"""
            def raise_for_status(self):
                pass
        return MockResp()

# Monkey patch
import services.ad as ad_mod
import httpx
original_client = httpx.AsyncClient
httpx.AsyncClient = MockHttpxClient

async def test():
    from services.ad import fetch_ads, get_random_ad
    
    ads = await fetch_ads()
    print(f"✅  获取到 {len(ads)} 条广告")
    for label, url in ads:
        print(f"   - {label}: {url}")
    
    random_ad = await get_random_ad()
    if random_ad:
        label, url = random_ad
        print(f"\n✅ 随机广告: {label} -> {url}")

asyncio.run(test())

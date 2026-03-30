import random
import time

import httpx

from log import logger

logger = logger.bind(name="AdService")

AD_URL = "https://raw.githubusercontent.com/itgoyo/TelegramBot/refs/heads/master/ad.txxt"

_ad_cache: list[tuple[str, str]] = []
_cache_time: float = 0
_CACHE_TTL = 3600  # 1 hour


async def fetch_ads() -> list[tuple[str, str]]:
    global _ad_cache, _cache_time
    if _ad_cache and time.time() - _cache_time < _CACHE_TTL:
        return _ad_cache

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(AD_URL, timeout=10)
            resp.raise_for_status()
            lines = resp.text.strip().splitlines()
            ads = []
            for line in lines:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                label, url = line.split(":", 1)
                label = label.strip()
                url = url.strip()
                if label and url:
                    ads.append((label, url))
            if ads:
                _ad_cache = ads
                _cache_time = time.time()
            return ads
    except Exception as e:
        logger.warning(f"获取广告数据失败: {e}")
        return _ad_cache


async def get_random_ad() -> tuple[str, str] | None:
    ads = await fetch_ads()
    if not ads:
        return None
    return random.choice(ads)

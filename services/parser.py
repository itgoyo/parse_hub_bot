from parsehub import ParseHub, Platform
from parsehub.types import (
    AnyParseResult,
)

from core import pl_cfg
from log import logger
from services.platform_tokens import PlatformTokenStore

logger = logger.bind(name="ParseService")

# Platforms that support user-provided token fallback
_TOKEN_SUPPORTED_PLATFORMS = {"twitter", "bilibili", "youtube"}

# Error keywords that indicate a timeout (retrying is usually pointless)
_TIMEOUT_KEYWORDS = ("超时", "timeout", "timed out", "ReadTimeout", "ConnectTimeout")


class ParseService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self.parser = ParseHub()

    def get_platform(self, url: str) -> Platform:
        p = self.parser.get_platform(url)
        if not p:
            raise ValueError("不支持的平台")
        return p

    @staticmethod
    def _is_timeout_error(err_str: str) -> bool:
        return any(kw in err_str for kw in _TIMEOUT_KEYWORDS)

    async def parse(self, url: str) -> AnyParseResult:
        logger.debug(f"开始解析 {url}")
        p = self.get_platform(url)

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                cookie = pl_cfg.roll_cookie(p.id)
                # Fallback to token store when no platform config cookie
                if p.id in _TOKEN_SUPPORTED_PLATFORMS and not cookie:
                    store = PlatformTokenStore(p.id)
                    store_cookie = store.get_cookie_str()
                    if store_cookie:
                        cookie = store_cookie
                        logger.debug(f"使用 TokenStore 中的 {p.id} Cookie")
                proxy = pl_cfg.roll_parser_proxy(p.id)
                logger.debug(f"使用配置: proxy={proxy}, cookie={cookie}, attempt={attempt}/{max_retries}")
                pr = await self.parser.parse(url, cookie=cookie, proxy=proxy)
                logger.debug(f"解析完成: {pr}")
                return pr
            except Exception as e:
                err_str = str(e)

                # Token invalid: remove it and retry with next
                if p.id in _TOKEN_SUPPORTED_PLATFORMS and cookie and PlatformTokenStore.is_auth_error(p.id, err_str):
                    store = PlatformTokenStore(p.id)
                    store.remove_token_by_cookie_str(cookie)
                    logger.warning(f"{p.id} Cookie 失效, 已删除, 尝试下一个")
                    cookie = None
                    if attempt < max_retries and store.has_tokens():
                        continue

                # Timeout: no point retrying, fail fast
                if self._is_timeout_error(err_str):
                    logger.warning(f"解析超时, 跳过重试: {e}")
                    raise Exception(e) from e

                logger.warning(f"解析失败, attempt={attempt}/{max_retries}, err={e}")
                if attempt >= max_retries:
                    raise Exception(e) from e
        raise

    async def get_raw_url(self, url: str, clean_all: bool = True) -> str:
        p = self.get_platform(url)

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                proxy = pl_cfg.roll_parser_proxy(p.id)
                logger.debug(f"使用配置: proxy={proxy}, attempt={attempt}/{max_retries}")
                raw_url = await self.parser.get_raw_url(url, proxy=proxy, clean_all=clean_all)
                logger.debug(f"原始 URL: {raw_url}")
                return raw_url
            except Exception as e:
                err_str = str(e)
                # Timeout: fail fast
                if self._is_timeout_error(err_str):
                    logger.warning(f"获取原始 URL 超时, 跳过重试: {e}")
                    raise Exception(e) from e

                logger.warning(f"获取原始 URL 失败, attempt={attempt}/{max_retries}, err={e}")
                if attempt >= max_retries:
                    raise Exception(e) from e
        raise

import json
import re
from pathlib import Path

from core import bs
from log import logger

logger = logger.bind(name="PlatformTokenStore")


TWITTER_TOKEN_TUTORIAL = (
    "**▎Twitter Cookie 获取教程**\n\n"
    "由于该推文需要登录才能查看，请提供您的 Twitter Cookie。\n\n"
    "**获取步骤：**\n"
    "1. 打开浏览器，登录 [Twitter/X](https://x.com)\n"
    "2. 按 `F12` 打开开发者工具\n"
    "3. 切换到 **Application**（应用）标签页\n"
    "4. 在左侧找到 **Cookies** → `https://x.com`\n"
    "5. 找到 `auth_token` 和 `ct0` 两个值\n"
    "6. 按以下格式发送给我：\n\n"
    "`auth_token=你的auth_token值; ct0=你的ct0值`\n\n"
    "**示例：**\n"
    "`auth_token=abc123def456; ct0=xyz789`\n\n"
    "⚠️ 请注意保护您的 Cookie，不要泄露给不信任的人。\n"
    "Token 提交后将用于解析受限推文，失效后会自动删除。"
)

BILIBILI_TOKEN_TUTORIAL = (
    "**▎Bilibili Cookie 获取教程**\n\n"
    "由于该内容触发了B站风控限制，请提供您的 Bilibili Cookie。\n\n"
    "**获取步骤：**\n"
    "1. 打开浏览器，登录 [哔哩哔哩](https://www.bilibili.com)\n"
    "2. 按 `F12` 打开开发者工具\n"
    "3. 切换到 **Application**（应用）标签页\n"
    "4. 在左侧找到 **Cookies** → `https://www.bilibili.com`\n"
    "5. 找到 `SESSDATA` 和 `bili_jct` 两个值\n"
    "6. 按以下格式发送给我：\n\n"
    "`SESSDATA=你的SESSDATA值; bili_jct=你的bili_jct值`\n\n"
    "**示例：**\n"
    "`SESSDATA=abc123%2C1234567890%2Cabcde*01; bili_jct=xyz789abcdef`\n\n"
    "⚠️ 请注意保护您的 Cookie，不要泄露给不信任的人。\n"
    "Token 提交后将用于解析受限内容，失效后会自动删除。"
)

YOUTUBE_TOKEN_TUTORIAL = (
    "**▎YouTube Cookie 获取教程**\n\n"
    "由于 YouTube 要求登录验证，请提供您的 YouTube Cookie 以继续解析。\n\n"
    "**方法一：使用浏览器插件导出（推荐）**\n"
    "1. 安装浏览器插件 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
    "2. 打开浏览器，登录 [YouTube](https://www.youtube.com)\n"
    "3. 点击插件图标，选择 **Export** 导出当前站点的 Cookie\n"
    "4. 打开导出的 txt 文件，复制全部内容发送给我\n\n"
    "**方法二：手动获取关键 Cookie**\n"
    "1. 打开浏览器，登录 [YouTube](https://www.youtube.com)\n"
    "2. 按 `F12` 打开开发者工具\n"
    "3. 切换到 **Application**（应用）标签页\n"
    "4. 在左侧找到 **Cookies** → `https://www.youtube.com`\n"
    "5. 找到以下 Cookie 值：\n"
    "   - `SID`\n"
    "   - `HSID`\n"
    "   - `SSID`\n"
    "   - `APISID`\n"
    "   - `SAPISID`\n"
    "   - `LOGIN_INFO`\n"
    "6. 按以下格式发送给我：\n\n"
    "`SID=值; HSID=值; SSID=值; APISID=值; SAPISID=值; LOGIN_INFO=值`\n\n"
    "**示例：**\n"
    "`SID=abc123; HSID=def456; SSID=ghi789; APISID=jkl012; SAPISID=mno345; LOGIN_INFO=AFmmF2swR...`\n\n"
    "⚠️ 请注意保护您的 Cookie，不要泄露给不信任的人。\n"
    "Cookie 提交后将用于解析 YouTube 视频，失效后会自动删除。"
)


# Platform config: required keys, cookie format, unique key for dedup
PLATFORM_TOKEN_CONFIG = {
    "twitter": {
        "required_keys": ["auth_token", "ct0"],
        "unique_key": "auth_token",
    },
    "bilibili": {
        "required_keys": ["SESSDATA", "bili_jct"],
        "unique_key": "SESSDATA",
    },
    "youtube": {
        "required_keys": ["SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"],
        "unique_key": "SAPISID",
    },
}

PLATFORM_TUTORIALS = {
    "twitter": TWITTER_TOKEN_TUTORIAL,
    "bilibili": BILIBILI_TOKEN_TUTORIAL,
    "youtube": YOUTUBE_TOKEN_TUTORIAL,
}

# Error keywords that indicate auth/cookie is needed
PLATFORM_AUTH_ERROR_KEYWORDS = {
    "twitter": ["error -2", "匿名用户无法查看"],
    "bilibili": ["风控", "-352", "动态不可见", "安全风控策略"],
    "youtube": ["Sign in to confirm you're not a bot", "Sign in to confirm your age", "This video requires authentication", "Use --cookies-from-browser or --cookies"],
}

# YouTube cookie expired keywords (cookie was passed but is no longer valid)
YOUTUBE_COOKIE_EXPIRED_KEYWORDS = [
    "cookies are no longer valid",
    "cookies have likely been rotated",
]

# Rate limit keywords
RATE_LIMIT_KEYWORDS = ["429", "Too Many Requests"]


class PlatformTokenStore:
    """Generic token store supporting multiple platforms with round-robin rotation."""

    _instances: dict[str, "PlatformTokenStore"] = {}

    def __new__(cls, platform_id: str):
        if platform_id not in cls._instances:
            instance = super().__new__(cls)
            instance._platform_id = platform_id
            instance._tokens: list[dict] = []
            instance._current_index: int = 0
            instance._file_path = bs.config_path / f"{platform_id}_tokens.json"
            instance._load()
            cls._instances[platform_id] = instance
        return cls._instances[platform_id]

    def _load(self):
        if self._file_path.exists():
            try:
                data = json.loads(self._file_path.read_text(encoding="utf-8"))
                self._tokens = data if isinstance(data, list) else []
                logger.debug(f"已加载 {len(self._tokens)} 个 {self._platform_id} Token")
            except Exception as e:
                logger.warning(f"加载 {self._platform_id} Token 失败: {e}")
                self._tokens = []

    def _save(self):
        self._file_path.write_text(
            json.dumps(self._tokens, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @property
    def _config(self) -> dict:
        return PLATFORM_TOKEN_CONFIG.get(self._platform_id, {"required_keys": [], "unique_key": ""})

    def add_token(self, token_dict: dict) -> bool:
        unique_key = self._config["unique_key"]
        new_val = token_dict.get(unique_key)
        for t in self._tokens:
            if t.get(unique_key) == new_val:
                t.update(token_dict)
                self._save()
                logger.info(f"更新已有 {self._platform_id} Token: {unique_key}={new_val[:8]}...")
                return False
        self._tokens.append(token_dict)
        self._save()
        logger.info(
            f"新增 {self._platform_id} Token: {unique_key}={new_val[:8]}..., 当前共 {len(self._tokens)} 个"
        )
        return True

    def get_cookie_str(self) -> str | None:
        if not self._tokens:
            return None
        idx = self._current_index % len(self._tokens)
        t = self._tokens[idx]
        self._current_index = (idx + 1) % max(len(self._tokens), 1)
        return "; ".join(f"{k}={v}" for k, v in t.items())

    def remove_token_by_cookie_str(self, cookie_str: str):
        unique_key = self._config["unique_key"]
        m = re.search(rf"{re.escape(unique_key)}=([^;\s]+)", cookie_str)
        if m:
            self._remove_by_unique(m.group(1))

    def _remove_by_unique(self, value: str):
        unique_key = self._config["unique_key"]
        before = len(self._tokens)
        self._tokens = [t for t in self._tokens if t.get(unique_key) != value]
        if len(self._tokens) < before:
            if self._current_index >= len(self._tokens) and self._tokens:
                self._current_index = 0
            self._save()
            logger.info(
                f"已删除失效 {self._platform_id} Token: {unique_key}={value[:8]}..., 剩余 {len(self._tokens)} 个"
            )

    def has_tokens(self) -> bool:
        return len(self._tokens) > 0

    def count(self) -> int:
        return len(self._tokens)

    @staticmethod
    def parse_cookie_input(text: str, platform_id: str) -> dict | None:
        config = PLATFORM_TOKEN_CONFIG.get(platform_id)
        if not config:
            return None
        result = {}
        for key in config["required_keys"]:
            m = re.search(rf"{re.escape(key)}\s*=\s*([^;\s]+)", text)
            if not m:
                return None
            result[key] = m.group(1)
        return result

    @staticmethod
    def is_auth_error(platform_id: str, error_str: str) -> bool:
        keywords = PLATFORM_AUTH_ERROR_KEYWORDS.get(platform_id, [])
        return any(kw in error_str for kw in keywords)

    @staticmethod
    def is_cookie_expired(platform_id: str, error_str: str) -> bool:
        """Check if the error indicates cookies were passed but are expired/invalid."""
        if platform_id == "youtube":
            return any(kw in error_str for kw in YOUTUBE_COOKIE_EXPIRED_KEYWORDS)
        return False

    @staticmethod
    def is_rate_limited(error_str: str) -> bool:
        return any(kw in error_str for kw in RATE_LIMIT_KEYWORDS)

    @staticmethod
    def get_tutorial(platform_id: str) -> str | None:
        return PLATFORM_TUTORIALS.get(platform_id)


# Backward compatibility aliases
class TwitterTokenStore(PlatformTokenStore):
    def __new__(cls):
        return PlatformTokenStore.__new__(PlatformTokenStore, "twitter")


class BilibiliTokenStore(PlatformTokenStore):
    def __new__(cls):
        return PlatformTokenStore.__new__(PlatformTokenStore, "bilibili")

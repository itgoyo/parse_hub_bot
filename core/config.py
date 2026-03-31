import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from parsehub.config import GlobalConfig
from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(...)
    api_id: str = Field(...)
    api_hash: str = Field(...)
    bot_proxy: dict | None = Field(default=None)
    data_path: Path = Path("data")
    cache_time: int = Field(default=30 * 24 * 60 * 60, description="0 为永久缓存, 默认缓存一个月")
    debug: bool = Field(default=False)
    debug_skip_cleanup: bool = Field(default=False, description="跳过资源清理")

    douyin_api: HttpUrl | None = None

    # MySQL
    mysql_host: str = Field(default="127.0.0.1")
    mysql_port: int = Field(default=3306)
    mysql_user: str = Field(default="root")
    mysql_password: str = Field(default="")
    mysql_db: str = Field(default="parse_hub_bot")
    daily_free_quota: int = Field(default=5, description="每日免费解析次数")
    ad_bonus_quota: int = Field(default=5, description="点击广告奖励解析次数")

    def model_post_init(self, __context) -> None:
        """模型初始化后的操作"""
        self.sessions_path.mkdir(parents=True, exist_ok=True)
        self.cache_path.mkdir(parents=True, exist_ok=True)
        self.config_path.mkdir(parents=True, exist_ok=True)

    @property
    def sessions_path(self) -> Path:
        return self.data_path / "sessions"

    @property
    def cache_path(self) -> Path:
        return self.data_path / "cache"

    @property
    def config_path(self) -> Path:
        return self.data_path / "config"

    @field_validator("bot_proxy", mode="before")
    @classmethod
    def proxy_config(cls, v: str | None = None) -> dict | None:
        url = urlparse(v) if v else None
        if not url:
            return None
        return {
            "scheme": url.scheme,
            "hostname": url.hostname,
            "port": url.port,
            "username": url.username,
            "password": url.password,
        }

    @property
    def bot_session_name(self) -> str:
        return f"bot_{self.bot_token.split(':')[0]}"

    @field_validator("data_path", mode="before")
    @classmethod
    def data_path_init(cls, v):
        p = Path(v) if isinstance(v, str) else v
        p.mkdir(exist_ok=True, parents=True)
        return p


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        env_prefix="WD_",
    )
    is_running: bool = Field(default=False)
    """运行中"""
    restart_count: int = Field(default=0)
    """重启次数"""
    disconnect_count: int = Field(default=0)
    """断开连接次数"""
    max_disconnect_count: int = Field(default=3)
    """最大断开连接次数, 超过后重启"""
    remove_session_after_restart: int = Field(default=3)
    """重启失败几次后删除会话文件"""
    max_restart_count: int = Field(default=6)
    """意外断开连接时，最大重启次数"""
    exit_flag: bool = Field(default=False)
    """退出标志"""

    def update_bot_restart_count(self):
        self.restart_count += 1
        os.environ["WD_RESTART_COUNT"] = str(self.restart_count)

    def reset_bot_restart_count(self):
        self.restart_count = 0
        os.environ["WD_RESTART_COUNT"] = "0"

    def update_bot_disconnect_count(self):
        self.disconnect_count += 1
        os.environ["WD_DISCONNECT_COUNT"] = str(self.disconnect_count)

    def reset_bot_disconnect_count(self):
        self.disconnect_count = 0
        os.environ["WD_DISCONNECT_COUNT"] = "0"


bs = BotSettings()
ws = WatchdogSettings()

if bs.douyin_api:
    GlobalConfig.douyin_api = bs.douyin_api


# ── Monkey-patch: 使用直接抓取 iesdouyin.com 替代 douyin.wtf API ──
def _patch_douyin_parser():
    """Patch DouyinParser.parse_api to scrape iesdouyin.com directly
    instead of relying on the (often unreliable) douyin.wtf API.

    Approach adapted from plugins/douyin.py (DouyinVideoClient).
    Supports video, image, and live-photo posts.
    Falls back to douyin.wtf API for TikTok URLs if DOUYIN_API is configured.
    """
    try:
        import re as _re
        import json

        import httpx as _httpx
        import json_repair as _json_repair
        from parsehub.parsers.parser.douyin import DouyinParser, DYResult, DYType
        from parsehub.types import (
            ImageRef,
            LivePhotoRef,
            ParseError as _ParseError,
            VideoRef,
        )

        _ROUTER_DATA_RE = _re.compile(
            r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", _re.S | _re.I
        )
        _MOBILE_UA = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
            "Mobile/15E148 Safari/604.1"
        )

        _original_parse_api = DouyinParser.parse_api

        @staticmethod
        async def _patched_parse_api(url: str) -> DYResult:
            # TikTok URLs: fall back to the original API approach if configured
            if "tiktok.com" in url:
                if GlobalConfig.douyin_api:
                    return await _tiktok_api_fallback(url)
                raise _ParseError("TikTok 解析需要配置 DOUYIN_API")

            from core.platform_config import PlatformsConfig

            proxy = PlatformsConfig().roll_parser_proxy("douyin")
            headers = {"User-Agent": _MOBILE_UA}

            try:
                # Step 1: 跟随重定向获取视频 ID
                async with _httpx.AsyncClient(
                    timeout=30, proxy=proxy, headers=headers
                ) as client:
                    resp = await client.get(url, follow_redirects=False)
                    location = resp.headers.get("Location")
                    if not location:
                        resp = await client.get(url, follow_redirects=True)
                        location = str(resp.url)

                    vid_match = _re.search(r"/(\d{15,})", location)
                    if not vid_match:
                        # Fallback: first long digit sequence
                        vid_match = _re.search(r"(\d{10,})", location)
                    if not vid_match:
                        raise _ParseError("抖音解析失败: 无法获取视频ID")
                    vid = vid_match.group(1)

                    # Step 2: 获取分享页面
                    resp = await client.get(
                        f"https://www.iesdouyin.com/share/video/{vid}",
                        follow_redirects=True,
                    )
                    if resp.status_code != 200:
                        raise _ParseError(
                            f"抖音解析失败: HTTP {resp.status_code}"
                        )

            except (_httpx.ReadTimeout, _httpx.ConnectTimeout) as e:
                raise _ParseError("抖音解析超时") from e

            # Step 3: 提取 window._ROUTER_DATA
            m = _ROUTER_DATA_RE.search(resp.text)
            if not m:
                raise _ParseError("抖音解析失败: 无法获取页面数据")

            raw_str = m.group(1).strip().rstrip("; \n\r\t")
            if not raw_str.startswith("{"):
                idx = raw_str.find("{")
                if idx == -1:
                    raise _ParseError("抖音解析失败: 页面数据格式异常")
                raw_str = raw_str[idx:].rstrip("; \n\r\t")

            try:
                page_data = json.loads(raw_str)
            except json.JSONDecodeError:
                page_data = _json_repair.loads(raw_str)

            # Step 4: 提取视频详情
            video_detail = (
                page_data
                .get("loaderData", {})
                .get("video_(id)/page", {})
                .get("videoInfoRes", {})
                .get("item_list", [None])
            )
            if not video_detail or not video_detail[0]:
                raise _ParseError("抖音解析失败: 未找到视频数据")
            video_detail = video_detail[0]

            platform = "douyin" if "douyin" in url else "tiktok"
            desc = video_detail.get("desc", "")

            # Step 5: 构建 DYResult
            # ── 图文帖子 ──
            if images := video_detail.get("images"):
                image_list = _build_image_list(images)
                return DYResult(
                    type=DYType.IMAGE,
                    desc=desc,
                    image_list=image_list,
                    platform=platform,
                )

            if image_post_info := video_detail.get("image_post_info"):
                imgs = image_post_info.get("images", [])
                image_list = [
                    ImageRef(url=img["display_image"]["url_list"][-1])
                    for img in imgs
                    if img.get("display_image", {}).get("url_list")
                ]
                return DYResult(
                    type=DYType.IMAGE,
                    desc=desc,
                    image_list=image_list,
                    platform=platform,
                )

            # ── 视频帖子 ──
            vd = video_detail.get("video", {})
            play_addr = vd.get("play_addr", {})
            cover = vd.get("cover", {})

            # 优先使用 URI 构建下载地址（更稳定）
            uri = play_addr.get("uri")
            if uri:
                video_url = (
                    f"http://www.iesdouyin.com/aweme/v1/play/"
                    f"?video_id={uri}&ratio=1080p&line=0"
                )
            else:
                url_list = play_addr.get("url_list", [])
                if url_list:
                    video_url = url_list[0]
                else:
                    raise _ParseError("抖音解析失败: 未获取到视频下载地址")

            thumb_list = cover.get("url_list", [])
            thumb_url = thumb_list[-1] if thumb_list else None
            width = play_addr.get("width", 0)
            height = play_addr.get("height", 0)
            duration = vd.get("duration", 0)

            return DYResult(
                type=DYType.VIDEO,
                video=VideoRef(
                    url=video_url,
                    thumb_url=thumb_url,
                    width=width,
                    height=height,
                    duration=duration,
                ),
                desc=desc,
                platform=platform,
            )

        def _build_image_list(
            images: list[dict],
        ) -> list[ImageRef | LivePhotoRef]:
            """从 images 数组构建图片/实况照片列表"""
            result: list[ImageRef | LivePhotoRef] = []
            for image in images:
                if video_data := image.get("video"):
                    # 实况照片
                    pa = video_data.get("play_addr", {})
                    uri = pa.get("uri")
                    if uri:
                        v_url = (
                            f"http://www.iesdouyin.com/aweme/v1/play/"
                            f"?video_id={uri}&ratio=1080p&line=0"
                        )
                    else:
                        v_urls = pa.get("url_list", [])
                        v_url = v_urls[0] if v_urls else ""
                    cover_urls = video_data.get("cover", {}).get("url_list", [])
                    result.append(
                        LivePhotoRef(
                            url=cover_urls[-1] if cover_urls else "",
                            video_url=v_url,
                            width=pa.get("width", 0),
                            height=pa.get("height", 0),
                            duration=video_data.get("duration", 3),
                        )
                    )
                else:
                    url_list = image.get("url_list", [])
                    if url_list:
                        result.append(ImageRef(url=url_list[-1]))
            return result

        async def _tiktok_api_fallback(url: str) -> DYResult:
            """TikTok URLs: use douyin.wtf API as fallback."""
            from core.platform_config import PlatformsConfig

            proxy = PlatformsConfig().roll_parser_proxy("douyin")

            async with _httpx.AsyncClient(timeout=30, proxy=proxy) as client:
                params = {"url": url, "minimal": False}
                try:
                    response = await client.get(
                        f"{str(GlobalConfig.douyin_api).rstrip('/')}"
                        f"/api/hybrid/video_data",
                        params=params,
                    )
                except (_httpx.ReadTimeout, _httpx.ConnectTimeout) as e:
                    raise _ParseError("TikTok 解析超时") from e
            if response.status_code != 200:
                raise _ParseError("TikTok 解析失败")
            return DYResult.parse(url, response.json())

        DouyinParser.parse_api = _patched_parse_api
    except ImportError:
        pass


_patch_douyin_parser()

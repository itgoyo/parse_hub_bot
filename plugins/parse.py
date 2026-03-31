import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from itertools import batched
from typing import Literal

from parsehub.types import (
    AniFile,
    AnyMediaRef,
    ImageFile,
    LivePhotoFile,
    PostType,
    VideoFile,
)
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)

from core import bs
from log import logger
from plugins.filters import platform_filter
from plugins.helpers import (
    ProcessedMedia,
    build_caption,
    build_caption_by_str,
    create_richtext_telegraph,
    resolve_media_info,
)
from services import ParseService
from services.ad import get_random_ad
from services.cache import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult, parse_cache, persistent_cache
from services.db import check_and_consume_quota, get_remaining_quota, log_ad_click, log_parse, upsert_user
from services.pipeline import ParsePipeline, PipelineResult, StatusReporter
from services.platform_tokens import PlatformTokenStore
from utils.helpers import pack_dir_to_tar_gz, to_list, with_request_id

logger = logger.bind(name="Parse")
SKIP_DOWNLOAD_THRESHOLD = 0
MAX_RETRIES = 5

# Platforms that support user-provided token input
_TOKEN_SUPPORTED_PLATFORMS = {"twitter", "bilibili", "youtube"}

# Users waiting to input platform token: {chat_id: {url, mode, platform_id}}
_token_waiting: dict[int, dict] = {}

# 配额广告映射: user_id -> {ad_label, ad_url, shown_at}
_pending_ad_claims: dict[int, dict] = {}


async def _get_ad_markup() -> InlineKeyboardMarkup | None:
    ad = await get_random_ad()
    if not ad:
        return None
    label, url = ad
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])


async def _send_quota_ad(msg: Message) -> None:
    """发送配额广告：展示广告链接，提示用户查看后发送 /claim 领取额度。"""
    ad = await get_random_ad()
    if not ad:
        await msg.reply_text(
            f"**▎今日解析次数已用完** 🚫\n\n"
            f"每日免费 {bs.daily_free_quota} 次，明天再来吧！"
        )
        return

    label, url = ad
    user_id = msg.from_user.id
    _pending_ad_claims[user_id] = {
        "ad_label": label,
        "ad_url": url,
        "shown_at": time.time(),
    }

    await msg.reply_text(
        f"**▎今日解析次数已用完** 🚫\n\n"
        f"每日免费 {bs.daily_free_quota} 次，点击下方广告链接查看后，发送 /claim 即可获得额外 **{bs.ad_bonus_quota}** 次额度。",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📢 {label}", url=url)],
        ]),
    )


async def _send_ad_button(msg: Message) -> None:
    markup = await _get_ad_markup()
    if markup:
        await _send_with_rate_limit(lambda: msg.reply_text("📢 推荐", reply_markup=markup, quote=False))


async def _send_with_rate_limit[T](
    send_coro_fn: Callable[[], Awaitable[T]],
) -> T:
    """带自动重试的发送包装器。

    Args:
        send_coro_fn: 返回协程的可调用对象（lambda 或函数），每次重试会重新调用
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await send_coro_fn()
        except FloodWait as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"FloodWait 重试 ({attempt + 1}/{MAX_RETRIES})，等待 {e.value}s")
                await asyncio.sleep(e.value)
            else:
                raise e from e
    return None


class MessageStatusReporter(StatusReporter):
    """基于 Telegram Message 的状态报告器"""

    def __init__(self, user_msg: Message):
        self._user_msg = user_msg
        self._msg = None

    async def report(self, text: str) -> None:
        await self._edit_text(f"**▎{text}**")

    async def report_error(self, stage: str, error: Exception) -> None:
        await self._edit_text(
            f"**▎{stage}错误:** \n```\n{error}```",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

        async def fn():
            await asyncio.sleep(15)
            await self._msg.delete()

        loop = asyncio.get_running_loop()
        loop.create_task(fn())

    async def dismiss(self) -> None:
        if self._msg:
            await self._msg.delete()

    async def _edit_text(self, text: str, **kwargs):
        try:
            if self._msg is None:
                self._msg = await self._user_msg.reply_text(text, **kwargs)
            else:
                if self._msg.text != text:
                    await self._msg.edit_text(text, **kwargs)
                    self._msg.text = text
        except FloodWait:
            pass


# ── 广告额度领取 /claim ───────────────────────────────────────────

_CLAIM_MIN_WAIT = 8  # 秒，展示广告后至少等待时间


@Client.on_message(filters.command("claim"))
async def handle_claim(cli: Client, msg: Message):
    user_id = msg.from_user.id
    pending = _pending_ad_claims.pop(user_id, None)

    if not pending:
        await msg.reply_text("**▎没有可领取的额度** \n请先发送解析链接，当次数用完时会收到广告。")
        return

    elapsed = time.time() - pending["shown_at"]
    if elapsed < _CLAIM_MIN_WAIT:
        # 还没看够时间，放回去
        _pending_ad_claims[user_id] = pending
        wait = int(_CLAIM_MIN_WAIT - elapsed) + 1
        await msg.reply_text(f"**▎请先点击广告链接查看** \n{wait} 秒后再发送 /claim 领取额度。")
        return

    await log_ad_click(user_id, pending["ad_label"])
    remaining = await get_remaining_quota(user_id)
    await msg.reply_text(
        f"**▎额度领取成功** ✅\n\n"
        f"已获得 **{bs.ad_bonus_quota}** 次解析额度，今日剩余 **{remaining}** 次。\n"
        f"现在可以继续发送链接进行解析。"
    )


# ── Handler ──────────────────────────────────────────────────────────


@Client.on_message(filters.command(["jx", "raw", "zip"]) | ((filters.text | filters.caption) & platform_filter))
async def jx(cli: Client, msg: Message):
    mode = "preview"
    if msg.command:
        match msg.command[0]:
            case "raw":
                mode = "raw"
            case "jx":
                mode = "preview"
            case "zip":
                mode = "zip"

        text = " ".join(msg.command[1:]) if msg.command[1:] else ""
        if not text and msg.reply_to_message:
            text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        if not text:
            await msg.reply_text("**▎请加上链接或回复一条消息**")
            return
    else:
        text = msg.text or msg.caption

    text = text.strip().split()
    urls = list({i for i in text if ParseService().parser.get_platform(i)})[:10]

    if not urls:
        await msg.reply_text("**▎不支持的平台**")
        return

    for url in urls:
        await handle_parse(cli, msg, url, mode)


# ── Platform Token 输入处理 ───────────────────────────────────────────

# Cookie key patterns per platform
_PLATFORM_COOKIE_KEYS: dict[str, list[str]] = {
    "twitter": ["auth_token", "ct0"],
    "bilibili": ["SESSDATA", "bili_jct"],
    "youtube": ["SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"],
}

_PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "twitter": "Twitter",
    "bilibili": "Bilibili",
    "youtube": "YouTube",
}


def _detect_platform_from_cookie(text: str) -> str | None:
    """Detect which platform a cookie string belongs to."""
    for platform_id, keys in _PLATFORM_COOKIE_KEYS.items():
        if all(re.search(rf"{re.escape(k)}\s*=\s*\S+", text) for k in keys):
            return platform_id
    return None


def _platform_cookie_filter(_, __, msg: Message):
    text = msg.text or ""
    return _detect_platform_from_cookie(text) is not None


@Client.on_message(filters.private & filters.create(_platform_cookie_filter))
async def handle_platform_token_input(cli: Client, msg: Message):
    text = msg.text.strip()
    platform_id = _detect_platform_from_cookie(text)
    if not platform_id:
        return

    display_name = _PLATFORM_DISPLAY_NAMES.get(platform_id, platform_id)
    keys = _PLATFORM_COOKIE_KEYS[platform_id]

    # Extract each cookie value
    values: dict[str, str] = {}
    for key in keys:
        m = re.search(rf"{re.escape(key)}\s*=\s*([^;\s]+)", text)
        if not m:
            example = "; ".join(f"{k}=xxx" for k in keys)
            await msg.reply_text(f"**▎格式错误，请按以下格式发送：**\n`{example}`")
            return
        values[key] = m.group(1)

    store = PlatformTokenStore(platform_id)
    is_new = store.add_token(values)

    if is_new:
        await msg.reply_text(
            f"**▎{display_name} Token 已保存** ✅\n"
            f"当前共有 {store.count()} 个可用 Token。\n"
            f"现在可以重新发送链接进行解析。"
        )
    else:
        await msg.reply_text(
            f"**▎{display_name} Token 已更新** ✅\n"
            f"当前共有 {store.count()} 个可用 Token。"
        )

    # If user was waiting to parse, retry
    chat_id = msg.chat.id
    if chat_id in _token_waiting:
        waiting = _token_waiting.pop(chat_id)
        await handle_parse(cli, msg, waiting["url"], waiting.get("mode", "preview"))


# ── 主流程 ───────────────────────────────────────────────────────────


@with_request_id
async def handle_parse(
    cli: Client, msg: Message, url: str, mode: Literal["raw", "preview", "zip"] | str = "preview"
) -> None:
    logger.debug(f"收到解析请求: url={url}, chat_id={msg.chat.id}, msg_id={msg.id}, mode={mode}")

    # ── 记录用户 & 配额检查 ──
    user = msg.from_user
    user_id = user.id if user else None
    if user:
        nickname = user.first_name or ""
        if user.last_name:
            nickname += f" {user.last_name}"
        await upsert_user(user.id, nickname)

        allowed, remaining = await check_and_consume_quota(user.id, url)
        if not allowed:
            await _send_quota_ad(msg)
            return

    reporter = MessageStatusReporter(msg)
    match mode:
        case "raw":
            use_caching = False
            skip_media_processing = True
            singleflight = False
            save_metadata = False
        case "zip":
            use_caching = False
            skip_media_processing = True
            singleflight = False
            save_metadata = True
        case _:
            use_caching = True
            skip_media_processing = False
            singleflight = True
            save_metadata = False
    try:
        raw_url = await ParseService().get_raw_url(url)
    except Exception as e:
        await reporter.report_error("获取原始链接", e)
        return

    if use_caching and (cached := await persistent_cache.get(raw_url)):
        logger.debug("file_id 缓存命中, 直接发送")
        await _send_cached(msg, cached, raw_url)
        if user_id:
            await log_parse(user_id, url)
        await _send_ad_button(msg)
        return

    # ── Pre-parse to detect auth errors ──
    cached_parse_result = await parse_cache.get(raw_url)
    if not cached_parse_result:
        try:
            cached_parse_result = await ParseService().parse(url)
            await parse_cache.set(raw_url, cached_parse_result)
        except Exception as e:
            err_str = str(e)
            platform_id = None
            try:
                platform_id = ParseService().get_platform(url).id
            except Exception:
                pass
            if platform_id and platform_id in _TOKEN_SUPPORTED_PLATFORMS and PlatformTokenStore.is_auth_error(platform_id, err_str):
                store = PlatformTokenStore(platform_id)

                # Cookie 已过期（例如 YouTube cookie 被浏览器轮换）
                if store.has_tokens() and PlatformTokenStore.is_cookie_expired(platform_id, err_str):
                    store.remove_token_by_cookie_str(store.get_cookie_str() or "")
                    logger.warning(f"{platform_id} Cookie 已过期, 已删除")
                    _token_waiting[msg.chat.id] = {"url": url, "mode": mode, "platform_id": platform_id}
                    await reporter.dismiss()
                    await msg.reply_text(
                        f"**▎{platform_id} Cookie 已过期** ⚠️\n\n"
                        f"您之前提交的 Cookie 已被浏览器自动轮换失效。\n"
                        f"请重新获取最新的 Cookie 并发送给我。\n\n"
                        f"⚠️ **注意：** 导出 Cookie 后请**不要**在浏览器上继续操作该网站，否则 Cookie 会立即失效。",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                    return

                # IP 被限流（429）
                if PlatformTokenStore.is_rate_limited(err_str):
                    await reporter.report_error(
                        "解析",
                        Exception("YouTube 当前 IP 请求过于频繁 (429)，请稍后再试。这与 Cookie 无关。"),
                    )
                    return

                # 没有 Cookie，提示用户提交
                if not store.has_tokens():
                    _token_waiting[msg.chat.id] = {"url": url, "mode": mode, "platform_id": platform_id}
                    tutorial = PlatformTokenStore.get_tutorial(platform_id)
                    await reporter.dismiss()
                    await msg.reply_text(tutorial, link_preview_options=LinkPreviewOptions(is_disabled=True))
                    return

                # Cookie 存在但其他原因失败
                await reporter.report_error(
                    "解析",
                    Exception(f"{platform_id} 当前访问受限，请稍后重试。已有的 Cookie 仍然有效，无需重新提交。"),
                )
                return
            await reporter.report_error("解析", e)
            return

    pipeline = ParsePipeline(
        url,
        reporter,
        parse_result=cached_parse_result,
        singleflight=singleflight,
        skip_media_processing=skip_media_processing,
        skip_download_threshold=SKIP_DOWNLOAD_THRESHOLD,
        save_metadata=save_metadata,
    )

    if (result := await pipeline.run()) is None:
        if pipeline.waited:
            logger.debug("Singleflight 等待完成, 重新检查缓存")
            if cached := await persistent_cache.get(raw_url):
                await _send_cached(msg, cached, raw_url)
                await _send_ad_button(msg)
            else:
                await handle_parse(cli, msg, url, mode=mode)
                return
        else:
            logger.debug("Pipeline 返回 None, 跳过后续处理")
        return

    parse_result = result.parse_result
    await parse_cache.set(raw_url, parse_result)

    # ── 富文本 → Telegraph ──
    if parse_result.type == PostType.RICHTEXT:
        logger.debug(f"富文本类型, 创建 Telegraph 页面: title={parse_result.title}")
        try:
            await msg.reply_chat_action(enums.ChatAction.TYPING)
            ph_url = await create_richtext_telegraph(cli, parse_result)
            logger.debug(f"Telegraph 页面创建完成: {ph_url}")
            caption = build_caption(parse_result, ph_url)
            await msg.reply_text(
                caption,
                link_preview_options=LinkPreviewOptions(show_above_text=True),
            )
            await persistent_cache.set(
                raw_url,
                CacheEntry(
                    parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content),
                    telegraph_url=ph_url,
                ),
            )
            if user_id:
                await log_parse(user_id, url)
            await reporter.dismiss()
            await _send_ad_button(msg)
            return
        finally:
            pipeline.finish()

    caption = build_caption(parse_result)
    if not result.processed_list:
        logger.debug("无媒体文件, 仅发送文本")
        await msg.reply_chat_action(enums.ChatAction.TYPING)
        await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        cache_entry = CacheEntry(parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content))
        await persistent_cache.set(raw_url, cache_entry)
        if user_id:
            await log_parse(user_id, url)
        await reporter.dismiss()
        await _send_ad_button(msg)
        pipeline.finish()
        return

    if mode == "raw":
        await _send_raw(msg, result, reporter)
        if user_id:
            await log_parse(user_id, url)
        await _send_ad_button(msg)
        return
    if mode == "zip":
        await _send_zip(msg, result, reporter)
        if user_id:
            await log_parse(user_id, url)
        await _send_ad_button(msg)
        return

    # ── 上传媒体 ──
    logger.debug(f"开始上传媒体: media_count={len(result.processed_list)}")
    await reporter.report("上 传 中...")
    try:
        cache_entry = await _send_media(msg, parse_result, result.processed_list, caption)
        if cache_entry:
            await persistent_cache.set(raw_url, cache_entry)
        if user_id:
            await log_parse(user_id, url)
        await reporter.dismiss()
        await _send_ad_button(msg)
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"上传失败: {e}")
        await reporter.report_error("上传", e)
        return
    finally:
        result.cleanup()
        pipeline.finish()


# ── 构建 InputMedia ──────────────────────────────────────────────────


def _build_input_media(
    media_refs: list[AnyMediaRef],
    processed_list: list[ProcessedMedia],
) -> tuple[list[InputMediaPhoto | InputMediaVideo], list[InputMediaAnimation]]:
    """根据处理结果和媒体引用构建 Telegram InputMedia 列表。

    Returns:
        (photos_videos, animations) 两类媒体列表
    """
    photos_videos: list[InputMediaPhoto | InputMediaVideo] = []
    animations: list[InputMediaAnimation] = []

    for media_ref, processed in zip(media_refs, processed_list, strict=False):
        file_paths = processed.output_paths or [processed.source.path]
        for file_path in file_paths:
            file_path_str = str(file_path)
            width, height, duration = resolve_media_info(processed, file_path_str)

            match processed.source:
                case ImageFile():
                    photos_videos.append(InputMediaPhoto(media=file_path_str))
                case AniFile():
                    animations.append(InputMediaAnimation(media=file_path_str))
                case VideoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=file_path_str,
                            video_cover=media_ref.thumb_url,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )
                case LivePhotoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=processed.source.video_path,
                            video_cover=file_path_str,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )

    return photos_videos, animations


# ── 缓存条目构建 ─────────────────────────────────────────────────────


def _cache_media_from_message(m: Message) -> CacheMedia | None:
    """从已发送的 Telegram Message 提取 CacheMedia。"""
    if m.photo:
        return CacheMedia(type=CacheMediaType.PHOTO, file_id=m.photo.file_id)
    if m.video:
        return CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id=m.video.file_id,
            cover_file_id=m.video.video_cover.file_id if m.video.video_cover else None,
        )
    if m.animation:
        return CacheMedia(type=CacheMediaType.ANIMATION, file_id=m.animation.file_id)
    if m.document:
        return CacheMedia(type=CacheMediaType.DOCUMENT, file_id=m.document.file_id)
    return None


def _make_cache_entry(parse_result, media_list: list[CacheMedia]) -> CacheEntry:
    return CacheEntry(
        parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content),
        media=media_list,
    )


# ── Raw 模式上传 ──────────────────────────────────────────────────────


async def _send_raw(
    msg: Message,
    result: PipelineResult,
    reporter: MessageStatusReporter,
) -> None:
    """Raw 模式：将文件以原始文档形式上传。"""
    logger.debug("Raw 模式, 直接上传文件")
    await reporter.report("上 传 中...")
    try:
        caption = build_caption(result.parse_result)
        all_docs: list[InputMediaDocument] = []
        livephoto_videos: dict[int, InputMediaDocument] = {}

        for idx, processed in enumerate(result.processed_list):
            # raw 模式下 processed.output_paths 只有一个文件
            file_path = processed.output_paths[0]
            all_docs.append(InputMediaDocument(media=str(file_path)))
            if isinstance(processed.source, LivePhotoFile):
                livephoto_videos[idx] = InputMediaDocument(media=str(processed.source.video_path))

        if len(all_docs) == 1:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            m = await _send_with_rate_limit(
                lambda: msg.reply_document(all_docs[0].media, caption=caption, force_document=True)
            )
            if livephoto_videos:
                await _send_with_rate_limit(lambda: m.reply_document(livephoto_videos[0].media, force_document=True))
        else:
            msgs: list[Message] = []
            for batch in batched(all_docs, 10):
                await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                # noinspection PyDefaultArgument
                mg = await _send_with_rate_limit(lambda b=list(batch): msg.reply_media_group(b))  # type: ignore
                msgs.extend(mg)
            if livephoto_videos:
                for idx, m in livephoto_videos.items():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                    await _send_with_rate_limit(
                        lambda m_=m, idx_=idx: msgs[idx_].reply_document(m_.media, force_document=True)
                    )
            await _send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )

    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"Raw 模式上传失败: {e}")
        await reporter.report_error("上传", e)
        return
    finally:
        result.cleanup()

    await reporter.dismiss()


async def _send_zip(
    msg: Message,
    result: PipelineResult,
    reporter: MessageStatusReporter,
) -> None:
    logger.debug("Zip 模式, 开始打包")
    await reporter.report("打 包 中...")
    try:
        caption = build_caption(result.parse_result)
        pack_path = pack_dir_to_tar_gz(result.output_dir)
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"打包失败: {e}")
        await reporter.report_error("打包", Exception("..."))
        return
    finally:
        result.cleanup()

    await reporter.report("上 传 中...")
    try:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
        await _send_with_rate_limit(lambda: msg.reply_document(str(pack_path), caption=caption))
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"上传失败: {e}")
        await reporter.report_error("上传", e)
        return
    finally:
        if not bs.debug_skip_cleanup:
            logger.debug("清理压缩包")
            os.remove(pack_path)

    await reporter.dismiss()


# ── 发送媒体 ─────────────────────────────────────────────────────────


async def _send_single(
    msg: Message,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
) -> list[CacheMedia] | None:
    """发送单个媒体，返回 CacheMedia 列表。上传失败时降级为 document。
    返回 None 表示不缓存
    """
    media_list: list[CacheMedia] = []
    all_media = animations + photos_videos

    try:
        if animations:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            sent = await _send_with_rate_limit(lambda: msg.reply_animation(animations[0].media, caption=caption))
        else:
            single = photos_videos[0]
            match single:
                case InputMediaPhoto():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
                    sent = await _send_with_rate_limit(lambda: msg.reply_photo(single.media, caption=caption))
                case InputMediaVideo():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)
                    sent = await _send_with_rate_limit(
                        lambda: msg.reply_video(
                            single.media,
                            caption=caption,
                            video_cover=single.video_cover,
                            duration=single.duration,
                            width=single.width,
                            height=single.height,
                            supports_streaming=True,
                        )
                    )

        if sent and (cm := _cache_media_from_message(sent)):
            media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
        await _send_with_rate_limit(
            lambda: msg.reply_document(all_media[0].media, caption=caption, force_document=True)
        )
        return None

    return media_list


async def _send_multi(
    msg: Message,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
) -> list[CacheMedia] | None:
    """发送多个媒体（动图逐条、图片视频分批），返回 CacheMedia 列表。
    返回 None 表示不缓存
    """
    media_list: list[CacheMedia] = []
    not_cache = False

    for ani in animations:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        caption_ = caption if ani == animations[-1] and not photos_videos else ""
        try:
            sent = await _send_with_rate_limit(
                lambda a=ani, c=caption_: msg.reply_animation(
                    a.media,
                    caption=c,
                )
            )
        except Exception as e:
            logger.warning(f"上传失败 {e}, 使用兼容模式上传")
            not_cache = True
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            await _send_with_rate_limit(
                lambda a=ani, c=caption_: msg.reply_document(a.media, caption=c, force_document=True)
            )
        else:
            # 过大的 GIF 会返回 document
            if sent.document:
                media_list.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=sent.document.file_id))
            else:
                media_list.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=sent.animation.file_id))

    try:
        for batch in batched(photos_videos, 10):
            if batch[-1] == photos_videos[-1]:
                batch[0].caption = caption

            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            # noinspection PyDefaultArgument
            sent_msgs = await _send_with_rate_limit(lambda b=list(batch): msg.reply_media_group(media=b))
            for m in sent_msgs:
                if cm := _cache_media_from_message(m):
                    media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        input_documents = [InputMediaDocument(media=item.media) for item in photos_videos]
        for batch in batched(input_documents, 10):
            if batch[-1] == input_documents[-1]:
                batch[0].caption = caption

            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            # noinspection PyDefaultArgument
            await _send_with_rate_limit(lambda b=list(batch): msg.reply_media_group(media=b))  # type: ignore
        return None

    return None if not_cache else media_list


async def _send_media(
    msg: Message, parse_result, processed_list: list[ProcessedMedia], caption: str
) -> CacheEntry | None:
    """构建、发送媒体，并返回缓存条目。
    返回 None 表示不缓存
    """
    media_refs: list[AnyMediaRef] = to_list(parse_result.media)
    photos_videos, animations = _build_input_media(media_refs, processed_list)
    all_count = len(photos_videos) + len(animations)
    logger.debug(f"媒体分类完成: animations={len(animations)}, photos_videos={len(photos_videos)}")

    if all_count == 1:
        logger.debug("单媒体模式发送")
        media_list = await _send_single(msg, photos_videos, animations, caption)
    else:
        logger.debug(f"多媒体模式发送: total={all_count}")
        media_list = await _send_multi(msg, photos_videos, animations, caption)

    if media_list is None:
        return None
    return _make_cache_entry(parse_result, media_list)


# ── 缓存发送 ─────────────────────────────────────────────────────────


async def _send_cached(msg: Message, entry: CacheEntry, url: str):
    """从 file_id 缓存直接发送，跳过解析/下载/转码"""
    logger.debug(f"缓存发送: media={entry.media}")
    caption = build_caption_by_str(entry.parse_result.title, entry.parse_result.content, url, entry.telegraph_url)

    # 富文本类型
    if entry.telegraph_url:
        await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(show_above_text=True),
        )
        return

    if not entry.media:
        await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if len(entry.media) == 1:
        await _send_cached_single(msg, entry.media[0], caption)
    else:
        await _send_cached_multi(msg, entry.media, caption)


async def _send_cached_single(msg: Message, m: CacheMedia, caption: str) -> None:
    """从缓存发送单个媒体。"""
    match m.type:
        case CacheMediaType.PHOTO:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            await _send_with_rate_limit(lambda: msg.reply_photo(m.file_id, caption=caption))
        case CacheMediaType.VIDEO:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)
            await _send_with_rate_limit(
                lambda: msg.reply_video(
                    m.file_id, caption=caption, supports_streaming=True, video_cover=m.cover_file_id
                )
            )
        case CacheMediaType.ANIMATION:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            await _send_with_rate_limit(lambda: msg.reply_animation(m.file_id, caption=caption))
        case CacheMediaType.DOCUMENT:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            await _send_with_rate_limit(lambda: msg.reply_document(m.file_id, caption=caption, force_document=True))


async def _send_cached_multi(msg: Message, media: list[CacheMedia], caption: str) -> None:
    """从缓存发送多个媒体。"""
    animations = [m for m in media if m.type == CacheMediaType.ANIMATION]
    others = [m for m in media if m.type != CacheMediaType.ANIMATION]

    for ani in animations:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        await _send_with_rate_limit(
            lambda a=ani: msg.reply_animation(
                a.file_id,
                caption=caption if a == animations[-1] and not others else "",
            )
        )

    media_group = _build_cached_media_group(others)
    for batch in batched(media_group, 10):
        if batch[-1] == media_group[-1]:
            batch[0].caption = caption

        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        # noinspection PyDefaultArgument
        await _send_with_rate_limit(lambda m=list(batch): msg.reply_media_group(m))


def _build_cached_media_group(
    media: list[CacheMedia],
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaDocument]:
    """从 CacheMedia 列表构建 Telegram media group。"""
    group: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
    for m in media:
        match m.type:
            case CacheMediaType.PHOTO:
                group.append(InputMediaPhoto(media=m.file_id))
            case CacheMediaType.VIDEO:
                group.append(InputMediaVideo(media=m.file_id, supports_streaming=True, video_cover=m.cover_file_id))
            case CacheMediaType.DOCUMENT:
                group.append(InputMediaDocument(media=m.file_id))
    return group

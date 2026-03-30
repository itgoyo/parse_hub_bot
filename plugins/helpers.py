"""plugins 共用的工具函数和数据类"""

from dataclasses import dataclass
from pathlib import Path

from markdown import markdown
from parsehub import ParseHub, Platform
from parsehub.types import AnyMediaFile, AnyParseResult, RichTextParseResult
from parsehub.utils.media_info import MediaInfoReader
from pyrogram import Client

from log import logger
from utils.converter import clean_article_html
from utils.helpers import to_list
from utils.media_processing_unit import MediaProcessingUnit
from utils.ph import Telegraph

logger = logger.bind(name="Helpers")


@dataclass
class ProcessedMedia:
    source: AnyMediaFile
    output_paths: list[Path] | None = None
    output_dir: Path | None = None


def resolve_media_info(processed: "ProcessedMedia", file_path: str) -> tuple[int, int, int]:
    """获取媒体的宽、高、时长。若经过转码则从文件读取，否则使用源信息。"""
    if processed.output_paths:
        info = MediaInfoReader.read(file_path)
        return info.width, info.height, info.duration
    return processed.source.width, processed.source.height, getattr(processed.source, "duration", 0)


def build_caption(parse_result: AnyParseResult, telegraph_url: str | None = None):
    return build_caption_by_str(parse_result.title, parse_result.content, parse_result.raw_url, telegraph_url)


def build_caption_by_str(title: str | None, content: str | None, raw_url: str, telegraph_url: str | None = None) -> str:
    """构建消息正文：标题 + 内容 + 来源链接"""
    title, content = title or "", content or ""

    if telegraph_url:
        label = (title or content[:15]).replace("\n", " ") or "无标题"
        body = f"**[{label}]({telegraph_url})**"
    else:
        parts = []
        if title:
            parts.append(f"**{title}**")
        if content:
            parts.append(content)
        body = format_text("\n\n".join(parts) or "**无标题**")

    return f"{body}\n\n<b>▎<a href='{raw_url}'>Source</a></b>"


def format_text(text: str) -> str:
    """格式化输出内容, 限制长度, 添加折叠块样式"""
    text = text.strip()
    if len(text) > 500 or len(text.splitlines()) > 10:
        if len(text) > 1000:
            text = text[:900] + "......"
        return f"<blockquote expandable>{text}</blockquote>"
    else:
        return text


def progress(current: int, total: int, unit: str):
    text = f"下 载 中... | {f'{current * 100 / total:.0f}%' if unit == 'bytes' else f'{current}/{total}'}"
    if unit == "bytes":
        if round(current * 100 / total, 1) % 25 == 0:
            return text
    else:
        if (current + 1) % 3 == 0 or (current + 1) == total:
            return text
    return None


async def create_telegraph_page(html_content: str, cli: Client, parse_result: AnyParseResult) -> str:
    """创建 Telegraph 页面，返回页面 URL"""
    logger.debug(f"创建 Telegraph 页面: title={parse_result.title}")
    me = await cli.get_me()
    page = await Telegraph().create_page(
        parse_result.title or "无标题",
        html_content=html_content,
        author_name=me.full_name,
        author_url=parse_result.raw_url,
    )
    logger.debug(f"Telegraph 页面已创建: {page.url}")
    return page.url


async def create_richtext_telegraph(cli: Client, parse_result: RichTextParseResult) -> str:
    """将富文本解析结果转换为 Telegraph 页面，返回页面 URL"""
    logger.debug(f"富文本转 Telegraph: platform={parse_result.platform}, md_len={len(parse_result.markdown_content)}")
    md = parse_result.markdown_content
    match parse_result.platform:
        case Platform.WEIXIN:
            md = md.replace("mmbiz.qpic.cn", "qpic.cn.in/mmbiz.qpic.cn")
        case Platform.COOLAPK:
            md = md.replace("image.coolapk.com", "qpic.cn.in/image.coolapk.com")
    html = clean_article_html(markdown(md))
    return await create_telegraph_page(html, cli, parse_result)


async def process_media_files(download_result) -> list[ProcessedMedia]:
    """对下载结果中的媒体文件进行格式转换，返回 ProcessedMedia 列表"""
    processed_dir = download_result.output_dir.joinpath("processed")
    processor = MediaProcessingUnit(processed_dir, segment_height=1920, logger=logger.bind(name="MediaProcessor").debug)
    media_files = to_list(download_result.media)
    logger.debug(f"开始媒体格式转换: 文件数={len(media_files)}, output_dir={processed_dir}")
    processed_list: list[ProcessedMedia] = []
    for media_file in media_files:
        # 对于实况图片只处理图片, 不处理视频
        logger.debug(f"处理文件: {media_file.path}")
        result = await processor.process(media_file.path)
        logger.debug(f"处理结果: output_paths={result.output_paths}")
        processed_list.append(ProcessedMedia(media_file, result.output_paths, result.temp_dir))
    logger.debug(f"媒体格式转换完成: 处理数={len(processed_list)}")
    return processed_list


_PLATFORM_NAME_MAP: dict[str, str] = {
    "twitter": "Twitter / X",
    "douyin": "抖音 / TikTok",
    "youtube": "YouTube",
}

_PLATFORM_EXTRA_TYPES: dict[str, list[str]] = {
    "twitter": ["文章"],
}

_SPECIAL_TYPE_EMOJI: dict[str, str] = {
    "动态": "📝",
    "文章": "📝",
    "音乐": "🎵",
}

_PLATFORM_ORDER: list[str] = [
    "twitter", "instagram", "youtube", "facebook", "threads",
    "bilibili", "douyin", "weibo", "xhs", "tieba",
    "weixin", "kuaishou", "coolapk", "pipix", "zuiyou", "xiaoheihe",
]


def _format_platform_line(name: str, supported_types: list[str]) -> str:
    tags: list[str] = []
    for t in supported_types:
        if t in ("视频", "图文"):
            tags.append(f"✅{t}")
        else:
            emoji = _SPECIAL_TYPE_EMOJI.get(t, "📌")
            tags.append(f"{emoji} {t}")
    return f"**{name}**  {'  '.join(tags)}"


def get_supported_platforms() -> str:
    platform_data: dict[str, dict] = {}
    for p in ParseHub().get_platforms():
        platform_data[p["id"]] = p

    result_lines: list[str] = []
    ordered_ids = list(_PLATFORM_ORDER)
    for pid in platform_data:
        if pid not in ordered_ids:
            ordered_ids.append(pid)

    for pid in ordered_ids:
        if pid not in platform_data:
            continue
        p = platform_data[pid]
        name = _PLATFORM_NAME_MAP.get(pid, p["name"])
        types = list(p["supported_types"])
        for extra in _PLATFORM_EXTRA_TYPES.get(pid, []):
            if extra not in types:
                types.append(extra)
        result_lines.append(_format_platform_line(name, types))

    return "\n".join(result_lines)


def build_start_text() -> str:
    return (
        "**发送分享链接以进行解析**\n\n"
        "**支持的平台:**\n"
        f"<blockquote>{get_supported_platforms()}</blockquote>\n\n"
        "**命令列表:**\n"
        "`/jx <链接>` - 解析并发送媒体\n"
        "`/raw <链接>` - 不处理媒体, 发送原始文件\n"
        "`/zip <链接>` - 不处理媒体, 保存解析结果, 发送压缩包\n\n"
    )

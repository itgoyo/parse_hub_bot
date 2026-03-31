import aiomysql
from datetime import date, datetime

from core import bs
from log import logger

logger = logger.bind(name="DB")

_pool: aiomysql.Pool | None = None

_INIT_SQL = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        nickname VARCHAR(255) NOT NULL DEFAULT '',
        first_seen DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS parse_logs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        url VARCHAR(2048) NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_user_date (user_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS ad_clicks (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        ad_label VARCHAR(255) NOT NULL DEFAULT '',
        clicked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_user_date (user_id, clicked_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


async def init_db():
    global _pool
    _pool = await aiomysql.create_pool(
        host=bs.mysql_host,
        port=bs.mysql_port,
        user=bs.mysql_user,
        password=bs.mysql_password,
        db=bs.mysql_db,
        autocommit=True,
        charset="utf8mb4",
        minsize=1,
        maxsize=10,
    )
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            for sql in _INIT_SQL:
                await cur.execute(sql)
    logger.info("MySQL 数据库初始化完成")


async def close_db():
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        logger.info("MySQL 连接池已关闭")


def _get_pool() -> aiomysql.Pool:
    if _pool is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _pool


# ── 用户 ──────────────────────────────────────────────────────────────


async def upsert_user(user_id: int, nickname: str):
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, nickname)
                VALUES (%s, %s) AS new_val
                ON DUPLICATE KEY UPDATE nickname = new_val.nickname
                """,
                (user_id, nickname),
            )


# ── 解析记录 ──────────────────────────────────────────────────────────


async def log_parse(user_id: int, url: str):
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO parse_logs (user_id, url) VALUES (%s, %s)",
                (user_id, url),
            )


async def get_today_parse_count(user_id: int) -> int:
    pool = _get_pool()
    today = date.today()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM parse_logs WHERE user_id = %s AND DATE(created_at) = %s",
                (user_id, today),
            )
            row = await cur.fetchone()
            return row[0] if row else 0


# ── 广告点击 ──────────────────────────────────────────────────────────


async def log_ad_click(user_id: int, ad_label: str = ""):
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO ad_clicks (user_id, ad_label) VALUES (%s, %s)",
                (user_id, ad_label),
            )


async def get_today_ad_clicks(user_id: int) -> int:
    pool = _get_pool()
    today = date.today()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM ad_clicks WHERE user_id = %s AND DATE(clicked_at) = %s",
                (user_id, today),
            )
            row = await cur.fetchone()
            return row[0] if row else 0


# ── 配额 ──────────────────────────────────────────────────────────────


async def get_remaining_quota(user_id: int) -> int:
    """获取用户今日剩余解析次数"""
    used = await get_today_parse_count(user_id)
    ad_clicks = await get_today_ad_clicks(user_id)
    total = bs.daily_free_quota + ad_clicks * bs.ad_bonus_quota
    return max(0, total - used)


async def check_and_consume_quota(user_id: int, url: str) -> tuple[bool, int]:
    """原子检查并消耗一次配额。返回 (是否允许, 剩余次数)"""
    pool = _get_pool()
    today = date.today()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 在同一个连接中原子执行：查 used、查 ad_clicks、判断、插入
            await cur.execute(
                "SELECT COUNT(*) FROM parse_logs WHERE user_id = %s AND DATE(created_at) = %s",
                (user_id, today),
            )
            used = (await cur.fetchone())[0]

            await cur.execute(
                "SELECT COUNT(*) FROM ad_clicks WHERE user_id = %s AND DATE(clicked_at) = %s",
                (user_id, today),
            )
            ad_clicks = (await cur.fetchone())[0]

            total = bs.daily_free_quota + ad_clicks * bs.ad_bonus_quota
            remaining = total - used

            if remaining <= 0:
                return False, 0

            return True, remaining

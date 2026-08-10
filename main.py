"""
Main entry point for Telegram Channel Analyzer.
Telethon (user account) collector + PostgreSQL + Gemini + FastAPI Web UI.
"""
import asyncio
import os
import sys
import logging
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv

from db import Database
from gemini_service import GeminiService
from collector import TelegramCollector
from scheduler import BotScheduler
from utils import setup_logging
from web import app as web_app, startup as web_startup, shutdown as web_shutdown

logger = logging.getLogger(__name__)

# Global state
collector = None
db = None
gemini = None


async def _sync_today_comments(collector: "TelegramCollector", db: "Database"):
    """补采今日帖子的评论。"""
    today = datetime.now(timezone.utc).date()
    for chat_id in list(collector.watched_chat_ids):
        posts = await db.get_posts_for_date(chat_id, today)
        if not posts:
            continue
        entity = await collector.client.get_entity(chat_id)
        post_ids = [(entity, p["message_id"], p["id"]) for p in posts]
        logger.info(f"🔄 Syncing comments for {len(post_ids)} posts in chat {chat_id}...")
        total = await collector.sync_comments_for_posts(post_ids)
        logger.info(f"   Synced {total} comments")


async def run_collector():
    """后台运行采集器和调度器。"""
    global collector, db, gemini

    # 校验环境变量
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")
    gemini_api_key = os.getenv("OPENAI_API_KEY")
    database_url = os.getenv("DATABASE_URL")
    watch = os.getenv("WATCH_CHANNELS", "")
    summary_chat_id = os.getenv("SUMMARY_CHAT_ID", "me")
    report_time = os.getenv("REPORT_TIME", "23:59")

    missing = []
    if not api_id: missing.append("TELEGRAM_API_ID")
    if not api_hash: missing.append("TELEGRAM_API_HASH")
    if not phone: missing.append("TELEGRAM_PHONE")
    if not gemini_api_key: missing.append("OPENAI_API_KEY")
    if not database_url: missing.append("DATABASE_URL")
    if not watch: missing.append("WATCH_CHANNELS")
    if missing:
        logger.error(f"❌ Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    # 数据库
    logger.info("📦 Connecting to PostgreSQL...")
    db = Database(database_url)
    await db.connect()

    # Gemini
    logger.info("🤖 Initializing Gemini AI service...")
    gemini = GeminiService(gemini_api_key)

    # 设置 web app 的全局 db/collector/gemini
    import web
    web.db = db
    web.collector = None  # Will be set after collector starts
    web.gemini = gemini

    # Telethon 采集器
    logger.info("🤖 Initializing Telethon collector...")
    collector = TelegramCollector(int(api_id), api_hash, phone, db, watch.split(","))
    await collector.start()
    web.collector = collector

    # 启动时补采
    logger.info("🔄 Syncing recent posts...")
    await collector.sync_recent(limit=500)
    await _sync_today_comments(collector, db)

    # 调度器
    logger.info(f"⏰ Daily teacher report at {report_time}")
    scheduler = BotScheduler(collector, db, gemini, summary_chat_id)
    scheduler.start(report_time=report_time)

    logger.info("✅ Collector is running...")

    # 保持运行
    while True:
        await asyncio.sleep(3600)


async def main():
    """启动 FastAPI Web + 后台采集器。"""
    load_dotenv()
    setup_logging()

    logger.info("=" * 60)
    logger.info("🚀 Telegram Channel Analyzer Starting...")
    logger.info("=" * 60)

    # 启动后台采集器
    asyncio.create_task(run_collector())

    # 启动 FastAPI
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

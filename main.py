"""
Main entry point for Telegram Channel Analyzer.
Telethon (user account) collector + PostgreSQL + Gemini + 定时报告（无 Web 页面）。
"""
import asyncio
import os
import sys
import logging

from dotenv import load_dotenv

from db import Database
from gemini_service import GeminiService
from collector import TelegramCollector
from scheduler import BotScheduler
from utils import setup_logging, sync_today_comments

logger = logging.getLogger(__name__)


async def run_collector():
    """后台运行采集器和调度器。"""
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

    # Telethon 采集器
    logger.info("🤖 Initializing Telethon collector...")
    collector = TelegramCollector(int(api_id), api_hash, phone, db, watch.split(","))
    await collector.start()

    # 启动时补采
    logger.info("🔄 Syncing recent posts...")
    await collector.sync_recent(limit=500)
    await sync_today_comments(collector, db)

    # 调度器（高分榜单 09:00 + 每日精华 23:59 + 清理 00:30）
    logger.info(f"⏰ Daily teacher report at {report_time}")
    scheduler = BotScheduler(collector, db, gemini, summary_chat_id)
    scheduler.start(report_time=report_time)

    logger.info("✅ Collector is running...")

    # 保持运行
    while True:
        await asyncio.sleep(3600)


async def main():
    load_dotenv()
    setup_logging()

    logger.info("=" * 60)
    logger.info("🚀 Telegram Channel Analyzer Starting...")
    logger.info("=" * 60)

    # 启动后台采集器 + 定时任务（无 Web 页面）
    await run_collector()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

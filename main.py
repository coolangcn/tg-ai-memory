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

    # 全量补帖子（only_with_score 只保留含综合评分的老师帖，
    # 确保 DB 帖子覆盖端上全部 5262 条，评论才能全部挂载）
    logger.info("🔄 Full syncing posts...")
    await collector.full_sync_posts(only_with_score=True)
    await sync_today_comments(collector, db)

    # 启动时补采讨论组报告（覆盖离线期间遗漏的评价）
    logger.info("🔄 Syncing discussion reports...")
    await collector.sync_discussion_reports(limit=500)

    # 启动时全量补采评论（v2：GetDiscussionMessageRequest 定位镜像帖后抓取，幂等纠偏关联）
    logger.info("🔄 Syncing all discussion comments (v2)...")
    await collector.sync_all_comments()

    # 调度器（高分榜单 09:00 + 每日精华 23:59 + 清理 00:30）
    logger.info(f"⏰ Daily teacher report at {report_time}")
    scheduler = BotScheduler(collector, db, gemini, summary_chat_id)
    scheduler.start(report_time=report_time)

    logger.info("✅ Collector is running...")

    # 保持运行 + Telegram 断线自动重连看门狗
    # Telethon 断线后不会自动恢复，需定期检查并重连，否则定时任务会报
    # "Cannot send requests while disconnected"。
    logger.info("🛡️ Connection watchdog started (check every 60s)")
    while True:
        await asyncio.sleep(60)
        try:
            if collector.client.is_connected():
                continue
            logger.warning("⚠️ Telegram 连接断开，尝试重连...")
            await collector.client.connect()
            if not collector.client.is_connected():
                logger.error("❌ Telegram 重连失败，60s 后重试")
                continue
            authorized = await collector.client.is_user_authorized()
            if authorized:
                logger.info("✅ Telegram 重连成功")
            else:
                logger.error("❌ 已重连但会话失效，请重新运行 login.py")
        except Exception as e:
            logger.error(f"❌ 连接检查/重连异常: {e}")


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

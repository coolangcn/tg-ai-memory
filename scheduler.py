"""
调度器：高分榜单报告 + 每日精华总结 + 数据库清理。
"""
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from collector import TelegramCollector
from db import Database
from gemini_service import GeminiService
from utils import chunk_text, sync_today_comments

logger = logging.getLogger(__name__)


class BotScheduler:
    def __init__(
        self,
        collector: TelegramCollector,
        database: Database,
        gemini: GeminiService,
        summary_chat_id,
    ):
        self.scheduler = AsyncIOScheduler()
        self.collector = collector
        self.db = database
        self.gemini = gemini
        self.summary_chat_id = summary_chat_id

    async def cleanup_task(self):
        """定时清理 14 天前的旧消息。"""
        logger.info("⏰ Running scheduled cleanup task...")
        try:
            deleted_count = await self.db.cleanup_old_messages()
            logger.info(f"✅ Cleanup completed: {deleted_count} messages deleted")
        except Exception as e:
            logger.error(f"❌ Cleanup task failed: {e}")

    async def daily_ranking_task(self):
        """定时生成并发送高分老师榜单报告（图文并茂）。"""
        logger.info("⏰ Running scheduled ranking report task...")
        try:
            # 先补采评论（报告），确保榜单数据是最新的
            await sync_today_comments(self.collector, self.db)

            from send_report import generate_and_send
            report = await generate_and_send(self.db, self.collector, self.gemini)
            if report:
                logger.info("✅ Ranking report sent")
            else:
                logger.info("No eligible teachers, ranking report skipped")
        except Exception as e:
            logger.error(f"❌ Ranking report task failed: {e}")

    async def daily_summary_task(self):
        """生成并发送今日老师分析与评分报告。"""
        logger.info("⏰ Running scheduled daily teacher report task...")
        try:
            # 1. 先补采，防止离线期间漏消息
            await self.collector.sync_recent(limit=500)

            # 1.5 补采今日帖子的评论
            await sync_today_comments(self.collector, self.db)

            # 2. 汇总今日所有目标来源的帖子+评论
            today = datetime.now().date()
            all_posts_with_comments = []
            for chat_id in list(self.collector.watched_chat_ids):
                items = await self.db.get_posts_with_comments(chat_id, today)
                all_posts_with_comments.extend(items)

            if not all_posts_with_comments:
                logger.info("No posts today, sending empty digest")
                await self.collector.send_message(
                    self.summary_chat_id,
                    f"📝 每日老师分析 ({today})\n\n今日暂无新帖子。"
                )
                return

            total_comments = sum(len(item["comments"]) for item in all_posts_with_comments)
            logger.info(f"Generating teacher report for {len(all_posts_with_comments)} teachers, {total_comments} comments")

            # 3. 调用 Gemini 生成老师分析报告
            source_names = ", ".join(self.collector.source_names)
            summary = await self.gemini.generate_teacher_report(
                all_posts_with_comments,
                date_str=str(today),
                source_names=source_names,
            )

            # 4. 分块发送
            chunks = chunk_text(summary)
            for i, chunk in enumerate(chunks):
                text = f"📝 每日老师分析 ({today})\n\n{chunk}" if i == 0 else chunk
                await self.collector.send_message(self.summary_chat_id, text)

            logger.info("✅ Daily teacher report sent")
        except Exception as e:
            logger.error(f"❌ Daily digest task failed: {e}")

    def start(self, report_time: str = "23:59"):
        """启动调度器。report_time 按服务器本地时区。"""
        try:
            hour, minute = map(int, report_time.split(":"))
        except ValueError:
            logger.warning(f"Invalid REPORT_TIME '{report_time}', defaulting to 23:59")
            hour, minute = 23, 59

        self.scheduler.add_job(
            self.daily_summary_task,
            CronTrigger(hour=hour, minute=minute),
            id='daily_summary',
            name='Daily Digest',
            replace_existing=True
        )
        logger.info(f"📅 Scheduled: Daily digest at {hour:02d}:{minute:02d} (server local time)")

        # 高分榜单报告（每天 09:00，与每日精华错开）
        self.scheduler.add_job(
            self.daily_ranking_task,
            CronTrigger(hour=9, minute=0),
            id='daily_ranking',
            name='Daily Ranking Report',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Ranking report at 09:00 (server local time)")

        self.scheduler.add_job(
            self.cleanup_task,
            CronTrigger(hour=0, minute=30),
            id='daily_cleanup',
            name='Database Cleanup',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Database cleanup at 00:30")

        self.scheduler.start()
        logger.info("✅ Scheduler started successfully")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")

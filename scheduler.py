"""
调度器：高分榜单报告 + 每日精华总结 + 全量评论补抓。
（已移除数据库清理任务：数据只增不减，保留全部历史帖子与评论。）
"""
import asyncio
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

    async def discussion_report_sync_task(self):
        """定时增量拉取讨论组报告模板，按老师名关联到频道帖子入库。"""
        logger.info("⏰ Running discussion report sync task...")
        try:
            matched = await self.collector.sync_discussion_reports(limit=500)
            logger.info(f"✅ Discussion report sync completed: {matched} 条报告评论关联入库")
        except Exception as e:
            logger.error(f"❌ Discussion report sync task failed: {e}")

    async def daily_ranking_task(self):
        """定时生成并发送高分老师榜单报告（图文并茂）。"""
        logger.info("⏰ Running scheduled ranking report task...")
        try:
            # 先补采评论（报告），确保榜单数据是最新的
            await sync_today_comments(self.collector, self.db)

            # 补采讨论组报告模板（按老师名关联到帖子）
            await self.collector.sync_discussion_reports(limit=500)

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

            # 1.6 补采讨论组报告模板（按老师名关联到帖子）
            await self.collector.sync_discussion_reports(limit=500)

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

    async def daily_score_update_task(self):
        """每日更新所有帖子的评论计算评分。"""
        logger.info("⏰ Running daily score update task...")
        try:
            chat_ids = list(self.collector.watched_chat_ids)
            if not chat_ids:
                chat_ids = await self.db.get_distinct_chat_ids()
            
            for chat_id in chat_ids:
                stats = await self.db.update_all_comment_scores(chat_id)
                logger.info(
                    f"  ✅ chat_id={chat_id}: 更新 {stats['updated']} 个帖子的评论评分, "
                    f"无有效评论 {stats['no_comments']} 个"
                )
            
            logger.info("✅ Score update completed")
        except Exception as e:
            logger.error(f"❌ Score update task failed: {e}")

    async def daily_post_content_sync_task(self):
        """每日同步帖子内容：从频道拉取最新帖子文本，检测编辑（如评分变更）。"""
        logger.info("⏰ Running daily post content sync task...")
        try:
            chat_ids = list(self.collector.watched_chat_ids)
            if not chat_ids:
                chat_ids = await self.db.get_distinct_chat_ids()
            
            for chat_id in chat_ids:
                updated = await self.collector.sync_posts_content(chat_id)
                logger.info(f"  ✅ chat_id={chat_id}: 同步 {updated} 个帖子内容")
            
            logger.info("✅ Post content sync completed")
        except Exception as e:
            logger.error(f"❌ Post content sync task failed: {e}")

    async def daily_full_post_sync_task(self):
        """每日全量帖子同步：拉取频道全部历史帖子入库，防止漏采。
        使用 iter_messages 全量遍历 + 数据库唯一索引自动去重，幂等可重复执行。
        """
        logger.info("⏰ Running daily full post sync task...")
        try:
            # 全量同步所有监控频道（只保留含'综合评分'的老师帖，过滤广告/通知）
            added = await self.collector.full_sync_posts(only_with_score=True)
            logger.info(f"✅ Full post sync completed: 新增 {added} 条帖子")
        except Exception as e:
            logger.error(f"❌ Full post sync task failed: {e}")

    async def daily_comment_backfill_task(self):
        """每日全量评论补抓（v2）：对每个帖子调用 GetDiscussionMessageRequest
        定位讨论群「镜像帖」，再用 iter_messages(reply_to=镜像帖ID) 抓取评论，
        以帖子 DB id 关联入库。
        幂等：评论按 (chat_id, message_id) 唯一去重，DO UPDATE 纠正父帖关联。
        只处理最近 200 个帖子（每日增量），历史全量由启动时完成。
        """
        logger.info("⏰ Running daily comment backfill task (v2, recent 200 posts)...")
        try:
            matched = await self.collector.sync_all_comments(limit=200)
            logger.info(f"✅ Comment backfill completed: {matched} 条评论入库/更新")
        except Exception as e:
            logger.error(f"❌ Daily comment backfill task failed: {e}")

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

        # 讨论组报告同步（每小时一次，确保榜单/每日总结前数据最新）
        self.scheduler.add_job(
            self.discussion_report_sync_task,
            CronTrigger(minute=10),
            id='discussion_report_sync',
            name='Discussion Report Sync',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Discussion report sync every hour")

        # 全量帖子同步（每天 01:30，在评论补抓之前）
        self.scheduler.add_job(
            self.daily_full_post_sync_task,
            CronTrigger(hour=1, minute=30),
            id='daily_full_post_sync',
            name='Daily Full Post Sync',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Full post sync at 01:30")

        # 全量评论补抓（每天 02:00，在帖子同步之后）
        self.scheduler.add_job(
            self.daily_comment_backfill_task,
            CronTrigger(hour=2, minute=0),
            id='daily_comment_backfill',
            name='Daily Comment Backfill',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Comment backfill at 02:00")

        # 评论评分更新（每天 02:30，在评论补抓之后）
        self.scheduler.add_job(
            self.daily_score_update_task,
            CronTrigger(hour=2, minute=30),
            id='daily_score_update',
            name='Daily Score Update',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Score update at 02:30")

        # 帖子内容同步（每天 03:00，检测帖子编辑/评分变更）
        self.scheduler.add_job(
            self.daily_post_content_sync_task,
            CronTrigger(hour=3, minute=0),
            id='daily_post_sync',
            name='Daily Post Content Sync',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Post content sync at 03:00")

        self.scheduler.start()
        logger.info("✅ Scheduler started successfully")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")

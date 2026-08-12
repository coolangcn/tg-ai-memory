"""
调度器：高分榜单报告 + 每日精华总结 + 数据库清理 + 全量评论补抓。
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

    async def daily_comment_backfill_task(self):
        """每日全量评论补抓：对所有帖子拉取评论，补全缺失部分。
        
        策略：遍历所有帖子，逐个拉取全部评论。
        已存在的评论会因唯一索引冲突自动跳过（ON CONFLICT DO NOTHING），
        确保不重复入库，同时补全缺失的评论。
        """
        from telethon.errors import MsgIdInvalidError, FloodWaitError
        from datetime import timezone
        
        logger.info("⏰ Running daily comment backfill task (full reconciliation)...")
        try:
            total_new_comments = 0
            total_skipped = 0  # 已存在被跳过的评论数
            total_posts_processed = 0
            total_posts_with_new = 0
            total_errors = 0
            
            # 失败原因分类统计
            failure_reasons = {
                'msg_id_invalid': 0,   # 消息已删除/不存在
                'flood_wait': 0,       # 请求过于频繁
                'forbidden': 0,        # 无权限访问
                'timeout': 0,          # 超时
                'other': 0,            # 其他错误
            }
            
            # 获取频道 ID 列表（优先 watched_chat_ids，否则从数据库获取）
            chat_ids = list(self.collector.watched_chat_ids)
            if not chat_ids:
                # 从数据库获取有帖子的频道
                chat_ids = await self.db.get_distinct_chat_ids()
                logger.info(f"  📋 从数据库获取到 {len(chat_ids)} 个有帖子的频道")
            
            if not chat_ids:
                logger.info("⚠️ 无可用频道，跳过评论补抓")
                return
            
            for chat_id in chat_ids:
                # 获取该频道所有帖子
                all_posts = await self.db.get_all_posts(chat_id)
                if not all_posts:
                    logger.info(f"  ✅ chat_id={chat_id}: 无帖子，跳过")
                    continue
                
                logger.info(f"  📥 chat_id={chat_id}: {len(all_posts)} 个帖子，开始全量对账...")
                
                # 获取频道实体
                try:
                    entity = await self.collector.client.get_entity(chat_id)
                except Exception as e:
                    logger.error(f"  ❌ 无法获取频道实体 {chat_id}: {e}")
                    continue
                
                for post in all_posts:
                    post_db_id = post['id']
                    post_msg_id = post['message_id']
                    
                    if not post_msg_id:
                        continue
                    
                    try:
                        # 拉取该帖子的全部评论，逐个插入（重复自动跳过）
                        new_count = 0
                        skip_count = 0
                        async for reply in self.collector.client.iter_messages(entity, reply_to=post_msg_id):
                            reply_text = reply.text or ""
                            if not reply_text:
                                continue
                            
                            # 评论者信息
                            reply_user_name = None
                            reply_user_id = None
                            try:
                                sender = await reply.get_sender()
                                if sender is not None:
                                    reply_user_id = str(sender.id)
                                    from telethon.tl.types import User
                                    if isinstance(sender, User):
                                        reply_user_name = sender.username or sender.first_name or f"User{sender.id}"
                            except Exception:
                                pass
                            
                            reply_created = reply.date
                            if reply_created and reply_created.tzinfo is None:
                                reply_created = reply_created.replace(tzinfo=timezone.utc)
                            
                            # 插入评论（已存在则跳过）
                            result = await self.db.insert_message(
                                chat_id=chat_id,
                                message_id=reply.id,
                                user_id=reply_user_id,
                                user_name=reply_user_name,
                                message_text=reply_text,
                                created_at=reply_created,
                                parent_message_id=post_db_id,
                                message_type="comment",
                            )
                            if result:
                                new_count += 1
                            else:
                                skip_count += 1
                        
                        total_posts_processed += 1
                        total_new_comments += new_count
                        total_skipped += skip_count
                        
                        if new_count > 0:
                            total_posts_with_new += 1
                        
                        if total_posts_processed % 20 == 0:
                            logger.info(
                                f"  📊 已处理 {total_posts_processed}/{len(all_posts)} 个帖子, "
                                f"新增 {total_new_comments} 条, 跳过 {total_skipped} 条"
                            )
                        
                        await asyncio.sleep(0.3)
                        
                    except MsgIdInvalidError:
                        # 帖子已被删除，正常跳过，不中断流程
                        total_posts_processed += 1
                        failure_reasons['msg_id_invalid'] += 1
                        logger.debug(f"  ⚠️ 帖子 {post_msg_id}: 已删除，自动跳过")
                        continue  # 明确跳过，继续下一个
                    except FloodWaitError as e:
                        # Telegram 限流，等待后继续处理当前帖子
                        failure_reasons['flood_wait'] += 1
                        logger.warning(f"  ⏳ FloodWait: {e.seconds}秒，等待后继续")
                        await asyncio.sleep(e.seconds)
                        # 重试当前帖子
                        try:
                            async for reply in self.collector.client.iter_messages(entity, reply_to=post_msg_id):
                                reply_text = reply.text or ""
                                if not reply_text:
                                    continue
                                # ... 重新处理（简化：直接跳过，下次定时任务会补）
                        except Exception:
                            pass
                        continue
                    except Exception as e:
                        total_errors += 1
                        total_posts_processed += 1
                        # 分类错误原因
                        err_str = str(e).lower()
                        if 'forbidden' in err_str or 'not authorized' in err_str or 'privacy' in err_str:
                            failure_reasons['forbidden'] += 1
                            logger.debug(f"  ⚠️ 帖子 {post_msg_id}: 无权限，跳过")
                        elif 'timeout' in err_str or 'timed out' in err_str:
                            failure_reasons['timeout'] += 1
                            logger.debug(f"  ⚠️ 帖子 {post_msg_id}: 超时，跳过")
                        else:
                            failure_reasons['other'] += 1
                            if total_errors <= 10:
                                logger.warning(f"  ⚠️ 帖子 {post_msg_id}: {str(e)[:80]}")
                        await asyncio.sleep(1)
                        continue  # 明确跳过
                
                logger.info(f"  ✅ chat_id={chat_id}: 处理完成")
            
            logger.info(
                f"✅ Comment backfill completed:\n"
                f"  📊 处理 {total_posts_processed} 个帖子, "
                f"有新增的帖子 {total_posts_with_new} 个\n"
                f"  💬 新增评论 {total_new_comments} 条, "
                f"跳过已有 {total_skipped} 条\n"
                f"  ❌ 失败 {total_errors} 条:\n"
                f"     - 消息已删除/不存在: {failure_reasons['msg_id_invalid']}\n"
                f"     - 请求频繁(FloodWait): {failure_reasons['flood_wait']}\n"
                f"     - 无权限访问: {failure_reasons['forbidden']}\n"
                f"     - 超时: {failure_reasons['timeout']}\n"
                f"     - 其他错误: {failure_reasons['other']}"
            )
            
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

        self.scheduler.add_job(
            self.cleanup_task,
            CronTrigger(hour=0, minute=30),
            id='daily_cleanup',
            name='Database Cleanup',
            replace_existing=True
        )
        logger.info("📅 Scheduled: Database cleanup at 00:30")

        # 全量评论补抓（每天 02:00，在清理之后、榜单之前）
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

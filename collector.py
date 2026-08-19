"""
Telethon collector - 用用户账号采集 Telegram 频道/群组消息。

为什么用 Telethon（用户账号）而不是 Bot API：
- Bot API 只能看到"机器人是管理员/成员"的群和频道；
- 用户账号可以读取自己订阅的所有频道、加入的所有群组，
  即使没有管理权限（只有访问权限）也能采集。
"""
import asyncio
import logging
import os
import re
from datetime import timezone

from telethon import TelegramClient, events
from telethon.tl.types import Channel, User

from db import Database
from gemini_service import GeminiService

logger = logging.getLogger(__name__)

SESSION_NAME = "telegram"

# 讨论组 chat_id：频道帖子（👧#老师名）通过老师名关联讨论组里的「报告模板」评价
DISCUSSION_CHAT_ID = int(os.getenv("DISCUSSION_CHAT_ID", "-1003367541028"))


def extract_teacher_from_report(text: str):
    """从讨论组报告提取老师名字。"""
    if not text:
        return None
    m = re.search(r'老师[：:]\s*#?(\w+)', text)
    return m.group(1) if m else None


def extract_teacher_from_post(text: str):
    """从频道帖子提取老师名字。"""
    if not text:
        return None
    m = re.search(r'👧#(\w+)', text)
    return m.group(1) if m else None


def is_report(text: str) -> bool:
    """判断是否是报告模板消息。"""
    if not text:
        return False
    return ('报告模板' in text) or ('整场综合评分' in text) or ('老师：' in text or '老师:' in text)

# SPA 广告过滤关键词
SPA_KEYWORDS = [
    "养生SPA", "SPA", "会所", "本店", "到店", "进店", "门店",
    "开业大酬宾", "开业优惠", "充值", "会员卡", "套餐价",
    "地址：", "地址:", "导航", "停车场", "营业时间",
    "客服微信", "前台", "预约电话", "到店消费",
    "spa", "店铺", "商家", "促销", "打折", "优惠",
    "光临", "惠顾", "连锁", "品牌",
]


def is_spa_ad(text: str) -> bool:
    """判断是否是 SPA/店铺广告。"""
    if not text:
        return False
    count = sum(1 for k in SPA_KEYWORDS if k in text)
    return count >= 2


def _build_proxy():
    """从 TELEGRAM_PROXY 环境变量构建 Telethon 代理元组，如 socks5://127.0.0.1:7890"""
    import socks

    raw = os.getenv("TELEGRAM_PROXY", "").strip()
    if not raw:
        return None
    scheme, _, rest = raw.partition("://")
    host_port, _, auth = rest.partition("@")
    host, _, port = host_port.partition(":")
    port = int(port) if port else 0
    cls = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP}.get(scheme.lower())
    if cls is None:
        raise ValueError(f"不支持的代理类型: {scheme}")
    if auth:
        user, _, pwd = auth.partition(":")
        return (cls, host, port, True, user, pwd)
    return (cls, host, port)


class TelegramCollector:
    def __init__(self, api_id: int, api_hash: str, phone: str, database: Database, watch_list):
        self.client = TelegramClient(SESSION_NAME, api_id, api_hash, proxy=_build_proxy())
        self.phone = phone
        self.db = database
        self.watch_list = [w.strip() for w in watch_list if w.strip()]
        self.watched_entities = []
        self.watched_chat_ids = set()
        self.source_names = []
        self.discussion_chat_id = int(os.getenv("DISCUSSION_CHAT_ID", "-1003367541028"))
        self.discussion_entity = None

    async def start(self):
        """启动客户端、解析采集目标、注册实时监听。"""
        await self.client.start(phone=self.phone)
        me = await self.client.get_me()
        logger.info(f"✅ Logged in as {me.first_name} (@{me.username or me.id})")

        # 解析采集目标（@用户名 或 数字ID），失败说明账号无访问权限
        for name in self.watch_list:
            try:
                entity = await self.client.get_input_entity(name)
                self.watched_entities.append(entity)
                title = await self._entity_title(name)
                self.source_names.append(title or name)
                logger.info(f"✅ Watching: {name} ({title or 'unknown title'})")
            except Exception as e:
                logger.warning(f"⚠️ Cannot access {name}: {e} (是否已订阅/加入?)")

        if not self.watched_entities:
            raise RuntimeError("No watchable entities - check WATCH_CHANNELS and account access")

        # 注册实时消息监听（频道）
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=self.watched_entities)
        )
        logger.info("👂 Real-time listener registered")

        # 注册讨论组监听：报告模板消息按老师名关联到频道帖子，作为评论入库
        try:
            self.discussion_entity = await self.client.get_input_entity(self.discussion_chat_id)
            self.client.add_event_handler(
                self._on_new_message,
                events.NewMessage(chats=[self.discussion_entity])
            )
            logger.info(f"👂 Discussion group listener registered: {self.discussion_chat_id}")
        except Exception as e:
            logger.warning(f"⚠️ Cannot access discussion group {self.discussion_chat_id}: {e}")

    async def _entity_title(self, name: str) -> str:
        try:
            entity = await self.client.get_entity(name)
            return getattr(entity, "title", None) or getattr(entity, "first_name", None) or name
        except Exception:
            return name

    async def _on_new_message(self, event):
        """实时收到的新消息 -> 入库。讨论组报告按老师名关联到频道帖子。"""
        try:
            msg = event.message
            text = msg.text or ""
            if not text:
                return

            # 讨论组消息：报告模板 -> 关联频道帖子作为评论入库；其他消息忽略
            if msg.chat_id == self.discussion_chat_id:
                if not is_report(text):
                    return
                teacher = extract_teacher_from_report(text)
                if not teacher:
                    logger.debug(f"讨论组报告无老师名，跳过 msg={msg.id}")
                    return
                post_db_id = await self._find_post_db_id_by_teacher(teacher)
                if not post_db_id:
                    logger.debug(f"讨论组报告未匹配到频道帖子: 老师={teacher}, msg={msg.id}")
                    return
                await self._store(msg, text, message_type="comment", parent_message_id=post_db_id)
                logger.info(f"✅ 讨论组报告入库: 老师={teacher}, msg={msg.id}")
                return

            # 频道消息：判断是否是评论（回复某条帖子）
            parent_id = None
            msg_type = "post"
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                parent_id = msg.reply_to.reply_to_msg_id
                msg_type = "comment"
            await self._store(msg, text, message_type=msg_type, parent_message_id=parent_id)
        except Exception as e:
            logger.error(f"Error handling new message: {e}")

    async def _find_post_db_id_by_teacher(self, teacher: str):
        """按老师名查找最新频道帖子的 db id。"""
        if not teacher:
            return None
        async with self.db.pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT id FROM messages
                WHERE message_type = 'post' AND teacher_name = $1
                ORDER BY created_at DESC
                LIMIT 1
            """, teacher)

    async def sync_discussion_reports(self, limit: int = 500):
        """增量拉取讨论组最近的报告模板消息，按老师名关联到频道帖子入库。
        幂等：重复执行不会产生重复评论；已有关联会被纠正（DO UPDATE parent_message_id）。
        返回: 本次匹配入库的报告评论数。
        """
        if self.discussion_entity is None:
            try:
                self.discussion_entity = await self.client.get_entity(self.discussion_chat_id)
            except Exception as e:
                logger.error(f"❌ 无法访问讨论组 {self.discussion_chat_id}: {e}")
                return 0

        # 构建 老师名 -> 帖子 db id 映射（同名字保留最新帖）
        async with self.db.pool.acquire() as conn:
            post_rows = await conn.fetch("""
                SELECT id, teacher_name FROM messages
                WHERE message_type = 'post' AND teacher_name IS NOT NULL
                ORDER BY created_at DESC
            """)
        name_to_post = {}
        for r in post_rows:
            if r['teacher_name'] not in name_to_post:
                name_to_post[r['teacher_name']] = r['id']

        matched = 0
        scanned = 0
        async for m in self.client.iter_messages(self.discussion_entity, limit=limit):
            scanned += 1
            text = m.text or ""
            if not text or not is_report(text):
                continue
            teacher = extract_teacher_from_report(text)
            if not teacher:
                continue
            post_db_id = name_to_post.get(teacher)
            if not post_db_id:
                continue

            created_at = m.date
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            async with self.db.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO messages (chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'comment')
                    ON CONFLICT (chat_id, message_id) WHERE message_id IS NOT NULL
                    DO UPDATE SET parent_message_id = EXCLUDED.parent_message_id
                """, m.chat_id, m.id, "讨论组", str(m.sender_id), teacher, text, created_at, post_db_id)
            matched += 1

        logger.info(f"🔄 讨论组报告同步: 扫描 {scanned} 条消息, 关联入库 {matched} 条报告评论")
        return matched

    async def cleanup_all_comments(self):
        """清理 v1 污染数据：删除全部评论记录，由 v2 全量重建。
        旧版 sync_all_comments（v1）把讨论群 2084 条自发消息误当成评论入库，
        且把评论的父帖关联（parent_message_id）批量改错，
        无法用简单条件区分真假，因此整体清空评论表由 v2 重建最安全。
        返回: 删除条数。
        """
        async with self.db.pool.acquire() as conn:
            deleted = await conn.execute("DELETE FROM messages WHERE message_type = 'comment'")
        count = deleted.split()[-1]
        logger.info(f"🧹 评论表已清空: 删除 {count} 条（等待 v2 全量重建）")
        return int(count)

    async def sync_all_comments(self, limit: int = None):
        """全量补采评论（v2 正确版，核心修复）。

        关键认知：Telegram 频道帖子的评论实际存储在关联讨论群中，且讨论群
        「镜像帖」的 message_id 与频道帖子 ID 是两套独立编号体系，
        不能用讨论群消息的 reply_to 去频道反查帖子。

        正确抓取路径（已用帖子 17988 实测验证，35 条与端上完全一致）：
          1. GetDiscussionMessageRequest(peer=频道, msg_id=帖子ID) 取讨论消息；
          2. 在返回消息中找镜像帖：m.replies.replies > 0 且 date 最早的一条；
          3. iter_messages(讨论群, reply_to=镜像帖ID) 抓取该帖子全部评论；
          4. 评论以帖子 DB id 作为 parent_message_id 入库。

        幂等：评论按 (chat_id, message_id) 唯一去重，DO UPDATE 纠正父帖关联。
        limit: 只处理最近的 N 个帖子（None 表示全部）。
        返回: 本次入库/更新的评论数。
        """
        from telethon.tl.functions.messages import GetDiscussionMessageRequest
        from telethon.errors import FloodWaitError, MsgIdInvalidError

        if self.discussion_entity is None:
            try:
                self.discussion_entity = await self.client.get_entity(self.discussion_chat_id)
            except Exception as e:
                logger.error(f"❌ 无法访问讨论组 {self.discussion_chat_id}: {e}")
                return 0

        # DB 帖子 -> 待处理列表（含所属频道 chat_id）
        async with self.db.pool.acquire() as conn:
            post_rows = await conn.fetch("""
                SELECT id, message_id, chat_id FROM messages
                WHERE message_type = 'post' AND message_id IS NOT NULL
                ORDER BY created_at DESC
            """)
        if limit:
            post_rows = post_rows[:limit]
        logger.info(f"🔄 评论全量同步(v2): 待处理帖子 {len(post_rows)} 个")

        chat_entities = {}
        async def _get_chat_entity(chat_id):
            if chat_id not in chat_entities:
                chat_entities[chat_id] = await self.client.get_entity(chat_id)
            return chat_entities[chat_id]

        matched = 0
        no_discussion = 0
        errors = 0
        for i, r in enumerate(post_rows, 1):
            post_db_id, post_msg_id, post_chat_id = r['id'], r['message_id'], r['chat_id']
            try:
                channel_entity = await _get_chat_entity(post_chat_id)
                res = await self.client(GetDiscussionMessageRequest(
                    peer=channel_entity,
                    msg_id=post_msg_id,
                ))
            except MsgIdInvalidError:
                no_discussion += 1  # 帖子无讨论/已被删除
                continue
            except FloodWaitError as e:
                logger.warning(f"  ⏳ GetDiscussionMessage FloodWait {e.seconds}秒，等待后继续")
                await asyncio.sleep(e.seconds)
                continue
            except Exception as e:
                errors += 1
                logger.debug(f"  ⚠️ 获取讨论失败 post={post_msg_id}: {str(e)[:60]}")
                continue

            # 找镜像帖：replies > 0 且 date 最早（首条是频道媒体投递链，须跳过）
            mirror = None
            for m in res.messages:
                rp = getattr(m, "replies", None)
                if rp and getattr(rp, "replies", 0) > 0:
                    if mirror is None or (m.date and (mirror.date is None or m.date < mirror.date)):
                        mirror = m
            if mirror is None:
                no_discussion += 1
                continue

            # 抓镜像帖的评论
            try:
                async for cm in self.client.iter_messages(self.discussion_entity, reply_to=mirror.id):
                    text = cm.text or ""
                    if not text:
                        continue
                    created_at = cm.date
                    if created_at is not None and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    async with self.db.pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO messages (chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'comment')
                            ON CONFLICT (chat_id, message_id) WHERE message_id IS NOT NULL AND message_type = 'comment'
                            DO UPDATE SET parent_message_id = EXCLUDED.parent_message_id
                        """, cm.chat_id, cm.id, "讨论组",
                            str(cm.sender_id) if cm.sender_id else None,
                            None, text, created_at, post_db_id)
                    matched += 1
            except FloodWaitError as e:
                logger.warning(f"  ⏳ 评论抓取 FloodWait {e.seconds}秒，等待后继续")
                await asyncio.sleep(e.seconds)
            except MsgIdInvalidError:
                no_discussion += 1
            except Exception as e:
                errors += 1
                logger.debug(f"  ⚠️ 评论抓取失败 post={post_msg_id}: {str(e)[:60]}")

            if i % 100 == 0:
                logger.info(f"  📦 v2 评论同步进度: {i}/{len(post_rows)} 帖子, 已入库 {matched} 条评论")

        logger.info(f"🔄 评论全量同步(v2)完成: 处理帖子 {len(post_rows)} 个, 入库 {matched} 条评论, 无讨论 {no_discussion}, 错误 {errors}")
        return matched

    async def _store(self, msg, text: str, message_type: str = "post", parent_message_id: int = None):
        """把一条消息写入数据库（按 chat_id + message_id 去重），并保存媒体引用。
        SPA 广告帖子将被过滤掉，不保存。
        无文本的帖子（纯图片/视频）也会保存。
        返回 db id；若为已存在的重复消息（评论）或 SPA 广告则返回 None。
        """
        # 过滤 SPA 广告（仅对帖子，且需要文本判断）
        if message_type == "post" and text and is_spa_ad(text):
            logger.debug(f"Filtered SPA ad: {msg.id}")
            return None

        chat_id = msg.chat_id
        self.watched_chat_ids.add(chat_id)

        chat_title = None
        try:
            chat = await msg.get_chat()
            chat_title = getattr(chat, "title", None)
        except Exception:
            pass

        sender_name = chat_title
        user_id = None
        try:
            sender = await msg.get_sender()
            if sender is not None:
                user_id = str(sender.id)
                if isinstance(sender, Channel):
                    sender_name = sender.title or chat_title or f"Channel{sender.id}"
                elif isinstance(sender, User):
                    sender_name = sender.username or sender.first_name or f"User{sender.id}"
        except Exception:
            pass

        created_at = msg.date
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        # 先写入消息，获取 db id
        db_id = await self.db.insert_message(
            chat_id=chat_id,
            message_id=msg.id,
            chat_title=chat_title,
            user_id=user_id,
            user_name=sender_name,
            message_text=text,
            created_at=created_at,
            parent_message_id=parent_message_id,
            message_type=message_type,
        )
        return db_id

    async def sync_recent(self, limit: int = 500):
        """补采：拉取每个目标最近的 limit 条帖子，入库。"""
        total = 0
        filtered = 0
        for entity in self.watched_entities:
            try:
                posts = []
                async for m in self.client.iter_messages(entity, limit=limit):
                    posts.append(m)
                posts.reverse()
                for post in posts:
                    text = post.text or ""
                    # 不过滤无文本帖子（可能有媒体），但过滤 SPA 广告
                    if text and is_spa_ad(text):
                        filtered += 1
                        continue
                    await self._store(post, text, message_type="post")
                    total += 1
            except Exception as e:
                logger.error(f"Sync failed for {entity}: {e}")
        logger.info(f"🔄 Sync finished: {total} posts stored, {filtered} SPA filtered")
        return total

    async def full_sync_posts(self, entity=None, only_with_score: bool = False):
        """全量同步：拉取频道的全部历史帖子（含最早的老帖），自动去重入库。
        解决 sync_recent 只拉最近 limit 条导致的历史帖子漏采问题。
        参数：
          entity: 指定频道；None 则遍历所有监控频道。
          only_with_score: True 则只保留含'综合评分'的老师帖，跳过广告/通知。
        返回: 新增帖子数。
        """
        from telethon.errors import FloodWaitError

        targets = [entity] if entity else list(self.watched_entities)
        added = 0
        skipped_dup = 0
        filtered = 0
        errors = 0

        for ent in targets:
            try:
                async for m in self.client.iter_messages(ent):
                    text = m.text or ""
                    # 可选：只保留含"综合评分"的老师帖
                    if only_with_score and "综合评分" not in text:
                        continue
                    # 过滤 SPA 广告
                    if text and is_spa_ad(text):
                        filtered += 1
                        continue
                    db_id = await self._store(m, text, message_type="post")
                    if db_id:
                        added += 1
                    else:
                        skipped_dup += 1
                    if added % 200 == 0:
                        logger.info(f"  📦 全量同步进度: 已入库 {added} 条, 跳过重复 {skipped_dup} 条, 过滤 {filtered} 条")
            except FloodWaitError as e:
                errors += 1
                logger.warning(f"  ⏳ 全量同步 FloodWait {e.seconds}秒，等待后继续")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                errors += 1
                logger.error(f"  ❌ 全量同步失败 {ent}: {str(e)[:80]}")

        logger.info(f"🔄 全量同步完成: 新增 {added} 条, 过滤 {filtered} 条, 错误 {errors}")
        return added

    async def sync_comments_for_posts(self, post_ids: list):
        """对指定的帖子列表拉取评论。post_ids 是 (entity, telegram_msg_id) 的列表。
        
        错误处理：帖子被删除自动跳过，限流自动等待，不中断流程。
        """
        from telethon.errors import MsgIdInvalidError, FloodWaitError
        
        total = 0
        skipped = 0
        errors = {}
        
        for entity, post_msg_id, post_db_id in post_ids:
            try:
                async for reply in self.client.iter_messages(entity, reply_to=post_msg_id):
                    reply_text = reply.text or ""
                    if not reply_text:
                        continue
                    await self._store(reply, reply_text, message_type="comment", parent_message_id=post_db_id)
                    total += 1
            except MsgIdInvalidError:
                # 帖子已被删除，正常跳过
                skipped += 1
                logger.debug(f"   ⚠️ 帖子 {post_msg_id}: 已删除，跳过")
            except FloodWaitError as e:
                # 限流，等待后继续
                errors['flood_wait'] = errors.get('flood_wait', 0) + 1
                logger.warning(f"   ⏳ FloodWait {e.seconds}秒，等待后继续")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                # 其他错误，分类记录
                err_str = str(e).lower()
                if 'forbidden' in err_str or 'privacy' in err_str:
                    errors['forbidden'] = errors.get('forbidden', 0) + 1
                elif 'timeout' in err_str:
                    errors['timeout'] = errors.get('timeout', 0) + 1
                else:
                    errors['other'] = errors.get('other', 0) + 1
                logger.debug(f"   ⚠️ 帖子 {post_msg_id}: {str(e)[:60]}")
        
        # 汇总日志
        log_parts = [f"🔄 评论同步完成: 新增 {total} 条"]
        if skipped:
            log_parts.append(f"跳过已删除 {skipped} 个")
        if errors:
            error_detail = ", ".join(f"{k}: {v}" for k, v in errors.items())
            log_parts.append(f"错误: {error_detail}")
        logger.info("，".join(log_parts))
        return total

    async def sync_posts_content(self, chat_id: int, batch_size: int = 100):
        """同步所有帖子的最新内容：遍历数据库所有帖子，按 message_id 拉取最新文本，
        检测是否有编辑（如综合评分变更）。
        
        注意：Telegram 频道没有"编辑事件"可订阅，只能通过主动拉取对比。
        """
        from telethon.errors import MsgIdInvalidError, FloodWaitError
        
        entity = await self.client.get_entity(chat_id)
        
        # 1. 从数据库获取该频道所有帖子的 message_id
        all_posts = await self.db.get_all_posts(chat_id)
        if not all_posts:
            logger.info(f"  📭 chat_id={chat_id}: 无帖子，跳过内容同步")
            return 0
        
        logger.info(f"  📥 chat_id={chat_id}: 共 {len(all_posts)} 个帖子，开始内容同步...")
        
        updated = 0
        errors = 0
        deleted = 0
        
        # 2. 分批拉取（Telethon get_messages 支持 ids 列表）
        msg_ids = [p['message_id'] for p in all_posts if p.get('message_id')]
        for i in range(0, len(msg_ids), batch_size):
            batch = msg_ids[i:i + batch_size]
            try:
                messages = await self.client.get_messages(entity, ids=batch)
                for m in messages:
                    if m is None:
                        deleted += 1  # 帖子已被删除
                        continue
                    text = m.text or ""
                    if not text:
                        continue
                    await self.db.insert_message(
                        chat_id=chat_id,
                        message_id=m.id,
                        user_id=str(m.sender_id) if m.sender_id else None,
                        user_name=None,
                        message_text=text,
                        created_at=m.date,
                        message_type="post",
                    )
                    updated += 1
            except MsgIdInvalidError:
                deleted += len(batch)
            except FloodWaitError as e:
                logger.warning(f"  ⏳ FloodWait: {e.seconds}秒，等待后继续")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                errors += len(batch)
                logger.debug(f"  ⚠️ 批量拉取失败 ({i}-{i+len(batch)}): {str(e)[:80]}")
            
            if (i // batch_size + 1) % 5 == 0:
                logger.info(f"  📊 已处理 {min(i + batch_size, len(msg_ids))}/{len(msg_ids)} 个帖子")
            await asyncio.sleep(0.3)
        
        logger.info(
            f"🔄 帖子内容同步完成: 更新 {updated} 个, 已删除 {deleted} 个, 错误 {errors} 个"
        )
        return updated

    async def send_message(self, chat_id, text: str):
        """发送消息（纯文本，避免 Markdown 解析失败）。"""
        return await self.client.send_message(chat_id, text, parse_mode=None)

    async def stop(self):
        await self.client.disconnect()
        logger.info("Collector stopped")

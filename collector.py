"""
Telethon collector - 用用户账号采集 Telegram 频道/群组消息。

为什么用 Telethon（用户账号）而不是 Bot API：
- Bot API 只能看到"机器人是管理员/成员"的群和频道；
- 用户账号可以读取自己订阅的所有频道、加入的所有群组，
  即使没有管理权限（只有访问权限）也能采集。
"""
import logging
import os
from datetime import timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import Channel, User, MessageMediaPhoto, MessageMediaDocument

from db import Database
from gemini_service import GeminiService

logger = logging.getLogger(__name__)

SESSION_NAME = "telegram"
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))

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

        # 注册实时消息监听
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=self.watched_entities)
        )
        logger.info("👂 Real-time listener registered")

    async def _entity_title(self, name: str) -> str:
        try:
            entity = await self.client.get_entity(name)
            return getattr(entity, "title", None) or getattr(entity, "first_name", None) or name
        except Exception:
            return name

    async def _on_new_message(self, event):
        """实时收到的新消息 -> 入库。"""
        try:
            msg = event.message
            text = msg.text or ""
            if not text:
                return
            # 判断是否是评论（回复某条帖子）
            parent_id = None
            msg_type = "post"
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                parent_id = msg.reply_to.reply_to_msg_id
                msg_type = "comment"
            await self._store(msg, text, message_type=msg_type, parent_message_id=parent_id)
        except Exception as e:
            logger.error(f"Error handling new message: {e}")

    async def _store(self, msg, text: str, message_type: str = "post", parent_message_id: int = None):
        """把一条消息写入数据库（按 chat_id + message_id 去重），并保存媒体引用。
        SPA 广告帖子将被过滤掉，不保存。
        无文本的帖子（纯图片/视频）也会保存。
        """
        # 过滤 SPA 广告（仅对帖子，且需要文本判断）
        if message_type == "post" and text and is_spa_ad(text):
            logger.debug(f"Filtered SPA ad: {msg.id}")
            return

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

        # 记录媒体信息（不下载，用 Telegram 链接访问）
        if db_id and message_type == "post":
            await self._save_media_reference(msg, db_id)

    async def _save_media_reference(self, msg, db_message_id: int):
        """保存媒体文件的 Telegram 引用（不下载，节省空间）。"""
        try:
            if not msg.media:
                return

            # 判断媒体类型
            if isinstance(msg.media, MessageMediaPhoto):
                file_type = "image"
                mime_type = "image/jpeg"
            elif isinstance(msg.media, MessageMediaDocument):
                doc = msg.media.document
                mime = getattr(doc, 'mime_type', '') or ''
                if mime.startswith('video/'):
                    file_type = "video"
                elif mime.startswith('image/'):
                    file_type = "image"
                else:
                    file_type = "file"
                mime_type = mime
            else:
                return

            # 保存媒体引用（通过 message_id 可在 Telegram 查看）
            file_size = 0
            if isinstance(msg.media, MessageMediaDocument):
                file_size = getattr(msg.media.document, 'size', 0) or 0

            await self.db.insert_media(
                message_id=db_message_id,
                file_type=file_type,
                file_path=f"telegram://{msg.chat_id}/{msg.id}",
                file_name=f"{file_type}_{msg.id}",
                file_size=file_size,
                mime_type=mime_type,
                telegram_file_ref=f"{msg.chat_id}/{msg.id}",
            )

        except Exception as e:
            logger.error(f"Error saving media reference for msg {msg.id}: {e}")

    @staticmethod
    def _get_file_ext(media, file_type: str) -> str:
        """根据媒体类型返回文件扩展名。"""
        if isinstance(media, MessageMediaPhoto):
            return ".jpg"
        if isinstance(media, MessageMediaDocument):
            mime = getattr(media.document, 'mime_type', '') or ''
            ext_map = {
                'video/mp4': '.mp4',
                'video/webm': '.webm',
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/gif': '.gif',
            }
            return ext_map.get(mime, '.bin')
        return '.bin'

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

    async def sync_comments_for_posts(self, post_ids: list):
        """对指定的帖子列表拉取评论。post_ids 是 (entity, telegram_msg_id) 的列表。"""
        total = 0
        for entity, post_msg_id, post_db_id in post_ids:
            try:
                async for reply in self.client.iter_messages(entity, reply_to=post_msg_id):
                    reply_text = reply.text or ""
                    if not reply_text:
                        continue
                    await self._store(reply, reply_text, message_type="comment", parent_message_id=post_db_id)
                    total += 1
            except Exception as e:
                logger.error(f"Sync comments failed for post {post_msg_id}: {e}")
        logger.info(f"🔄 Comments sync finished: {total} comments stored")
        return total

    async def send_message(self, chat_id, text: str):
        """发送消息（纯文本，避免 Markdown 解析失败）。"""
        return await self.client.send_message(chat_id, text, parse_mode=None)

    async def stop(self):
        await self.client.disconnect()
        logger.info("Collector stopped")

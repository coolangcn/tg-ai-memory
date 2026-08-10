"""
Database module for Telegram analytics bot.
Handles PostgreSQL connection, table initialization, and data operations.
"""
import asyncpg
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Establish connection pool to PostgreSQL database."""
        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                command_timeout=60
            )
            logger.info("✅ Database connection pool established")
            await self.initialize_database()
        except Exception as e:
            logger.error(f"❌ Failed to connect to database: {e}")
            raise

    async def initialize_database(self):
        """Create tables if they don't exist."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    platform TEXT DEFAULT 'telegram',
                    chat_id BIGINT,
                    message_id BIGINT,
                    chat_title TEXT,
                    user_id TEXT,
                    user_name TEXT,
                    message_text TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    parent_message_id BIGINT,
                    message_type TEXT DEFAULT 'post'
                );
                
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_id BIGINT;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS chat_title TEXT;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS parent_message_id BIGINT;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_type TEXT DEFAULT 'post';
                
                CREATE TABLE IF NOT EXISTS media (
                    id SERIAL PRIMARY KEY,
                    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
                    file_type TEXT NOT NULL,
                    file_name TEXT,
                    file_path TEXT NOT NULL,
                    file_size BIGINT DEFAULT 0,
                    mime_type TEXT,
                    telegram_file_ref TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
                
                CREATE INDEX IF NOT EXISTS idx_messages_created_at 
                ON messages(created_at DESC);
                
                CREATE INDEX IF NOT EXISTS idx_messages_chat_id 
                ON messages(chat_id);
                
                CREATE INDEX IF NOT EXISTS idx_messages_parent 
                ON messages(parent_message_id) WHERE parent_message_id IS NOT NULL;
                
                CREATE INDEX IF NOT EXISTS idx_media_message_id
                ON media(message_id);
                
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_msg_id 
                ON messages(chat_id, message_id) WHERE message_id IS NOT NULL;
            """)
            logger.info("✅ Database tables initialized")

    async def insert_media(
        self,
        message_id: int,
        file_type: str,
        file_path: str,
        file_name: str = None,
        file_size: int = 0,
        mime_type: str = None,
        telegram_file_ref: str = None,
    ):
        """Insert a media record linked to a message."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO media (message_id, file_type, file_name, file_path, file_size, mime_type, telegram_file_ref)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT DO NOTHING
            """, message_id, file_type, file_name, file_path, file_size, mime_type, telegram_file_ref)

    async def get_media_by_message_id(self, message_id: int) -> List[Dict]:
        """Get all media files for a message."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM media WHERE message_id = $1 ORDER BY id", message_id
            )
            return [dict(r) for r in rows]

    async def get_messages_with_media(self, chat_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get posts with their media and comment count."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.*, 
                       COALESCE((
                           SELECT json_agg(json_build_object(
                               'id', m.id,
                               'file_type', m.file_type,
                               'file_path', m.file_path,
                               'file_name', m.file_name,
                               'file_size', m.file_size,
                               'mime_type', m.mime_type
                           ))
                           FROM media m WHERE m.message_id = p.id
                       ), '[]') AS media,
                       (SELECT COUNT(*) FROM messages c WHERE c.parent_message_id = p.id AND c.message_type = 'comment') AS comment_count
                FROM messages p
                WHERE p.chat_id = $1 AND p.message_type = 'post'
                ORDER BY p.created_at DESC
                LIMIT $2 OFFSET $3
            """, chat_id, limit, offset)
            return [dict(r) for r in rows]
            
            # Run initial cleanup
            await self.cleanup_old_messages()

    async def cleanup_old_messages(self):
        """Delete messages older than 14 days."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=14)
        async with self.pool.acquire() as conn:
            deleted = await conn.execute(
                "DELETE FROM messages WHERE created_at < $1",
                cutoff_date
            )
            count = deleted.split()[-1]
            logger.info(f"🗑️ Cleanup completed: {count} old messages deleted")
            return int(count)

    async def insert_message(
        self,
        chat_id: int,
        user_id: str,
        user_name: str,
        message_text: str,
        message_id: int = None,
        chat_title: str = None,
        created_at: datetime = None,
        parent_message_id: int = None,
        message_type: str = "post",
    ):
        """Insert a new message into the database (dedup by chat_id + message_id).
        Returns the db id of the message (existing or newly inserted), or None.
        """
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            # Try to insert and return id
            row = await conn.fetchrow("""
                INSERT INTO messages (chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (chat_id, message_id) WHERE message_id IS NOT NULL
                DO UPDATE SET message_text = EXCLUDED.message_text
                RETURNING id
            """, chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
            if row:
                return row["id"]
            # Fallback: message without message_id, just inserted
            return None

    async def get_messages_last_24h(self, chat_id: int) -> List[Dict]:
        """Retrieve all messages from the last 24 hours."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, user_name, message_text, created_at
                FROM messages
                WHERE chat_id = $1 AND created_at >= $2
                ORDER BY created_at ASC
            """, chat_id, cutoff_time)
            
            return [dict(row) for row in rows]

    async def get_messages_last_14_days(self, chat_id: int) -> List[Dict]:
        """Retrieve all messages from the last 14 days."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=14)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, user_name, chat_title, message_text, created_at
                FROM messages
                WHERE chat_id = $1 AND created_at >= $2
                ORDER BY created_at ASC
            """, chat_id, cutoff_time)
            
            return [dict(row) for row in rows]

    async def get_messages_for_day(self, chat_id: int, date) -> List[Dict]:
        """Retrieve all messages for a calendar day (server local timezone)."""
        start_local = datetime.combine(date, datetime.min.time()).astimezone()
        end_local = start_local + timedelta(days=1)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, user_name, chat_title, message_text, created_at
                FROM messages
                WHERE chat_id = $1 AND created_at >= $2 AND created_at < $3
                ORDER BY created_at ASC
            """, chat_id, start_local, end_local)
            
            return [dict(row) for row in rows]

    async def get_post_db_id(self, chat_id: int, message_id: int) -> int:
        """根据 chat_id + message_id 查 db 的 serial id（用于关联评论）。"""
        if not message_id:
            return None
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT id FROM messages WHERE chat_id = $1 AND message_id = $2",
                chat_id, message_id,
            )

    async def get_posts_for_date(self, chat_id: int, date) -> List[Dict]:
        """获取某天的所有帖子（含 telegram message_id 和 db id），用于后续拉评论。"""
        start_local = datetime.combine(date, datetime.min.time()).astimezone()
        end_local = start_local + timedelta(days=1)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, message_id, message_text, created_at
                FROM messages
                WHERE chat_id = $1 AND message_type = 'post'
                  AND created_at >= $2 AND created_at < $3
                ORDER BY created_at ASC
            """, chat_id, start_local, end_local)
            return [dict(r) for r in rows]

    async def get_post_count(self, chat_id: int) -> int:
        """获取频道帖子总数。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND message_type = 'post'", chat_id
            )

    async def get_all_posts_with_comments(self, chat_id: int) -> List[Dict]:
        """获取某频道的所有帖子及其评论，按帖子分组。
        返回: [{"post": {...}, "comments": [{...}, ...]}, ...]
        """
        async with self.pool.acquire() as conn:
            post_rows = await conn.fetch("""
                SELECT id, user_id, user_name, chat_title, message_text, created_at, message_id
                FROM messages
                WHERE chat_id = $1 AND message_type = 'post'
                ORDER BY created_at ASC
            """, chat_id)

            if not post_rows:
                return []

            posts = [dict(r) for r in post_rows]
            post_ids = [p["id"] for p in posts]

            comment_rows = await conn.fetch("""
                SELECT id, parent_message_id, user_id, user_name, message_text, created_at
                FROM messages
                WHERE message_type = 'comment'
                  AND parent_message_id = ANY($1)
                ORDER BY created_at ASC
            """, post_ids)

            from collections import defaultdict
            comments_by_parent = defaultdict(list)
            for c in comment_rows:
                d = dict(c)
                comments_by_parent[d["parent_message_id"]].append(d)

            result = []
            for p in posts:
                result.append({
                    "post": p,
                    "comments": comments_by_parent.get(p["id"], []),
                })
            return result

    async def get_posts_with_comments(self, chat_id: int, date) -> List[Dict]:
        """获取某天的所有帖子及其评论，按帖子分组。
        返回: [{"post": {...}, "comments": [{...}, ...]}, ...]
        """
        start_local = datetime.combine(date, datetime.min.time()).astimezone()
        end_local = start_local + timedelta(days=1)
        async with self.pool.acquire() as conn:
            # 先取所有帖子（message_type='post'）
            post_rows = await conn.fetch("""
                SELECT id, user_id, user_name, chat_title, message_text, created_at, message_id
                FROM messages
                WHERE chat_id = $1 AND message_type = 'post'
                  AND created_at >= $2 AND created_at < $3
                ORDER BY created_at ASC
            """, chat_id, start_local, end_local)

            if not post_rows:
                return []

            posts = [dict(r) for r in post_rows]
            post_ids = [p["id"] for p in posts]

            # 取这些帖子的评论（不限 chat_id，因为频道评论可能在关联讨论组）
            comment_rows = await conn.fetch("""
                SELECT id, parent_message_id, user_id, user_name, message_text, created_at
                FROM messages
                WHERE message_type = 'comment'
                  AND parent_message_id = ANY($1)
                ORDER BY created_at ASC
            """, post_ids)

            # 按 parent_message_id 分组评论
            from collections import defaultdict
            comments_by_parent = defaultdict(list)
            for c in comment_rows:
                d = dict(c)
                comments_by_parent[d["parent_message_id"]].append(d)

            result = []
            for p in posts:
                result.append({
                    "post": p,
                    "comments": comments_by_parent.get(p["id"], []),
                })
            return result

    async def get_message_count(self, chat_id: int) -> int:
        """Get total message count for a chat."""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE chat_id = $1",
                chat_id
            )
            return result

    async def close(self):
        """Close database connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("Database connection pool closed")

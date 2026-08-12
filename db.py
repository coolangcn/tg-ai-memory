"""
Database module for Telegram analytics bot.
Handles PostgreSQL connection, table initialization, and data operations.
"""
import asyncpg
import logging
import re
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
        """Create tables if they don't exist (optimized schema)."""
        async with self.pool.acquire() as conn:
            # 检查是否需要升级（旧表没有 message_type 列）
            has_message_type = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'messages' AND column_name = 'message_type'
                )
            """)
            
            if not has_message_type:
                print("🔄 检测到旧表结构，执行升级...")
                # 旧表：添加缺失的列
                await conn.execute("""
                    ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_id BIGINT;
                    ALTER TABLE messages ADD COLUMN IF NOT EXISTS chat_title TEXT;
                    ALTER TABLE messages ADD COLUMN IF NOT EXISTS parent_message_id BIGINT;
                    ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_type TEXT DEFAULT 'post';
                """)
                # 为旧数据设置默认 message_type
                await conn.execute("""
                    UPDATE messages SET message_type = 'post' WHERE parent_message_id IS NULL AND message_type IS NULL;
                    UPDATE messages SET message_type = 'comment' WHERE parent_message_id IS NOT NULL AND message_type IS NULL;
                """)
            
            # 创建新表（如果不存在）
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    platform TEXT DEFAULT 'telegram',
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT,
                    chat_title TEXT,
                    user_id TEXT,
                    user_name TEXT,
                    message_text TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    parent_message_id BIGINT,
                    message_type TEXT DEFAULT 'post' CHECK (message_type IN ('post', 'comment', 'other')),
                    comment_calculated_score DOUBLE PRECISION DEFAULT 0,
                    teacher_name TEXT
                );
            """)
            
            # 确保列存在
            await conn.execute("""
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_id BIGINT;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS chat_title TEXT;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS parent_message_id BIGINT;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_type TEXT DEFAULT 'post';
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS comment_calculated_score DOUBLE PRECISION DEFAULT 0;
                ALTER TABLE messages ADD COLUMN IF NOT EXISTS teacher_name TEXT;
            """)
            
            # 回填历史帖子的老师名称（只处理尚未回填的帖子）
            backfilled = await conn.fetchval(r"""
                UPDATE messages
                SET teacher_name = SUBSTRING(message_text FROM '👧#(\w+)')
                WHERE message_type = 'post' AND teacher_name IS NULL AND message_text IS NOT NULL
            """)
            if backfilled:
                logger.info(f"🔄 回填老师名称字段: {backfilled} 条帖子")
            
            # 清理旧索引（如果存在，忽略错误）
            await conn.execute("""
                -- 帖子查询：按频道+类型+时间
                CREATE INDEX IF NOT EXISTS idx_messages_chat_type_created 
                ON messages(chat_id, message_type, created_at DESC);
                
                -- 评论查询：按父消息 id
                CREATE INDEX IF NOT EXISTS idx_messages_parent_type 
                ON messages(parent_message_id, message_type) WHERE parent_message_id IS NOT NULL AND message_type = 'comment';
                
                -- 帖子唯一性
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_msg_id 
                ON messages(chat_id, message_id) WHERE message_id IS NOT NULL AND message_type = 'post';
                
                -- 评论唯一性（防止重复补抓）
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_comment_chat_msg_id 
                ON messages(chat_id, message_id) WHERE message_id IS NOT NULL AND message_type = 'comment';
                
                -- 评论按时间排序
                CREATE INDEX IF NOT EXISTS idx_messages_comment_created 
                ON messages(created_at DESC) WHERE message_type = 'comment';
            """)
            
            # 清理旧索引（如果存在，忽略错误）
            try:
                await conn.execute("DROP INDEX IF EXISTS idx_messages_created_at")
                await conn.execute("DROP INDEX IF EXISTS idx_messages_chat_id")
                await conn.execute("DROP INDEX IF EXISTS idx_messages_parent")
            except Exception:
                pass
            
            logger.info("✅ Database tables initialized (optimized schema)")

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
        teacher_name: str = None,
    ):
        """Insert a new message into the database (dedup by chat_id + message_id).
        帖子：冲突时更新文本。评论：冲突时跳过（DO NOTHING）。
        Returns the db id of the message (existing or newly inserted), or None.
        """
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        # 帖子自动提取老师名称（👧#名字）
        if message_type == "post" and not teacher_name and message_text:
            m = re.search(r'👧#(\w+)', message_text)
            teacher_name = m.group(1) if m else None
        async with self.pool.acquire() as conn:
            if message_type == "comment":
                # 评论：重复则跳过
                row = await conn.fetchrow("""
                    INSERT INTO messages (chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (chat_id, message_id) WHERE message_id IS NOT NULL AND message_type = 'comment'
                    DO NOTHING
                    RETURNING id
                """, chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
            else:
                # 帖子：冲突时更新文本（含老师名称）
                row = await conn.fetchrow("""
                    INSERT INTO messages (chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type, teacher_name)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (chat_id, message_id) WHERE message_id IS NOT NULL AND message_type = 'post'
                    DO UPDATE SET message_text = EXCLUDED.message_text, teacher_name = COALESCE(EXCLUDED.teacher_name, messages.teacher_name)
                    RETURNING id
                """, chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type, teacher_name)
            if row:
                return row["id"]
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

    async def get_posts_without_comments(self, chat_id: int, limit: int = 100) -> List[Dict]:
        """获取缺少评论的帖子（用于补抓评论）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.id, p.message_id, p.message_text, p.created_at
                FROM messages p
                LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
                WHERE p.chat_id = $1 AND p.message_type = 'post'
                  AND p.message_id IS NOT NULL
                  AND c.id IS NULL
                ORDER BY p.created_at DESC
                LIMIT $2
            """, chat_id, limit)
            return [dict(r) for r in rows]

    async def get_all_posts(self, chat_id: int) -> List[Dict]:
        """获取频道所有帖子（用于全量评论对账）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, message_id, message_text, created_at
                FROM messages
                WHERE chat_id = $1 AND message_type = 'post'
                  AND message_id IS NOT NULL
                ORDER BY created_at DESC
            """, chat_id)
            return [dict(r) for r in rows]

    async def get_comment_count(self, chat_id: int) -> int:
        """获取频道评论总数。"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND message_type = 'comment'", chat_id
            ) or 0

    async def get_distinct_chat_ids(self) -> list:
        """获取所有有帖子的频道 ID 列表。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT chat_id FROM messages WHERE message_type = 'post' ORDER BY chat_id"
            )
            return [r["chat_id"] for r in rows]

    async def update_post_score(self, db_id: int, new_score: float):
        """更新帖子的综合评分。"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET comment_calculated_score = $1 WHERE id = $2",
                new_score, db_id
            )

    async def calculate_comment_score(self, db_id: int) -> float:
        """根据评论计算平均评分。"""
        async with self.pool.acquire() as conn:
            # 获取所有有效评分的评论
            rows = await conn.fetch("""
                SELECT message_text FROM messages 
                WHERE parent_message_id = $1 AND message_type = 'comment'
            """, db_id)
            
            scores = []
            import re
            for row in rows:
                text = row['message_text'] or ''
                # 匹配 "整场综合评分: X.X" 格式
                m = re.search(r'整场综合评分[：:]\s*(\d+\.?\d*)', text)
                if m:
                    score = float(m.group(1))
                    if score > 0:
                        scores.append(score)
            
            if scores:
                return sum(scores) / len(scores)
            return 0

    async def update_all_comment_scores(self, chat_id: int) -> Dict[str, int]:
        """更新频道所有帖子的评论计算评分。"""
        import re
        stats = {"updated": 0, "no_comments": 0}
        
        async with self.pool.acquire() as conn:
            # 获取所有有评论的帖子
            rows = await conn.fetch("""
                SELECT DISTINCT p.id 
                FROM messages p
                INNER JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
                WHERE p.chat_id = $1 AND p.message_type = 'post'
            """, chat_id)
            
            for row in rows:
                db_id = row['id']
                # 计算该帖子的评论均分
                score = await self.calculate_comment_score(db_id)
                if score > 0:
                    await self.update_post_score(db_id, score)
                    stats["updated"] += 1
                else:
                    stats["no_comments"] += 1
        
        return stats

    async def close(self):
        """Close database connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("Database connection pool closed")

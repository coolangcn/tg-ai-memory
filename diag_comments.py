"""诊断：评论与帖子的关联情况。"""
import asyncio
import os
import re
from dotenv import load_dotenv
from db import Database

load_dotenv()

def extract_score(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r'综合评分\s*(\d+\.?\d*)', text)
    return float(m.group(1)) if m else 0.0

async def main():
    db = Database(os.getenv("DATABASE_URL"))
    await db.connect()

    async with db.pool.acquire() as conn:
        # 1. 各类消息数量
        total = await conn.fetchval("SELECT COUNT(*) FROM messages")
        posts = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type='post'")
        comments = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type='comment'")
        print(f"总消息: {total}, 帖子: {posts}, 评论: {comments}", flush=True)

        # 2. 评论的 chat_id 分布
        print("\n评论的 chat_id 分布:", flush=True)
        rows = await conn.fetch("""
            SELECT chat_id, COUNT(*) FROM messages WHERE message_type='comment' GROUP BY chat_id
        """)
        for r in rows:
            print(f"  chat_id={r['chat_id']} count={r['count']}", flush=True)

        # 3. 评论的 parent_message_id 是否有 NULL
        null_parents = await conn.fetchval("""
            SELECT COUNT(*) FROM messages WHERE message_type='comment' AND parent_message_id IS NULL
        """)
        print(f"\n评论中 parent_message_id 为 NULL: {null_parents}", flush=True)

        # 4. 高分帖子及其评论数
        print("\n评分>9 的帖子评论数分布:", flush=True)
        high_posts = await conn.fetch("""
            SELECT p.id, p.message_id, p.message_text
            FROM messages p
            WHERE p.message_type='post' AND p.message_text LIKE '%综合评分%'
        """)
        result = []
        for p in high_posts:
            score = extract_score(p['message_text'])
            if score > 9:
                cc = await conn.fetchval("""
                    SELECT COUNT(*) FROM messages WHERE parent_message_id=$1 AND message_type='comment'
                """, p['id'])
                result.append((p['id'], p['message_id'], score, cc))
        result.sort(key=lambda x: -x[2])
        print(f"评分>9 帖子数: {len(result)}", flush=True)
        for pid, mid, score, cc in result[:30]:
            print(f"  id={pid} tg_msg_id={mid} score={score} comments={cc}", flush=True)

        # 5. 看看评论样例，确认 reply_to 信息
        print("\n评论样例 (前5条):", flush=True)
        sample = await conn.fetch("""
            SELECT id, chat_id, message_id, parent_message_id, message_text
            FROM messages WHERE message_type='comment' LIMIT 5
        """)
        for r in sample:
            t = (r['message_text'] or '')[:80]
            print(f"  id={r['id']} chat={r['chat_id']} msg_id={r['message_id']} parent={r['parent_message_id']} text={t}", flush=True)

        # 6. 讨论组的实际消息数（通过 Telegram 拉取对比）
        disc_chat_id = await conn.fetchval(
            "SELECT chat_id FROM messages WHERE message_type='comment' LIMIT 1"
        )
        print(f"\n讨论组 chat_id: {disc_chat_id}", flush=True)

    await db.close()

asyncio.run(main())

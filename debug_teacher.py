"""触发回填并查询老师名称"""
import asyncio
import os
from dotenv import load_dotenv
from db import Database

load_dotenv()

async def main():
    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:cncncncn@10.88.0.3:5433/tg_bot")
    db = Database(database_url)
    await db.connect()  # 触发 initialize_database → 回填 teacher_name
    
    async with db.pool.acquire() as conn:
        # 回填统计
        total_posts = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type='post'")
        with_name = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type='post' AND teacher_name IS NOT NULL")
        print(f"📊 帖子总数: {total_posts}, 已提取老师名称: {with_name}")
        
        # 查洛儿
        rows = await conn.fetch("""
            SELECT id, message_id, teacher_name, created_at, comment_calculated_score,
                   LEFT(message_text, 150) AS text_preview
            FROM messages
            WHERE message_type='post' AND teacher_name LIKE '%洛%'
            ORDER BY created_at DESC
        """)
        print(f"\n👧 含'洛'的老师帖子: {len(rows)} 条")
        for r in rows:
            print(f"  id={r['id']} msg_id={r['message_id']} 老师={r['teacher_name']} 时间={r['created_at']} 评论评分={r['comment_calculated_score']}")
            print(f"    文本: {r['text_preview']}")
        
        # 查所有唯一老师名
        names = await conn.fetch("""
            SELECT teacher_name, COUNT(*) AS cnt FROM messages
            WHERE message_type='post' AND teacher_name IS NOT NULL
            GROUP BY teacher_name ORDER BY teacher_name
        """)
        print(f"\n📋 全部老师（{len(names)}位）:")
        print("、".join(f"{r['teacher_name']}" for r in names))
    
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
"""查找洛儿老师未上榜的原因"""
import asyncio
import os
import re
import sys
from dotenv import load_dotenv
from db import Database

load_dotenv()

async def main():
    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:cncncncn@10.88.0.3:5433/tg_bot")
    db = Database(database_url)
    await db.connect()
    
    async with db.pool.acquire() as conn:
        # 1. 查找所有含"洛儿"及相似名字的帖子
        patterns = ['洛儿', '洛尔', '洛尔', '珞儿', '洛儿']
        like_exprs = " OR ".join([f"message_text LIKE '%{p}%'" for p in patterns])
        rows = await conn.fetch(f"""
            SELECT id, message_id, message_text, created_at, comment_calculated_score
            FROM messages
            WHERE chat_id = -1002460327295 AND message_type = 'post'
              AND ({like_exprs})
            ORDER BY created_at DESC
        """)
        
        print(f"📋 找到 {len(rows)} 条含'洛儿'相关名字的帖子\n")
        
        # 2. 查找所有以"洛"开头的老师
        all_luo = await conn.fetch("""
            SELECT DISTINCT SUBSTRING(message_text FROM '👧#(\w+)') AS teacher_name
            FROM messages
            WHERE chat_id = -1002460327295 AND message_type = 'post'
              AND message_text LIKE '👧#洛%'
        """)
        print(f"👧 以'洛'开头的老师: {[r['teacher_name'] for r in all_luo]}\n")
        
        for r in rows:
            text = r['message_text'] or ""
            # 提取评分
            score_match = re.search(r'综合评分\s*(\d+\.?\d*)', text)
            score = float(score_match.group(1)) if score_match else None
            # 提取老师名
            name_match = re.search(r'👧#(\w+)', text)
            name = name_match.group(1) if name_match else "未知"
            # 评论数
            comment_count = await conn.fetchval("""
                SELECT COUNT(*) FROM messages 
                WHERE parent_message_id = $1 AND message_type = 'comment'
            """, r['id'])
            # 有效评分评论数
            valid_count = await conn.fetchval("""
                SELECT COUNT(*) FROM messages 
                WHERE parent_message_id = $1 AND message_type = 'comment'
                  AND message_text ~ '整场综合评分[：:]\\s*\\d+\\.?\\d*'
            """, r['id'])
            
            print(f"  帖子 ID: {r['id']}, message_id: {r['message_id']}")
            print(f"  老师名: {name}")
            print(f"  综合评分: {score}")
            print(f"  评论数: {comment_count}, 有效评分评论: {valid_count}")
            print(f"  评论计算评分: {r['comment_calculated_score']}")
            print(f"  发帖时间: {r['created_at']}")
            print(f"  帖子文本前200字: {text[:200]}")
            print("-" * 50)
            
            # 检查是否含综合评分关键词
            has_score_text = '综合评分' in text
            print(f"  ⚠️ 帖子文本是否含'综合评分'字样: {has_score_text}")
            print("=" * 50)
    
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())

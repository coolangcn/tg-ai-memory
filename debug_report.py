"""排查报告候选人数变化原因"""
import asyncio
import os
import sys
from dotenv import load_dotenv
from db import Database

load_dotenv()

async def main():
    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:cncncncn@10.88.0.3:5433/tg_bot")
    db = Database(database_url)
    await db.connect()
    
    async with db.pool.acquire() as conn:
        # 1. 检查筛选条件
        print("📋 筛选条件：")
        print("  - 综合评分 >= 9.18")
        print("  - 评论数 >= 10")
        print("  - chat_id = -1002460327295")
        print()
        
        # 2. 统计所有帖子的评论数
        stats = await conn.fetchrow("""
            SELECT 
                COUNT(*) as total_posts,
                COUNT(CASE WHEN comment_count >= 10 THEN 1 END) as posts_with_10_comments,
                COUNT(CASE WHEN comment_count >= 5 THEN 1 END) as posts_with_5_comments,
                COUNT(CASE WHEN comment_count >= 1 THEN 1 END) as posts_with_1_comment
            FROM (
                SELECT p.id, COUNT(c.id) as comment_count
                FROM messages p
                LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
                WHERE p.message_type = 'post' AND p.chat_id = -1002460327295
                GROUP BY p.id
            ) sub
        """)
        
        print("📊 帖子评论数统计：")
        print(f"  总帖子数: {stats['total_posts']}")
        print(f"  评论 >= 10: {stats['posts_with_10_comments']}")
        print(f"  评论 >= 5: {stats['posts_with_5_comments']}")
        print(f"  评论 >= 1: {stats['posts_with_1_comment']}")
        print()
        
        # 3. 找出满足评论条件但评分不够的帖子
        rows = await conn.fetch("""
            SELECT p.id, p.message_text, p.created_at,
                   COUNT(c.id) as comment_count
            FROM messages p
            LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
            WHERE p.message_type = 'post' AND p.chat_id = -1002460327295
              AND p.message_text LIKE '%综合评分%'
            GROUP BY p.id
            HAVING COUNT(c.id) >= 10
            ORDER BY p.created_at DESC
        """)
        
        print(f"📊 满足评论 >= 10 的帖子（共 {len(rows)} 个）：")
        print("-" * 60)
        
        valid_teachers = []
        for r in rows:
            text = r['message_text'] or ""
            # 提取评分
            import re
            score_match = re.search(r'综合评分\s*(\d+\.?\d*)', text)
            score = float(score_match.group(1)) if score_match else 0
            
            # 提取老师名字
            name_match = re.search(r'👧#(\w+)', text)
            name = name_match.group(1) if name_match else "未知"
            
            status = "✅" if score >= 9.18 else "❌"
            if score >= 9.18:
                valid_teachers.append(name)
            
            print(f"  {status} {name}: 评分={score}, 评论={r['comment_count']}, 时间={r['created_at'].strftime('%Y-%m-%d')}")
        
        print("-" * 60)
        print(f"\n✅ 满足所有条件的老师（评分>=9.18 且 评论>=10）：{len(valid_teachers)} 个")
        print(f"   {valid_teachers}")
        
        # 4. 检查评分在 9.0-9.18 之间的帖子（接近达标）
        rows2 = await conn.fetch("""
            SELECT p.id, p.message_text, p.created_at,
                   COUNT(c.id) as comment_count
            FROM messages p
            LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
            WHERE p.message_type = 'post' AND p.chat_id = -1002460327295
              AND p.message_text LIKE '%综合评分%'
            GROUP BY p.id
            HAVING COUNT(c.id) >= 10
            ORDER BY p.created_at DESC
        """)
        
        print(f"\n📊 接近达标的老师（评分 9.0-9.18，评论>=10）：")
        close_count = 0
        for r in rows2:
            text = r['message_text'] or ""
            score_match = re.search(r'综合评分\s*(\d+\.?\d*)', text)
            score = float(score_match.group(1)) if score_match else 0
            
            if 9.0 <= score < 9.18:
                name_match = re.search(r'👧#(\w+)', text)
                name = name_match.group(1) if name_match else "未知"
                print(f"  ⚠️ {name}: 评分={score}, 评论={r['comment_count']}")
                close_count += 1
        
        if close_count == 0:
            print("  无")
    
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())

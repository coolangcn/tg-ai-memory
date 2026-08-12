"""修复重复数据：删除错误 chat_id 的重复帖子，保留唯一数据。"""
import asyncio
import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()

async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
    
    print("🔧 开始修复重复数据...")
    
    # 1. 删除错误 chat_id 中与正确 chat_id 重复的帖子
    deleted = await conn.execute("""
        DELETE FROM messages 
        WHERE chat_id = 2460327295 
          AND message_type = 'post'
          AND message_id IN (
              SELECT message_id FROM messages WHERE chat_id = -1002460327295
          )
    """)
    print(f"✅ 已删除重复帖子: {deleted}")
    
    # 2. 将错误 chat_id 剩余的帖子更新为正确 chat_id
    updated = await conn.execute("""
        UPDATE messages SET chat_id = -1002460327295 WHERE chat_id = 2460327295
    """)
    print(f"✅ 已更新 chat_id: {updated}")
    
    # 3. 验证结果
    rows = await conn.fetch("""
        SELECT chat_id, message_type, COUNT(*) as cnt 
        FROM messages GROUP BY chat_id, message_type ORDER BY chat_id, message_type
    """)
    print("\n📊 修复后数据分布:")
    for r in rows:
        print(f"  chat_id={r['chat_id']}, type={r['message_type']}, count={r['cnt']}")
    
    # 4. 检查是否还有重复
    dups = await conn.fetchval("""
        SELECT COUNT(*) FROM (
            SELECT message_id, COUNT(*) as cnt
            FROM messages 
            WHERE message_type = 'post' AND message_id IS NOT NULL
            GROUP BY message_id 
            HAVING COUNT(*) > 1
        ) t
    """)
    print(f"\n📊 剩余重复 message_id: {dups}")
    
    await conn.close()
    print("✅ 修复完成")

asyncio.run(main())

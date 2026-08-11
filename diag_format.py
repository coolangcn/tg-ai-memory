"""查看帖子格式和讨论组报告格式，确定关联逻辑。"""
import asyncio
import os
from dotenv import load_dotenv
from db import Database

load_dotenv()

async def main():
    db = Database(os.getenv("DATABASE_URL"))
    await db.connect()

    async with db.pool.acquire() as conn:
        # 1. 查看评分>9的帖子完整内容（前3条）
        print("="*60, flush=True)
        print("评分>9 帖子内容样例:", flush=True)
        posts = await conn.fetch("""
            SELECT id, message_id, message_text FROM messages
            WHERE message_type='post' AND message_text LIKE '%综合评分%'
            ORDER BY id DESC LIMIT 3
        """)
        for p in posts:
            print(f"\n--- 帖子 id={p['id']} tg_msg_id={p['message_id']} ---", flush=True)
            print((p['message_text'] or '')[:800], flush=True)

        # 2. 查看讨论组报告内容（前3条）
        print("\n" + "="*60, flush=True)
        print("讨论组报告内容样例:", flush=True)
        reports = await conn.fetch("""
            SELECT id, message_id, parent_message_id, message_text FROM messages
            WHERE message_type='comment' ORDER BY id DESC LIMIT 3
        """)
        for r in reports:
            print(f"\n--- 报告 id={r['id']} tg_msg_id={r['message_id']} parent={r['parent_message_id']} ---", flush=True)
            print((r['message_text'] or '')[:600], flush=True)

        # 3. 帖子中老师名字的格式统计
        print("\n" + "="*60, flush=True)
        print("帖子中老师标识格式统计:", flush=True)
        post_texts = await conn.fetch("""
            SELECT message_text FROM messages WHERE message_type='post' AND message_text LIKE '%综合评分%' LIMIT 500
        """)
        patterns = {}
        import re
        for r in post_texts:
            t = r['message_text'] or ''
            if re.search(r'👧#(\w+)', t):
                patterns.setdefault('👧#name', 0); patterns['👧#name'] += 1
            elif re.search(r'#(\w+)', t):
                patterns.setdefault('#name', 0); patterns['#name'] += 1
            elif re.search(r'老师[：:]', t):
                patterns.setdefault('老师:', 0); patterns['老师:'] += 1
            else:
                patterns.setdefault('other', 0); patterns['other'] += 1
        print(patterns, flush=True)

        # 4. 讨论组报告中老师名字格式统计
        print("\n讨论组报告老师格式统计:", flush=True)
        report_texts = await conn.fetch("""
            SELECT message_text FROM messages WHERE message_type='comment' LIMIT 500
        """)
        rep_patterns = {}
        for r in report_texts:
            t = r['message_text'] or ''
            if re.search(r'老师[：:]\s*#?(\w+)', t):
                rep_patterns.setdefault('老师:name', 0); rep_patterns['老师:name'] += 1
            elif re.search(r'#(\w+)', t):
                rep_patterns.setdefault('#name', 0); rep_patterns['#name'] += 1
            else:
                rep_patterns.setdefault('other', 0); rep_patterns['other'] += 1
        print(rep_patterns, flush=True)

    await db.close()

asyncio.run(main())

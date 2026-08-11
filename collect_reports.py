"""全量采集讨论组报告，按老师名字关联到频道帖子。

讨论组里的「苏州硬了么报告模板」是用户评价报告，包含：
- 老师：#名字
- 各项评分（人照、身材、气质、服务、态度、舒适度）
- 整场综合评分
- 服务过程描述、优缺点、个人评价

先删除旧的错误关联评论，再全量重新采集。
"""
import asyncio
import os
import re
from dotenv import load_dotenv
from db import Database
from collector import TelegramCollector

load_dotenv()

# 讨论组 chat_id（之前诊断得到）
DISCUSSION_CHAT_ID = -1003367541028


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


def parse_report_fields(text: str) -> dict:
    """解析报告的各项评分字段。"""
    fields = {}
    patterns = {
        '人照差别': r'人照差别[：:]\s*(\d+\.?\d*)',
        '身材比例': r'身材比例[：:]\s*(\d+\.?\d*)',
        '气质魅力': r'气质魅力[：:]\s*(\d+\.?\d*)',
        '服务水平': r'服务水平[：:]\s*(\d+\.?\d*)',
        '配合态度': r'配合态度[：:]\s*(\d+\.?\d*)',
        '舒适度': r'舒适度[：:]\s*(\d+\.?\d*)',
        '整场综合评分': r'整场综合评分[：:]\s*(\d+\.?\d*)',
        '留名': r'留名[：:]\s*(\S+)',
        '时间': r'时间[：:]\s*([\d\-]+)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            fields[key] = m.group(1).strip()
    return fields


async def main():
    db = Database(os.getenv("DATABASE_URL"))
    await db.connect()

    print("🔌 启动采集器...", flush=True)
    collector = TelegramCollector(
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
        os.getenv("TELEGRAM_PHONE"),
        db,
        os.getenv("WATCH_CHANNELS").split(","),
    )
    await collector.start()
    print("   采集器已就绪", flush=True)

    # 1. 构建帖子名字映射: teacher_name -> post db id
    async with db.pool.acquire() as conn:
        post_rows = await conn.fetch("""
            SELECT id, message_id, message_text FROM messages
            WHERE message_type='post' AND message_text LIKE '%👧#%'
        """)
    name_to_post = {}
    for r in post_rows:
        name = extract_teacher_from_post(r['message_text'])
        if name:
            # 同名字取最新帖
            name_to_post[name] = (r['id'], r['message_id'])
    print(f"📋 帖子名字映射: {len(name_to_post)} 位老师", flush=True)

    # 2. 删除旧评论
    async with db.pool.acquire() as conn:
        old = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type='comment'")
        await conn.execute("DELETE FROM messages WHERE message_type='comment'")
    print(f"🗑️ 已删除旧评论: {old} 条", flush=True)

    # 3. 全量拉取讨论组消息
    print("🔄 全量拉取讨论组消息...", flush=True)
    total = 0
    report_count = 0
    matched = 0
    unmatched = 0
    no_teacher = 0

    async for m in collector.client.iter_messages(DISCUSSION_CHAT_ID, limit=20000):
        total += 1
        text = m.text or ""
        if not text:
            continue

        # 只保留报告模板消息
        if not is_report(text):
            continue
        report_count += 1

        teacher = extract_teacher_from_report(text)
        if not teacher:
            no_teacher += 1
            continue

        # 匹配帖子
        post_info = name_to_post.get(teacher)
        if not post_info:
            unmatched += 1
            continue

        post_db_id, _ = post_info
        matched += 1

        # 写入评论（去重：同讨论组消息只写一次）
        created_at = m.date
        if created_at is not None and created_at.tzinfo is None:
            import datetime
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)

        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO messages (chat_id, message_id, chat_title, user_id, user_name, message_text, created_at, parent_message_id, message_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'comment')
                ON CONFLICT (chat_id, message_id) WHERE message_id IS NOT NULL
                DO UPDATE SET parent_message_id = EXCLUDED.parent_message_id
            """, m.chat_id, m.id, "讨论组", str(m.sender_id), teacher, text, created_at, post_db_id)

        if matched % 100 == 0:
            print(f"   已匹配 {matched} 条报告...", flush=True)
            await asyncio.sleep(0.3)

    print(f"\n📊 讨论组总消息: {total}, 报告: {report_count}", flush=True)
    print(f"   ✅ 匹配到帖子: {matched}", flush=True)
    print(f"   ❌ 未匹配: {unmatched} (老师不在频道帖子中)", flush=True)
    print(f"   ⚠️ 无老师名字: {no_teacher}", flush=True)

    # 4. 统计关联结果
    async with db.pool.acquire() as conn:
        # 每个老师的评论数
        rows = await conn.fetch("""
            SELECT p.id, p.message_text, COUNT(c.id) as cc
            FROM messages p
            LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
            WHERE p.message_type = 'post' AND p.message_text LIKE '%综合评分%'
            GROUP BY p.id
            ORDER BY cc DESC
        """)
        print(f"\n📈 帖子评论数分布:", flush=True)
        import re as _re
        def _score(t):
            m = _re.search(r'综合评分\s*(\d+\.?\d*)', t or '')
            return float(m.group(1)) if m else 0
        scored = [(r['id'], _score(r['message_text']), r['cc']) for r in rows]
        for pid, sc, cc in sorted(scored, key=lambda x: -x[1])[:15]:
            print(f"   id={pid} score={sc} comments={cc}", flush=True)

        # 评分>9 且 评论>=10 的老师
        top = [(r['id'], _score(r['message_text']), r['cc']) for r in rows]
        eligible = [x for x in top if x[1] > 9 and x[2] >= 10]
        print(f"\n🎯 评分>9 且 评论>=10 的老师: {len(eligible)} 位", flush=True)
        for pid, sc, cc in sorted(eligible, key=lambda x: -x[1])[:20]:
            print(f"   id={pid} score={sc} comments={cc}", flush=True)

    await collector.stop()
    await db.close()
    print("\n✅ 报告采集完成", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

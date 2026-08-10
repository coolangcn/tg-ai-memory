"""把高分老师精华报告发到 Telegram 收藏夹（图文并茂 v3）。"""
import asyncio
import os
import re
from dotenv import load_dotenv
from db import Database
from gemini_service import GeminiService
from collector import TelegramCollector

load_dotenv()


def extract_score(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r'综合评分\s*(\d+\.?\d*)', text)
    return float(m.group(1)) if m else 0.0


def extract_teacher_name(text: str) -> str:
    if not text:
        return "未知"
    m = re.search(r'👧#?(\w+)', text)
    if m:
        return m.group(1)
    m = re.search(r'老师[：:]\s*#?(\w+)', text)
    if m:
        return m.group(1)
    return "未知"


def extract_detail_scores(text: str) -> dict:
    scores = {}
    patterns = {
        '人照': r'人照\s*(\d+\.?\d*)',
        '气质': r'气质\s*(\d+\.?\d*)',
        '舒适': r'舒适\s*(\d+\.?\d*)',
        '态度': r'态度\s*(\d+\.?\d*)',
        '身材': r'身材\s*(\d+\.?\d*)',
        '服务': r'服务\s*(\d+\.?\d*)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            scores[key] = float(m.group(1))
    return scores


def extract_tags(text: str) -> list:
    tags = []
    tags.extend(re.findall(r'#(\w+)', text))
    tags.extend(re.findall(r'🌐(\w+)', text))
    return list(set(tags))


def extract_report_count(text: str) -> int:
    if not text:
        return 0
    m = re.search(r'(\d+)份?报告', text)
    return int(m.group(1)) if m else 0


async def main():
    db = Database(os.getenv("DATABASE_URL"))
    await db.connect()
    gemini = GeminiService(os.getenv("OPENAI_API_KEY"))

    # 获取所有帖子
    async with db.pool.acquire() as conn:
        posts = await conn.fetch("""
            SELECT p.*
            FROM messages p
            WHERE p.chat_id = -1002460327295 AND p.message_type = 'post'
            ORDER BY p.created_at DESC
        """)

    # 提取评分 > 9 的帖子
    high_score_posts = []
    for p in posts:
        text = p['message_text'] or ""
        score = extract_score(text)
        if score <= 9.0:
            continue

        # 获取评论
        async with db.pool.acquire() as conn:
            comments = await conn.fetch(
                "SELECT * FROM messages WHERE parent_message_id = $1 AND message_type = 'comment' ORDER BY created_at",
                p['id']
            )

        high_score_posts.append({
            'post': dict(p),
            'score': score,
            'teacher': extract_teacher_name(text),
            'detail_scores': extract_detail_scores(text),
            'tags': extract_tags(text),
            'report_count': extract_report_count(text),
            'comment_count': len(comments),
            'comments': [dict(c) for c in comments],
        })

    if not high_score_posts:
        print("没有找到评分 > 9 的帖子")
        await db.close()
        return

    # 按评分排序，取前 20
    high_score_posts.sort(key=lambda x: x['score'], reverse=True)
    top_posts = high_score_posts[:20]

    print(f"筛选后: {len(top_posts)} 位老师（评分>9, TOP20）", flush=True)

    # 生成报告
    channel_username = os.getenv("CHANNEL_USERNAME", "SZnewls")

    def link_fn(post):
        mid = post.get("message_id")
        if channel_username and mid:
            return f"https://t.me/{channel_username}/{mid}"
        return ""

    # 构建带所有评论的上下文（精简版）
    report_context = []
    for i, item in enumerate(top_posts, 1):
        post = item['post']
        ts = post['created_at'].strftime('%m-%d') if post.get('created_at') else ''
        url = link_fn(post)
        text = post.get('message_text', '')
        # 截断帖子内容
        if len(text) > 300:
            text = text[:300] + '...'

        context = f"【第{i}名】{item['teacher']} 综合评分{item['score']} ({ts})\n"
        context += f"分项: {item['detail_scores']}\n"
        context += f"报告数: {item['report_count']}, 评论数: {item['comment_count']}\n"
        context += f"内容: {text}\n"
        context += f"链接: {url}\n"
        if item['comments']:
            context += "评论:\n"
            # 最多取 5 条关键评论
            for c in item['comments'][:5]:
                ct = c.get('message_text', '')[:100]
                context += f"  - {ct}\n"
        report_context.append(context)

    context_text = "\n".join(report_context)

    prompt = f"""你是 Telegram 频道「苏州硬了么认证老师榜」的资深分析师。下面是频道历史所有高分老师帖子及其全部用户评价。

=== 数据 ===
{context_text}

=== 任务 ===
1. 为每位老师生成详细分析报告，结合其**所有评论**总结归纳
2. 为每位老师打上标签（如：服务好、身材棒、性价比高、颜值高、温柔、会聊天、回头客多等，根据评论内容判断）
3. 按综合评分从高到低排列，只输出前 20 名
4. 每位老师包含：
   - 排名 + 老师名 + 综合评分 + 分项评分
   - 标签（3-5 个，基于评论归纳）
   - 精华总结（2-3 句话，结合所有评论归纳核心特点）
   - 代表性好评摘录（引用 2-3 条最有价值的评论原话）
   - 帖子链接
5. SPA/广告帖子直接忽略

=== 输出格式 ===

🏆 **苏州硬了么认证老师榜 TOP 20**
（综合评分 > 9 分）

---

**第1名：{top_posts[0]['teacher']}** ⭐ {top_posts[0]['score']}/10
📊 分项：{top_posts[0]['detail_scores']}
🏷 标签： tag1, tag2, tag3
📝 总结：...
💬 评价：
  > "..."
  > "..."
🔗 原帖：https://t.me/{channel_username}/{top_posts[0]['post'].get('message_id', '')}

---

（依次列出第 2-20 名）

📊 总计：{len(top_posts)} 位高分老师
"""

    report = await gemini._chat(prompt)

    # 发到 Telegram 收藏夹
    print("📤 正在发送到收藏夹...", flush=True)
    collector = TelegramCollector(
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
        os.getenv("TELEGRAM_PHONE"),
        db,
        os.getenv("WATCH_CHANNELS").split(","),
    )
    await collector.start()

    entity = await collector.client.get_entity("me")

    # 1. 发送文字报告（分段）
    if len(report) > 4000:
        parts = []
        lines = report.split('\n')
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 4000:
                parts.append(current)
                current = line + '\n'
            else:
                current += line + '\n'
        if current:
            parts.append(current)
        for i, part in enumerate(parts, 1):
            await collector.client.send_message(entity, f"📊 精华报告 ({i}/{len(parts)})\n\n{part}")
            await asyncio.sleep(1)
    else:
        await collector.client.send_message(entity, report)

    await asyncio.sleep(2)

    # 2. 图文并茂：为前 10 位老师发送多图（相册）
    print("📸 发送老师照片...", flush=True)
    chat_id = -1002460327295

    for i, item in enumerate(top_posts[:10], 1):
        post = item['post']
        teacher = item['teacher']
        score = item['score']
        msg_id = post.get('message_id')

        if not msg_id:
            continue

        try:
            # 获取原消息（包含图片）
            msg = await collector.client.get_messages(chat_id, ids=msg_id)
            if not msg or not msg.media:
                continue

            # 构建图文明 caption
            caption = (
                f"🏆 第{i}名：{teacher}\n"
                f"⭐ 综合评分：{score}/10\n"
                f"🏷 标签：{', '.join(item['tags'][:3])}\n"
                f"� 分项：{item['detail_scores']}\n"
                f"� 评论：{item['comment_count']}条\n"
                f"🔗 https://t.me/{channel_username}/{msg_id}"
            )

            # 发送媒体（支持多图）
            await collector.client.send_file(
                entity,
                msg.media,
                caption=caption[:1024],
            )

            await asyncio.sleep(2)

        except Exception as e:
            print(f"   发送 {teacher} 失败: {e}", flush=True)

    # 3. 发送剩余老师链接
    remaining = top_posts[10:]
    if remaining:
        links_text = "📋 第 11-20 名老师帖子链接：\n\n"
        for i, item in enumerate(remaining, 11):
            post = item['post']
            teacher = item['teacher']
            score = item['score']
            msg_id = post.get('message_id')
            url = f"https://t.me/{channel_username}/{msg_id}" if msg_id else ""
            line = f"{i}. {teacher} ({score}分, {item['comment_count']}条评论) → {url}\n"
            if len(links_text) + len(line) > 4000:
                await collector.client.send_message(entity, links_text)
                links_text = "📋 链接（续）：\n\n"
                await asyncio.sleep(1)
            links_text += line
        if links_text.strip():
            await collector.client.send_message(entity, links_text)

    print("✅ 已发送到 Telegram 收藏夹（图文并茂 v3）", flush=True)

    await collector.stop()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())

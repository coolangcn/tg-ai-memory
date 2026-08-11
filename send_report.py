"""高分老师榜单报告 - 图文并茂版。
过滤条件：综合评分 >= 9.18 且 有效评价(报告) >= 10 条。
按评分排序取前 20 名，附图片、标签、评论总结、原帖链接。
"""
import asyncio
import os
import re
import random
from dotenv import load_dotenv
from db import Database
from gemini_service import GeminiService
from collector import TelegramCollector

load_dotenv()

CHANNEL_ID = -1002460327295
MIN_SCORE = 9.18
MIN_COMMENTS = 10
TOP_N = 20
MAX_COMMENTS_PER_TEACHER = 12  # 上下文采样上限


def extract_score(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r'综合评分\s*(\d+\.?\d*)', text)
    return float(m.group(1)) if m else 0.0


def extract_teacher_name(text: str) -> str:
    if not text:
        return "未知"
    m = re.search(r'👧#(\w+)', text)
    if m:
        return m.group(1)
    return "未知"


def extract_detail_scores(text: str) -> dict:
    """从帖子提取各项评分。"""
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
    tags = re.findall(r'#(\w+)', text or '')
    # 过滤掉已经作为名字/区域的常见标签
    stop = {'苏州', '硬了么'}
    return [t for t in tags if t not in stop][:8]


def extract_report_score(report_text: str) -> float:
    """从用户报告提取整场综合评分。"""
    m = re.search(r'整场综合评分[：:]\s*(\d+\.?\d*)', report_text or '')
    return float(m.group(1)) if m else 0.0


def summarize_report(report_text: str, max_len: int = 250) -> str:
    """压缩报告：保留关键字段和评价。"""
    if not report_text:
        return ""
    t = report_text.strip()
    # 提取关键部分：优缺点和个人评价
    key_parts = []
    for pat in [r'【过程描述】(.{0,200})', r'【优点】([^【]{0,80})', r'【建议/缺点】([^【]{0,80})', r'【个人评价总结】([^【\n]{0,60})']:
        m = re.search(pat, t)
        if m and m.group(1).strip():
            key_parts.append(m.group(1).strip())
    if key_parts:
        return " | ".join(key_parts)[:max_len]
    return t[:max_len]


async def generate_and_send(db, collector, gemini, channel_id: int = CHANNEL_ID):
    """核心函数：生成榜单报告并发送到收藏夹。供 main 调度器和独立脚本复用。"""
    # 获取评分 > 9 的帖子及评论数
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT p.id, p.message_id, p.message_text, p.created_at,
                   COUNT(c.id) AS comment_count
            FROM messages p
            LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
            WHERE p.message_type = 'post' AND p.chat_id = $1
              AND p.message_text LIKE '%综合评分%'
            GROUP BY p.id
            HAVING COUNT(c.id) >= $2
            ORDER BY p.created_at DESC
        """, channel_id, MIN_COMMENTS)

    candidates = []
    for r in rows:
        text = r['message_text'] or ""
        score = extract_score(text)
        if score >= MIN_SCORE:
            candidates.append({
                'post': dict(r),
                'score': score,
                'teacher': extract_teacher_name(text),
                'detail_scores': extract_detail_scores(text),
                'tags': extract_tags(text),
                'comment_count': r['comment_count'],
            })

    if not candidates:
        print(f"没有找到 评分>={MIN_SCORE} 且 评论>={MIN_COMMENTS} 的帖子")
        return None

    # 按评分排序取前 TOP_N
    candidates.sort(key=lambda x: x['score'], reverse=True)
    top = candidates[:TOP_N]

    print(f"🎯 候选 {len(candidates)} 位，取前 {len(top)} 名", flush=True)

    # 为每位老师加载评论并采样
    for item in top:
        async with db.pool.acquire() as conn:
            comments = await conn.fetch("""
                SELECT message_text, created_at FROM messages
                WHERE parent_message_id = $1 AND message_type = 'comment'
                ORDER BY created_at DESC
            """, item['post']['id'])
        comments = [dict(c) for c in comments]

        # 采样：按报告评分排序，高低混合取 MAX_COMMENTS_PER_TEACHER 条
        scored = [(extract_report_score(c['message_text']), c) for c in comments]
        scored.sort(key=lambda x: x[0], reverse=True)
        if len(scored) > MAX_COMMENTS_PER_TEACHER:
            # 高分 + 低分 + 随机中间
            high = scored[:5]
            low = scored[-2:]
            mid = random.sample(scored[5:-2], min(MAX_COMMENTS_PER_TEACHER - 7, len(scored) - 7)) if len(scored) > 7 else []
            sampled = high + mid + low
        else:
            sampled = scored
        sampled.sort(key=lambda x: x[0], reverse=True)
        item['comments'] = sampled
        item['avg_report_score'] = sum(extract_report_score(c['message_text']) for c in comments) / len(comments) if comments else 0
        print(f"  {item['teacher']}: score={item['score']} 评论={item['comment_count']} (采样{len(sampled)})", flush=True)

    # 构建每批数据（每批 3 位老师，避免超 TPM 限制）
    print("🤖 分批生成分析报告...", flush=True)

    def build_batch_context(batch):
        parts = []
        for item in batch:
            post = item['post']
            text = post['message_text'] or ""
            ts = post['created_at'].strftime('%Y-%m-%d') if post.get('created_at') else ''
            msg_id = post.get('message_id', '')
            ctx = (
                f"老师：{item['teacher']}（帖子时间 {ts}）\n"
                f"帖子综合评分：{item['score']}/10，报告数：{item['comment_count']}\n"
                f"分项评分：{item['detail_scores']}\n"
                f"原帖链接：https://t.me/SZnewls/{msg_id}\n"
                f"帖子简介：{text[:150]}\n"
                f"用户评价报告抽样（[分数] 内容）:\n"
            )
            for sc, c in item['comments']:
                summarized = summarize_report(c['message_text'], max_len=180)
                if summarized:
                    ctx += f"  - [{sc}分] {summarized}\n"
            parts.append(ctx)
        return "\n\n".join(parts)

    def build_batch_prompt(batch):
        batch_ctx = build_batch_context(batch)
        names = "、".join(x['teacher'] for x in batch)
        return f"""你是 Telegram 频道「苏州硬了么」的资深分析师，擅长从用户评价报告中提炼老师特点。

下面是 {len(batch)} 位高分老师的帖子信息与用户评价报告抽样（[分数] 为报告评分）：

=== 数据 ===
{batch_ctx}

=== 任务 ===
针对【{names}】这 {len(batch)} 位老师，每一位输出如下（全部中文）：

第N名：老师名
🏷 标签：3-5个标签（如：服务好、身材棒、性价比高、颜值高、温柔、会聊天、回头客多、真实可靠等，基于评价内容）
📊 分项：人照X 气质X 舒适X 态度X 身材X 服务X
📝 总结：2-3句，结合评价归纳核心特点（颜值/身材/服务/性格/性价比）
💬 用户评价：引用1-2条最有说服力的评价原话（简短）
🔗 原帖：直接使用数据中提供的"原帖链接"

要求：严格基于提供的数据，禁止编造；每条评价原话不超过50字；保持简洁。排名按数据中出现顺序即可。
"""

    # 分批生成
    batch_size = 3
    all_analyses = []
    for i in range(0, len(top), batch_size):
        batch = top[i:i + batch_size]
        prompt = build_batch_prompt(batch)
        try:
            text = await gemini._chat(prompt, max_tokens=2000)
            all_analyses.append(text)
            print(f"  ✅ 批次 {i//batch_size + 1} 完成 ({len(batch)}位: {'、'.join(x['teacher'] for x in batch)})", flush=True)
        except Exception as e:
            err = str(e)
            print(f"  ⚠️ 批次 {i//batch_size + 1} Gemini 被拦: {err[:100]}", flush=True)
            # 回退：用 llama（无内容过滤），减少采样条数
            try:
                gemini_llama = GeminiService(os.getenv("OPENAI_API_KEY"), model="llama-3.3-70b-versatile")
                # 临时减少采样
                for item in batch:
                    item['comments'] = item['comments'][:6]
                prompt_retry = build_batch_prompt(batch)
                text = await gemini_llama._chat(prompt_retry, max_tokens=2000)
                all_analyses.append(text)
                print(f"  ✅ 批次 {i//batch_size + 1} llama 重试成功", flush=True)
            except Exception as e2:
                print(f"  ❌ 批次 {i//batch_size + 1} llama 也失败: {str(e2)[:150]}", flush=True)
                all_analyses.append(f"（{'、'.join(x['teacher'] for x in batch)} 分析生成失败，请在 Web 页面查看原帖）")
        # 避免 TPM 限制
        await asyncio.sleep(3)

    # 拼接最终报告
    header = (
        f"🏆 苏州硬了么高分老师 TOP {len(top)}\n"
        f"（综合评分≥{MIN_SCORE}，评价报告≥{MIN_COMMENTS}条，共{len(candidates)}位达标）\n\n"
    )
    report = header + "\n\n".join(all_analyses)
    report += f"\n\n📊 数据统计：共 {len(top)} 位上榜老师，最高分 {top[0]['score']}，最低分 {top[-1]['score']}"

    # 发到收藏夹
    print("📤 发送到收藏夹...", flush=True)
    entity = await collector.client.get_entity("me")

    # 文字报告分段发送
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
            await collector.client.send_message(entity, f"📊 榜单报告 ({i}/{len(parts)})\n\n{part}")
            await asyncio.sleep(1)
    else:
        await collector.client.send_message(entity, report)

    await asyncio.sleep(2)

    # 前 10 名附原帖图片
    print("📸 发送前 10 名图片...", flush=True)
    for i, item in enumerate(top[:10], 1):
        post = item['post']
        msg_id = post.get('message_id')
        if not msg_id:
            continue
        try:
            msg = await collector.client.get_messages(channel_id, ids=msg_id)
            if not msg or not msg.media:
                continue
            caption = (
                f"🏆 第{i}名：{item['teacher']}\n"
                f"⭐ 综合评分：{item['score']}/10（{item['comment_count']}份报告）\n"
                f"🏷 标签：{', '.join(item['tags'][:4])}\n"
                f"📊 分项：{item['detail_scores']}\n"
                f"🔗 https://t.me/SZnewls/{msg_id}"
            )
            await collector.client.send_file(entity, msg.media, caption=caption[:1000])
            await asyncio.sleep(2)
        except Exception as e:
            print(f"   发送 {item['teacher']} 图片失败: {e}", flush=True)

    # 剩余老师链接
    remaining = top[10:]
    if remaining:
        links = "📋 第 11-20 名帖子链接：\n\n"
        for i, item in enumerate(remaining, 11):
            msg_id = item['post'].get('message_id')
            line = f"{i}. {item['teacher']}（{item['score']}分，{item['comment_count']}份报告）→ https://t.me/SZnewls/{msg_id}\n"
            if len(links) + len(line) > 4000:
                await collector.client.send_message(entity, links)
                links = "📋 链接（续）：\n\n"
                await asyncio.sleep(1)
            links += line
        if links.strip():
            await collector.client.send_message(entity, links)

    print("✅ 榜单已发送！", flush=True)
    return report


async def main():
    db = Database(os.getenv("DATABASE_URL"))
    await db.connect()
    gemini = GeminiService(os.getenv("OPENAI_API_KEY"))

    collector = TelegramCollector(
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
        os.getenv("TELEGRAM_PHONE"),
        db,
        os.getenv("WATCH_CHANNELS").split(","),
    )
    await collector.start()

    try:
        await generate_and_send(db, collector, gemini)
    finally:
        await collector.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

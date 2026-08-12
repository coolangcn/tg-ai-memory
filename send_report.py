"""高分老师榜单报告 - 程序化排版 + AI 内容增强版。

过滤条件：综合评分 >= 9.13 且 有效评价(报告) >= 10 条。

设计要点：
1. 排名/布局由代码统一控制：按综合评分排序、固定模板，保证一目了然、连续不乱；
2. AI 只负责内容增强：一次调用处理全部老师，返回严格 JSON（标签/总结/精选评价），
   不参与排名输出，避免多批次导致排名错乱；
3. 信息丰富：综合分、报告数、用户均分、发帖日期、六项分项、标签、帖子摘要、
   AI 总结、AI 精选评价、低分提醒、原帖链接；
4. AI 失败自动回退本地提取，报告永不中断。
"""
import asyncio
import json
import os
import re
from datetime import datetime
from dotenv import load_dotenv
from db import Database
from gemini_service import GeminiService
from collector import TelegramCollector

load_dotenv()

CHANNEL_ID = -1002460327295
MIN_SCORE = 9.13
MIN_COMMENTS = 10
TOP_N = 20
MAX_QUOTE_LEN = 45
SEP = "━" * 26
RANK_EMOJI = {1: "🥇", 2: "🥈", 3: "🥉"}

# AI 内容生成模型（按顺序重试；可用 RANKING_AI_MODEL 环境变量覆盖主力模型）
# 经实测：gemini-3.5-flash 中文总结质量好、速度快、稳定；glm-4.6 / gpt-5.4-mini 作备选
PRIMARY_MODEL = os.getenv("RANKING_AI_MODEL", "gemini-3.5-flash") or "gemini-3.5-flash"
FALLBACK_MODELS = [ "gpt-5.4-mini", "glm-5.2", "qwen3.6-27b", "grok-4.5"]
AI_BATCH_SIZE = 10  # 每组最多老师数：一次调用多位，避免单次输出超长截断


def extract_score(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r'综合评分\s*(\d+\.?\d*)', text)
    return float(m.group(1)) if m else 0.0


def extract_teacher_name(text: str) -> str:
    if not text:
        return "未知"
    m = re.search(r'👧#(\w+)', text)
    return m.group(1) if m else "未知"


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
    stop = {'苏州', '硬了么'}
    return [t for t in tags if t not in stop][:8]


def extract_report_score(report_text: str) -> float:
    """从用户报告提取整场综合评分。"""
    m = re.search(r'整场综合评分[：:]\s*(\d+\.?\d*)', report_text or '')
    return float(m.group(1)) if m else 0.0


def fmt_score(v) -> str:
    """9.0 -> 9、9.55 -> 9.6，避免多余小数。"""
    if not v:
        return "-"
    return f"{float(v):.1f}".rstrip('0').rstrip('.')


def extract_quote(comment_text: str) -> str:
    """从报告里提取一句可引用的话（个人评价总结/优点），用于 AI 失败回退。"""
    t = comment_text or ''
    for pat in [r'【个人评价总结】([^【\n]+)', r'【优点】([^【]+)', r'【整体评价】([^【\n]+)']:
        m = re.search(pat, t)
        if m:
            q = m.group(1).strip()
            if q and q not in ('无', '无。', '暂无'):
                return q[:MAX_QUOTE_LEN]
    return ""


def post_excerpt(text: str, max_len: int = 60) -> str:
    """帖子原文摘要（保留价格/区域等参考信息）。"""
    if not text:
        return ""
    t = re.sub(r'#\w+', '', text)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:max_len]


def rank_label(rank: int) -> str:
    medal = RANK_EMOJI.get(rank)
    return f"{medal} 第{rank}名" if medal else f"第{rank}名"


def build_overview(top) -> str:
    """全榜总览：一行一位，一目了然。"""
    lines = [f"📋 全榜总览（{len(top)}位，按综合评分排名）"]
    for i, item in enumerate(top, 1):
        ts = item['post']['created_at'].strftime('%m-%d') if item['post'].get('created_at') else '??'
        valid_count = item.get('valid_report_count', item['comment_count'])
        calc_score = item.get('comment_calculated_score', 0)
        calc_str = f"  📈评论评{fmt_score(calc_score)}" if calc_score > 0 else ""
        lines.append(
            f"{RANK_EMOJI.get(i, f'{i}.')} {item['teacher']}  "
            f"⭐{fmt_score(item['score'])}  💬{item['comment_count']}份(有效{valid_count}份)  "
            f"📊均{fmt_score(item['avg_report_score'])}{calc_str}  📅{ts}"
        )
    return "\n".join(lines)


def summarize_report(report_text: str, max_len: int = 90) -> str:
    """压缩单条报告：保留关键字段，用于喂给 AI 的上下文。"""
    if not report_text:
        return ""
    t = report_text.strip()
    key_parts = []
    for pat in [r'【过程描述】(.{0,60})', r'【优点】([^【]{0,50})', r'【建议/缺点】([^【]{0,40})', r'【个人评价总结】([^【\n]{0,40})']:
        m = re.search(pat, t)
        if m and m.group(1).strip():
            key_parts.append(m.group(1).strip())
    if key_parts:
        return " | ".join(key_parts)[:max_len]
    return t[:max_len]


def clean_ai_text(s: str) -> str:
    """清理 AI 输出中的杂散空白（中文内容不应有空格，含零宽字符）。"""
    if not s:
        return ""
    # 普通空白 + 全角空格 + 零宽空格/连接符等
    s = re.sub(r'[\s\u3000\u200b\u200c\u200d\ufeff\u00a0]+', '', s)
    return s.strip()


def clean_ai_content(obj: dict) -> dict:
    """统一清理 AI 返回的字段。"""
    tags = []
    for t in (obj.get("tags") or []):
        t = clean_ai_text(str(t))
        if t and t not in tags:
            tags.append(t)
    quotes = []
    for q in (obj.get("quotes") or []):
        q = clean_ai_text(str(q))
        if q and q not in quotes:
            quotes.append(q)
    return {
        "tags": tags,
        "summary": clean_ai_text(obj.get("summary") or ""),
        "quotes": quotes,
    }


# ── AI 内容生成（JSON 结构化，不输出排名） ──

def parse_ai_json(text: str) -> list:
    """从模型输出中提取 JSON 数组，容忍 markdown 围栏和前后缀文字。"""
    if not text:
        print("    ⚠️ parse_ai_json: 输入为空", flush=True)
        return []
    
    # 去掉 ```json ... ``` 围栏
    t = re.sub(r'```(?:json)?', '', text).strip()
    print(f"    📝 parse_ai_json: 清洗后长度={len(t)} 字符，前100字: {t[:100]!r}", flush=True)
    
    # 直接尝试整体解析
    try:
        data = json.loads(t)
        if isinstance(data, list):
            print(f"    ✅ parse_ai_json: 整体解析成功，共 {len(data)} 条", flush=True)
            return data
        else:
            print(f"    ⚠️ parse_ai_json: 整体解析结果不是 list，类型为 {type(data).__name__}", flush=True)
    except Exception as e:
        print(f"    ⚠️ parse_ai_json: 整体解析失败: {str(e)[:150]}", flush=True)
    
    # 提取第一个 [...] 块
    m = re.search(r'\[.*\]', t, re.S)
    if m:
        print(f"    📝 parse_ai_json: 匹配到 [...] 块，长度={len(m.group(0))}", flush=True)
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                print(f"    ✅ parse_ai_json: 块解析成功，共 {len(data)} 条", flush=True)
                return data
            else:
                print(f"    ⚠️ parse_ai_json: 块解析结果不是 list，类型为 {type(data).__name__}", flush=True)
        except Exception as e:
            print(f"    ⚠️ parse_ai_json: 块解析失败: {str(e)[:150]}", flush=True)
    else:
        print("    ⚠️ parse_ai_json: 未匹配到 [...] 块", flush=True)
    
    return []


def build_ai_context(top) -> str:
    """一次性构造全部老师的精简上下文（每位 1 条帖子摘要 + 最多 6 条精选评论）。"""
    parts = []
    for i, item in enumerate(top, 1):
        post = item['post']
        text = post['message_text'] or ""
        ts = post['created_at'].strftime('%Y-%m-%d') if post.get('created_at') else ''
        ctx = (
            f"【第{i}位】\n"
            f"名字：{item['teacher']}（帖子时间 {ts}）\n"
            f"综合评分：{item['score']}/10，报告数：{item['comment_count']}\n"
            f"帖子内容：{text[:150]}\n"
            f"用户评价抽样（[分数] 摘要）：\n"
        )
        for sc, ctext in item['comments'][:6]:
            summ = summarize_report(ctext)
            if summ:
                ctx += f"  - [{fmt_score(sc)}分] {summ}\n"
        parts.append(ctx.rstrip())
    return "\n\n".join(parts)


def build_ai_prompt(batch) -> str:
    ctx = build_ai_context(batch)
    return f"""你是 Telegram 频道「苏州硬了么」的资深分析师，擅长从用户评价报告中提炼老师特点。

下面是一组高分老师的数据（共 {len(batch)} 位，已按综合评分从高到低排序）：

=== 数据 ===
{ctx}

=== 任务 ===
针对每一位老师，提炼：标签（3-5个，如：服务好、身材棒、性价比高、颜值高、温柔、会聊天、回头客多、真实可靠）、
总结（2-3句，归纳核心特点：颜值/身材/服务/性格/性价比）、
精选评价（从提供的用户评价中选1-2条最有说服力的原话，每条不超过50字，必须忠于原文）。

=== 输出格式 ===
只输出一个 JSON 数组（不要输出任何其他文字、注释或 Markdown 围栏），
数组顺序必须与数据中老师的顺序一一对应，每位老师一个对象：

[
  {{"name": "老师名", "tags": ["标签1", "标签2", "标签3"], "summary": "总结", "quotes": ["原话1", "原话2"]}},
  ...
]

要求：严格基于提供的数据，禁止编造；name 必须与数据中的名字完全一致。"""


async def generate_ai_content(gemini, top) -> dict:
    """调用 AI 生成全部老师的内容（分块调用，块内一次处理多位）。
    返回 {name: {"tags": [...], "summary": str, "quotes": [...]}}。
    失败回退本地数据。"""
    if not top or gemini is None:
        print("  ⚠️ AI 内容为空或 gemini 未初始化，跳过", flush=True)
        return {}

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("  ⚠️ 无 OPENAI_API_KEY，跳过 AI 内容生成", flush=True)
        return {}

    models = [PRIMARY_MODEL] + [m for m in FALLBACK_MODELS if m != PRIMARY_MODEL]
    result = {}
    
    print(f"  📋 模型优先级队列: {models}", flush=True)
    print(f"  📋 共 {len(top)} 位老师，分 {(len(top) + AI_BATCH_SIZE - 1) // AI_BATCH_SIZE} 批处理", flush=True)

    for start in range(0, len(top), AI_BATCH_SIZE):
        batch = top[start:start + AI_BATCH_SIZE]
        names = "、".join(x['teacher'] for x in batch)
        prompt = build_ai_prompt(batch)
        print(f"\n  🔄 第 {start // AI_BATCH_SIZE + 1} 批: [{names}] ({len(batch)} 位老师)", flush=True)
        print(f"    📏 Prompt 长度: {len(prompt)} 字符", flush=True)

        ok = False
        for model in models:
            print(f"    🤖 尝试模型: {model}", flush=True)
            try:
                svc = GeminiService(api_key, model=model)
                text = await svc._chat(prompt, max_tokens=6000)
                print(f"    📥 收到响应: {len(text)} 字符，前200字: {text[:200]!r}", flush=True)
                
                data = parse_ai_json(text)
                if not data:
                    print(f"    ⚠️ AI({model}) 输出无法解析为有效 JSON，尝试下一模型", flush=True)
                    continue
                
                print(f"    📊 解析得到 {len(data)} 位老师数据，批次预期 {len(batch)} 位", flush=True)
                matched = 0
                for obj in data:
                    if isinstance(obj, dict) and obj.get("name"):
                        clean_obj = clean_ai_content(obj)
                        result[str(obj["name"]).strip()] = clean_obj
                        matched += 1
                        print(f"      ✅ {obj['name']}: tags={len(clean_obj['tags'])}个, summary={len(clean_obj['summary'])}字, quotes={len(clean_obj['quotes'])}条", flush=True)
                
                if matched == 0:
                    print(f"    ⚠️ 解析成功但无有效 name 字段，尝试下一模型", flush=True)
                    continue
                    
                print(f"  ✅ AI({model}) 处理 [{names}] 成功 ({matched}/{len(batch)} 位匹配)", flush=True)
                ok = True
                break
            except Exception as e:
                print(f"    ❌ AI({model}) 异常: {type(e).__name__}: {str(e)[:200]}", flush=True)
                await asyncio.sleep(2)
        
        if not ok:
            print(f"  ❌ 分组 [{names}] 全部模型失败，将使用本地数据回退", flush=True)
            # 打印回退信息
            for item in batch:
                local_tags = item.get('tags', [])
                local_quotes = item.get('best_quotes', [])
                print(f"    🔙 回退 [{item['teacher']}]: 本地标签={len(local_tags)}个, 本地高分摘录={len(local_quotes)}条", flush=True)
        
        await asyncio.sleep(2)  # 避免 TPM 限制

    print(f"\n  📦 AI 内容汇总: 成功 {len(result)}/{len(top)} 位老师", flush=True)
    if len(result) < len(top):
        missing = [item['teacher'] for item in top if item['teacher'] not in result]
        print(f"    ⚠️ 未覆盖的老师: {', '.join(missing)}", flush=True)
    return result


# ── 报告组装 ──

def build_teacher_block(rank: int, item, ai=None) -> str:
    """单个老师详情块：分组式排版，信息丰富、可读性高。ai 为可选 AI 内容 dict。"""
    post = item['post']
    text = post['message_text'] or ""
    msg_id = post.get('message_id', '')
    ts = post['created_at'].strftime('%Y-%m-%d') if post.get('created_at') else '未知'
    link = f"https://t.me/SZnewls/{msg_id}" if msg_id else "无链接"
    ai = ai or {}

    valid_count = item.get('valid_report_count', item['comment_count'])
    calc_score = item.get('comment_calculated_score', 0)
    calc_str = f" ｜ 📈 评论评 {fmt_score(calc_score)}" if calc_score > 0 else ""

    # ── 标题区 ──
    lines = [SEP, f"  {rank_label(rank)} ｜ {item['teacher']}", SEP, ""]

    # ── 评分区 ──
    lines.append(f"⭐ 综合评分：{fmt_score(item['score'])} / 10")
    lines.append(f"💬 报告 {item['comment_count']} 份（有效 {valid_count} 份）")
    lines.append(f"📊 用户均分：{fmt_score(item['avg_report_score'])}{calc_str}")
    lines.append(f"📅 发帖：{ts}")
    lines.append("")

    # ── 分项评分区 ──
    detail = item['detail_scores']
    if detail:
        lines.append("� 分项评分")
        detail_items = [f"{k} {fmt_score(v)}" for k, v in detail.items()]
        for i in range(0, len(detail_items), 3):
            lines.append("   " + " ｜ ".join(detail_items[i:i + 3]))
        lines.append("")

    # ── 标签区 ──
    tags = ai.get('tags') or item['tags']
    if tags:
        lines.append(f"🏷 标签：{' · '.join(tags[:6])}")
    else:
        print(f"    ⚠️ [{item['teacher']}] 标签缺失: AI无 + 本地无", flush=True)

    # ── 帖子摘要区 ──
    excerpt = post_excerpt(text)
    if excerpt:
        lines.append(f"📄 帖子：{excerpt}")
        lines.append("")

    # ── AI 总结区 ──
    summary = ai.get('summary', '').strip()
    if summary:
        lines.append(f"📝 总结：{summary}")
        lines.append("")
    else:
        print(f"    ⚠️ [{item['teacher']}] 总结缺失: AI总结为空", flush=True)

    # ── 用户评价区 ──
    quotes = ai.get('quotes') or []
    if quotes:
        lines.append("💬 用户评价")
        for q in quotes[:2]:
            lines.append(f"   “{q[:MAX_QUOTE_LEN]}”")
    else:
        bq = item.get('best_quotes', [])
        if bq:
            sc, q = bq[0]
            lines.append(f"💬 用户评价：“{q}”（{fmt_score(sc)}分）")
            print(f"    🔙 [{item['teacher']}] 评价回退到本地摘录", flush=True)
        else:
            print(f"    ⚠️ [{item['teacher']}] 评价缺失: AI无 + 本地无", flush=True)

    # ── 低分提醒 ──
    warnings = item.get('low_warnings', [])
    if warnings:
        sc, q = warnings[0]
        lines.append(f"⚠️ 低分提醒：“{q}”（{fmt_score(sc)}分）")

    lines.append("")
    lines.append(f"🔗 原帖：{link}")
    return "\n".join(lines)


async def _clear_history(collector, batch_size: int = 50):
    """直接清空收藏夹中的所有消息。"""
    from telethon.tl.types import InputMessagesFilterEmpty
    
    entity = await collector.client.get_entity("me")
    deleted = 0
    
    try:
        async for msg in collector.client.iter_messages(
            entity, 
            limit=None,  # 检查全部
            filter=InputMessagesFilterEmpty()
        ):
            await msg.delete()
            deleted += 1
            await asyncio.sleep(0.1)
            if deleted % batch_size == 0:
                print(f"  🗑️ 已删除 {deleted} 条消息...", flush=True)
    except Exception as e:
        print(f"  ⚠️ 清空收藏夹时出错: {e}", flush=True)
    
    if deleted > 0:
        print(f"  🗑️ 已清空收藏夹，共删除 {deleted} 条消息", flush=True)


async def generate_and_send(db, collector, gemini=None, channel_id: int = CHANNEL_ID):
    """核心函数：生成榜单报告并发送到收藏夹。供 main 调度器和独立脚本复用。"""
    # 先清除历史收藏消息
    await _clear_history(collector)
    async with db.pool.acquire() as conn:
        # 按老师去重：每位老师取综合评分最高的一条帖子（含其所有评论数）
        rows = await conn.fetch("""
            SELECT t.*
            FROM (
                SELECT p.id, p.message_id, p.message_text, p.created_at,
                       p.teacher_name,
                       p.comment_calculated_score,
                       COUNT(c.id) AS comment_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY p.teacher_name
                           ORDER BY p.created_at DESC
                       ) AS rn
                FROM messages p
                LEFT JOIN messages c ON c.parent_message_id = p.id AND c.message_type = 'comment'
                WHERE p.message_type = 'post' AND p.chat_id = $1
                  AND p.message_text LIKE '%综合评分%'
                GROUP BY p.id, p.teacher_name
            ) t
            WHERE t.rn = 1 AND t.comment_count >= $2
            ORDER BY t.created_at DESC
        """, channel_id, MIN_COMMENTS)

    candidates = []
    for r in rows:
        text = r['message_text'] or ""
        score = extract_score(text)
        if score >= MIN_SCORE:
            teacher = r['teacher_name'] or extract_teacher_name(text) or "未知"
            candidates.append({
                'post': dict(r),
                'score': score,
                'teacher': teacher,
                'detail_scores': extract_detail_scores(text),
                'tags': extract_tags(text),
                'comment_count': r['comment_count'],
                'comment_calculated_score': r['comment_calculated_score'] or 0,
            })

    if not candidates:
        print(f"没有找到 评分>={MIN_SCORE} 且 评论>={MIN_COMMENTS} 的帖子")
        return None

    # 排名：综合评分优先，同分按报告数（代码统一排序，AI 不参与）
    candidates.sort(key=lambda x: (-x['score'], -x['comment_count']))
    top = candidates[:TOP_N]

    print(f"🎯 候选 {len(candidates)} 位，取前 {len(top)} 名", flush=True)

    # 为每位老师加载评论，计算均分 + 摘录 + 采样
    for item in top:
        async with db.pool.acquire() as conn:
            comments = await conn.fetch("""
                SELECT message_text, created_at FROM messages
                WHERE parent_message_id = $1 AND message_type = 'comment'
                ORDER BY created_at DESC
            """, item['post']['id'])
        comments = [dict(c) for c in comments]

        scored = [(extract_report_score(c['message_text']), c['message_text']) for c in comments]
        scored.sort(key=lambda x: x[0], reverse=True)

        # 均分：只统计有效评分（>0），排除无评分评论
        valid_scores = [s for s, _ in scored if s > 0]
        item['avg_report_score'] = sum(valid_scores) / len(valid_scores) if valid_scores else 0
        item['valid_report_count'] = len(valid_scores)  # 有效评分数量

        # 采样：高分 + 低分 + 中间随机，供 AI 上下文
        sample = scored[:5] + scored[-2:]
        item['comments'] = sample

        # 本地回退用：高分摘录 + 低分提醒
        quotes = []
        for sc, t in scored:
            q = extract_quote(t)
            if q and not any(q in existing for _, existing in quotes):
                quotes.append((sc, q))
            if len(quotes) >= 2:
                break
        item['best_quotes'] = quotes

        lows = [(s, t) for s, t in scored if 0 < s < 8]
        warnings = []
        for sc, t in lows:
            q = extract_quote(t)
            if q and not any(q in existing for _, existing in warnings):
                warnings.append((sc, q))
            if len(warnings) >= 2:
                break
        item['low_warnings'] = warnings

        calc_score = item.get('comment_calculated_score', 0)
        calc_str = f"  📈评论评{fmt_score(calc_score)}" if calc_score > 0 else ""
        print(
            f"  {item['teacher']}: score={item['score']} 报告={item['comment_count']} "
            f"均分={item['avg_report_score']:.2f} (有效评分{item['valid_report_count']}条){calc_str}",
            flush=True,
        )

    # ── AI 内容增强（一次调用，JSON 结构化） ──
    print("🤖 生成 AI 内容（标签/总结/评价）...", flush=True)
    ai_content = await generate_ai_content(gemini, top)
    
    print("\n" + "="*50, flush=True)
    print("📊 AI 覆盖情况检查:", flush=True)
    for item in top:
        name = item['teacher']
        ai = ai_content.get(name)
        if ai:
            has_tags = bool(ai.get('tags'))
            has_summary = bool(ai.get('summary'))
            has_quotes = bool(ai.get('quotes'))
            status = "✅" if (has_tags and has_summary and has_quotes) else "⚠️"
            print(f"  {status} {name}: tags={'✓' if has_tags else '✗'} summary={'✓' if has_summary else '✗'} quotes={'✓' if has_quotes else '✗'}", flush=True)
        else:
            print(f"  ❌ {name}: 无 AI 数据，将全部使用本地回退", flush=True)
    print("="*50 + "\n", flush=True)
    
    if not ai_content:
        print("  ⚠️ AI 内容不可用，使用本地数据回退", flush=True)

    # ── 组装每个老师的详细报告（不再发送统一榜单文本）──
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    header = (
        f"🏆 苏州硬了么高分老师 TOP {len(top)}\n"
        f"📊 筛选：综合评分≥{MIN_SCORE} · 报告≥{MIN_COMMENTS}条 · 达标{len(candidates)}位\n"
        f"📅 生成时间：{now}\n\n"
    )
    blocks = [build_teacher_block(i, item, ai_content.get(item['teacher'])) for i, item in enumerate(top, 1)]
    footer = f"\n{SEP}\n📊 数据统计：上榜 {len(top)} 位 ｜ 最高 {fmt_score(top[0]['score'])} 分 ｜ 最低 {fmt_score(top[-1]['score'])} 分"

    report = header + "\n".join(blocks) + footer

    # 每位老师：原帖图片 + 详细报告
    print("📸 发送前 10 名（图片 + 详细报告）...", flush=True)
    entity = await collector.client.get_entity("me")
    for i, item in enumerate(top[:10], 1):
        msg_id = item['post'].get('message_id')
        detail_text = blocks[i - 1]
        try:
            if msg_id:
                msg = await collector.client.get_messages(channel_id, ids=msg_id)
                if msg and msg.media:
                    await collector.client.send_file(entity, msg.media, caption=detail_text[:1000])
                    await asyncio.sleep(2)
                    continue
            # 无图片则直接发送详细报告文本
            await collector.client.send_message(entity, detail_text)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"   发送 {item['teacher']} 失败: {e}，改为文本发送", flush=True)
            try:
                await collector.client.send_message(entity, detail_text)
                await asyncio.sleep(1)
            except Exception as e2:
                print(f"   文本发送也失败: {e2}", flush=True)

    # 剩余老师链接
    remaining = top[10:]
    if remaining:
        lines = ["📋 第 11-20 名原帖链接：", ""]
        for i, item in enumerate(remaining, 11):
            msg_id = item['post'].get('message_id')
            lines.append(f"{i}. {item['teacher']}（⭐{fmt_score(item['score'])}，💬{item['comment_count']}份）→ https://t.me/SZnewls/{msg_id}")
        links = "\n".join(lines)
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

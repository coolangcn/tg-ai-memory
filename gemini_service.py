"""
AI 服务：通过 OpenAI 兼容接口（自建 new-api 中转）调用 Gemini 模型
生成每日报告、每日精华、回答历史问题。
"""
import logging
import os
from typing import List, Dict, Optional
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class GeminiService:
    def __init__(self, api_key: str, base_url: str = "", model: str = ""):
        self.api_key = api_key
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.moco.fun/v1")
        self.model = model or os.getenv("OPENAI_MODEL", "gemini-3.5-flash")
        self.client = AsyncOpenAI(api_key=api_key, base_url=self.base_url)

    async def _chat(self, prompt: str, max_tokens: int = 4000) -> str:
        """调用 OpenAI 兼容接口，返回文本。"""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个专业的 Telegram 内容分析师。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    def _format_messages_for_context(self, messages: List[Dict]) -> str:
        """Format messages into a readable context string."""
        if not messages:
            return "No messages available."

        formatted = []
        for msg in messages:
            timestamp = msg['created_at'].strftime('%Y-%m-%d %H:%M')
            user = msg.get('user_name') or msg.get('user_id') or '未知'
            text = msg['message_text']
            chat = msg.get('chat_title')
            if chat:
                formatted.append(f"[{timestamp}] {chat} | {user}: {text}")
            else:
                formatted.append(f"[{timestamp}] {user}: {text}")

        return "\n".join(formatted)

    def _detect_language(self, messages: List[Dict]) -> str:
        """Detect the primary language used in messages."""
        if not messages:
            return "English"

        # Sample last 50 messages to detect language
        sample_messages = messages[-50:] if len(messages) > 50 else messages
        sample_text = " ".join([msg['message_text'] for msg in sample_messages])

        # Simple heuristic: check for Cyrillic characters (Russian, Ukrainian, etc.)
        cyrillic_count = sum(1 for c in sample_text if '\u0400' <= c <= '\u04FF')
        cjk_count = sum(1 for c in sample_text if '\u4E00' <= c <= '\u9FFF')
        total_letters = sum(1 for c in sample_text if c.isalpha())

        if total_letters > 0 and (cyrillic_count / total_letters) > 0.3:
            return "Russian"  # or the language of the messages

        if total_letters > 0 and (cjk_count / total_letters) > 0.3:
            return "Chinese"

        # Could add more language detection here
        return "English"

    def _chunk_messages(self, messages: List[Dict], max_chars: int = 30000) -> List[str]:
        """Split messages into chunks to avoid token limits."""
        chunks = []
        current_chunk = []
        current_length = 0

        for msg in messages:
            msg_str = f"[{msg['created_at']}] {msg['user_name']}: {msg['message_text']}\n"
            msg_length = len(msg_str)

            if current_length + msg_length > max_chars and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0

            current_chunk.append(msg_str)
            current_length += msg_length

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks

    async def _summarize_chunks(self, messages: List[Dict]) -> str:
        """当消息过多时，先分块让模型归纳，再合并。"""
        chunks = self._chunk_messages(messages, max_chars=30000)
        logger.info(f"Large context detected, processing {len(chunks)} chunks")
        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            try:
                resp = await self._chat(
                    f"用中文简要归纳以下Telegram消息的要点，保留具体名称、数字、价格等关键信息：\n\n{chunk}"
                )
                chunk_summaries.append(resp)
            except Exception as e:
                logger.error(f"Error processing chunk {i}: {e}")
        return "\n\n".join(chunk_summaries)

    async def generate_daily_report(self, messages: List[Dict]) -> str:
        """
        Generate a comprehensive daily analytics report.
        Returns markdown-formatted report with topics, stats, and insights.
        """
        if not messages:
            return "📊 **Daily Report**\n\nNo messages were recorded in the last 24 hours."

        # Detect language from messages
        language = self._detect_language(messages)
        logger.info(f"Detected language: {language}")

        context = self._format_messages_for_context(messages)

        # If context is too large, summarize in chunks
        if len(context) > 30000:
            context = await self._summarize_chunks(messages)

        prompt = f"""You are analyzing Telegram group chat messages from the last 24 hours. Generate a MAXIMALLY DETAILED professional business report.

Messages:
{context}

IMPORTANT: Respond in {language}. Analyze the language used in the messages and reply in THE SAME LANGUAGE.

Generate a COMPREHENSIVE, DETAILED BUSINESS REPORT in Markdown format. Include ALL significant information:

## 📊 DETAILED ACTIVITY STATISTICS
- Total message count (exact number)
- Number of active participants (full list)
- TOP-5 most active participants with exact message counts
- Peak activity hours (specific time ranges)
- Average message length (in words)
- Longest and shortest messages
- Activity distribution by hours (if possible)

## 🎯 COMPREHENSIVE TOPICS & PROJECTS ANALYSIS
- Group ALL messages by work topics/projects
- For EACH topic specify:
  * Message count
  * Key participants in discussion
  * Main points and conclusions
  * Status of discussion
- Highlight priority tasks with full details
- Note completed tasks and who completed them
- Incomplete tasks and reasons

## 👥 FULL PARTICIPANT ANALYSIS
- COMPLETE list of ALL participants with message counts (sorted descending)
- For each active participant:
  * Number of messages
  * Main topics they discussed
  * Their role (initiator, executor, commentator)
- New participants (if any)
- Inactive participants (who stayed silent)
- Activity time for each participant

## 📈 KEY DECISIONS AND RESULTS
- ALL decisions made with full descriptions
- ALL assigned tasks specifying:
  * Who assigned
  * Assigned to whom
  * Deadlines
  * Current status
- Deadlines and important dates
- Achieved results (detailed)
- All problems and their solutions (detailed)
- Open questions requiring resolution

## 💬 DETAILED COMMUNICATION ANALYSIS
- Communication style (formal/informal)
- Discussion tone (positive/negative/neutral)
- Team engagement level
- Response speed to messages
- Feedback quality
- Conflicts or disagreements (if any)

## 🔍 DEEP INSIGHTS AND PATTERNS
- Recurring themes or questions
- Discussion trends
- Participant activity patterns
- Communication effectiveness (what works, what doesn't)
- Process bottlenecks and issues
- Improvement opportunities

## ⚡ IMPORTANT MOMENTS & HIGHLIGHTS
- Critically important messages or decisions
- Urgent matters requiring attention
- Risks and warnings
- Opportunities not to be missed

## 📋 PLANS AND NEXT STEPS
- All scheduled meetings with details (time, participants, purpose)
- All upcoming tasks with priorities
- Important dates and events
- Specific recommendations for tomorrow
- Action items for each participant

## 📝 EXECUTIVE SUMMARY
- Main achievements of the day
- Key problems
- Key takeaways
- Overall project progress

Be MAXIMALLY detailed and specific! Extract ALL valuable information. Don't skip details. Use {language} for all text.

FORMATTING RULES:
- Use simple Markdown formatting only
- Use **bold** for headers and important text
- Use | for tables (keep tables simple)
- Avoid underscores in emphasis, use asterisks instead
- Keep formatting safe for Telegram parsing
- Maximum 4000 characters"""

        try:
            return await self._chat(prompt)
        except Exception as e:
            logger.error(f"AI API error in daily report: {e}")
            return f"❌ Failed to generate report: {str(e)}"

    async def generate_daily_summary(self, messages: List[Dict], source_names: str = "") -> str:
        """
        Generate a daily digest (每日精华) for channel/group content.
        Messages should already be filtered to today's content.
        """
        if not messages:
            return "今日暂无消息。"

        context = self._format_messages_for_context(messages)

        # If context is too large, summarize in chunks first
        if len(context) > 30000:
            context = await self._summarize_chunks(messages)

        prompt = f"""你是Telegram频道内容分析师。下面是今日从多个Telegram频道/群组收集的全部消息，请生成一份【每日精华总结】，全部用中文回复。

消息来源：{source_names or '多个频道/群组'}

今日消息：
{context}

请输出以下内容（简洁精炼，总字数控制在1500字以内，只保留有信息量的内容）：

## 📌 今日概览
- 今日消息总数
- 各来源渠道的消息数量概况

## ✨ 今日精华（按重要程度排序）
- 逐条列出今日最值得关注的内容（3-10条），每条注明：来源频道/群、发布时间、内容要点
- 同一话题的多条消息可以合并成一条精华

## 🔥 热点话题
- 今日发布最密集或最受关注的几个主题

## 🎯 值得关注
- 新店/新产品/价格变动/新活动/重要通知等信息量大的内容
- 需要重点留意的信息

## 📝 一句话总结
用一句话概括今天所有频道/群的主要内容。

要求：
- 只总结消息中真实存在的信息，禁止编造
- 保留具体名称、数字、价格、联系方式等关键信息
- 格式简单清晰，适合Telegram阅读"""

        try:
            return await self._chat(prompt)
        except Exception as e:
            logger.error(f"AI API error in daily summary: {e}")
            return f"❌ 每日精华生成失败：{str(e)}"

    async def answer_question(self, question: str, messages: List[Dict]) -> str:
        """
        Answer a user question based on message history context.
        """
        if not messages:
            return "I don't have any message history to answer your question."

        # Detect language from messages
        language = self._detect_language(messages)
        logger.info(f"Detected language for Q&A: {language}")

        context = self._format_messages_for_context(messages)

        # Handle large contexts
        if len(context) > 40000:
            # Use only recent messages for context
            context = self._format_messages_for_context(messages[-500:])
            context = f"[Showing last 500 messages]\n\n{context}"

        prompt = f"""You are a helpful assistant analyzing Telegram group chat history.

Chat History (last 14 days):
{context}

User Question: {question}

IMPORTANT: Analyze the language used in the chat history and the question. Respond in THE SAME LANGUAGE as the user's question and chat messages ({language}).

Provide a helpful, accurate answer based on the chat history above. If the question cannot be answered from the available context, say so politely. Keep your response concise and relevant. Use {language} for your entire response.

FORMATTING RULES:
- Use simple text formatting only
- Avoid complex Markdown syntax
- Use **bold** only for important words
- Don't use underscores, brackets, or special characters
- Keep formatting simple and safe for Telegram"""

        try:
            return await self._chat(prompt)
        except Exception as e:
            logger.error(f"AI API error in Q&A: {e}")
            return f"❌ 回答问题时出错：{str(e)}"

    def _format_teacher_data(self, posts_with_comments: List[Dict]) -> str:
        """把帖子+评论数据格式化成模型可读的文本。"""
        lines = []
        for i, item in enumerate(posts_with_comments, 1):
            post = item["post"]
            comments = item["comments"]
            ts = post['created_at'].strftime('%Y-%m-%d %H:%M') if post.get('created_at') else ''
            lines.append(f"【帖子 {i}】{ts}")
            lines.append(post.get('message_text', ''))
            if comments:
                lines.append(f"  ── 评价({len(comments)}条) ──")
                for c in comments:
                    cts = c['created_at'].strftime('%m-%d %H:%M') if c.get('created_at') else ''
                    lines.append(f"  · {cts} {c.get('message_text', '')}")
            lines.append("")
        return "\n".join(lines)

    def _is_spa_ad(self, text: str) -> bool:
        """判断是否是 SPA/店铺广告（应排除）。"""
        if not text:
            return False
        text_lower = text.lower()
        spa_keywords = [
            "养生SPA", "SPA", "会所", "本店", "到店", "进店", "门店",
            "开业大酬宾", "开业优惠", "充值", "会员卡", "套餐价",
            "地址：", "地址:", "导航", "停车场", "营业时间",
            "客服微信", "前台", "预约电话", "到店消费",
            "spa", "店铺", "商家", "促销", "打折", "优惠",
            "光临", "惠顾", "连锁", "品牌",
        ]
        count = sum(1 for k in spa_keywords if k in text)
        return count >= 2

    async def generate_essence_report(
        self,
        posts_with_comments: List[Dict],
        link_fn,
        date_str: str = "",
        source_names: str = "",
    ) -> str:
        """
        从历史数据中找出综合评分 > 9 分的老师，归纳总结，附原始帖子链接。
        输入：[{"post": {...}, "comments": [{...}]}], link_fn(post) -> url
        """
        filtered = [
            item for item in posts_with_comments
            if not self._is_spa_ad(item["post"].get("message_text", ""))
        ]
        if not filtered:
            return "📊 高分老师精华\n\n历史数据中无有效老师帖子。"

        lines = []
        for i, item in enumerate(filtered, 1):
            post = item["post"]
            comments = item["comments"]
            ts = post['created_at'].strftime('%Y-%m-%d %H:%M') if post.get('created_at') else ''
            url = link_fn(post) if link_fn else ''
            lines.append(f"【老师帖子 {i}】{ts} 链接:{url}")
            lines.append(post.get('message_text', ''))
            if comments:
                lines.append(f"  ── 评价({len(comments)}条) ──")
                for c in comments:
                    cts = c['created_at'].strftime('%m-%d %H:%M') if c.get('created_at') else ''
                    lines.append(f"  · {cts} {c.get('message_text', '')}")
            lines.append("")
        context = "\n".join(lines)

        total_posts = len(filtered)
        total_comments = sum(len(item["comments"]) for item in filtered)

        prompt = f"""你是 Telegram 频道「苏州硬了么认证老师榜」的资深分析师。下面是频道历史所有老师帖子及其用户评价。

数据来源：{source_names or '认证老师榜'}
帖子总数：{total_posts} 个
评价总数：{total_comments} 条

=== 帖子与评价 ===
{context}

=== 任务 ===
1. 排除 SPA/店铺广告帖子（如仍混入请忽略）
2. 对每位老师独立评分（0-10 分），评分标准：
   - 评价数量与质量（有真实用户好评）
   - 服务/态度/外形等被称赞的维度
   - 标签完整度与价格信息
   - 严格评分：只有口碑过硬、好评占比高、无明显负面评价的老师才能到 9 分以上
   - 没有评价或评价极少的新老师，最多给 6-7 分
3. 只输出**综合评分 > 9 分**的老师（严格筛选，宁缺毋滥）
4. 每位高分老师包含：
   - 老师名/称呼、区域、标签、价格
   - 评分：X/10
   - 精华总结（结合评价归纳特点、优势）
   - 用户好评摘录（引用 1-2 条原话，短）
   - 原始帖子链接（必须用提供的链接）

=== 输出格式 ===

## 🏆 高分老师精华榜（历史综合评分 > 9 分）

### 第1名：[称呼/区域] ⭐ 评分 9.5/10
- **标签/价格**：...
- **精华总结**：...
- **用户评价**："..." / "..."
- **帖子链接**：https://...

（全部高分老师依次列出）

## 📝 总结
- 共 X 位高分老师（占总数 Y%）
- 高分老师共同特点：...

如果没有任何老师达到 9 分以上，直接输出：
## 🏆 高分老师精华榜
暂无老师达到综合评分 9 分以上。最高分为 X/10（[称呼]）。"""

        try:
            return await self._chat(prompt, max_tokens=8000)
        except Exception as e:
            logger.error(f"AI API error in essence report: {e}")
            return f"❌ 高分老师精华生成失败：{str(e)}"

    async def generate_teacher_report(
        self,
        posts_with_comments: List[Dict],
        date_str: str = "",
        source_names: str = "",
    ) -> str:
        """
        生成「老师分析与评分报告」。
        输入：[{"post": {...}, "comments": [{...}]}, ...]
        """
        # 过滤掉 SPA/店铺广告
        filtered = [
            item for item in posts_with_comments
            if not self._is_spa_ad(item["post"].get("message_text", ""))
        ]
        if not filtered:
            return f"📊 老师分析报告 ({date_str})\n\n今日无广告过滤后无有效老师帖子。"

        total_comments = sum(len(item["comments"]) for item in filtered)
        teacher_count = len(filtered)

        context = self._format_teacher_data(filtered)

        prompt = f"""你是 Telegram 频道「苏州硬了么认证老师榜」的专业分析师。下面是从频道采集到的老师帖子及其下方的用户评价，请生成一份详细的【老师分析与评分报告】，全部用中文。

数据来源：{source_names or '认证老师榜'}
日期：{date_str or '今日'}
老师数量：{teacher_count} 位
评价总数：{total_comments} 条

=== 帖子与评价 ===
{context}

=== 分析要求 ===

1. **排除 SPA/店铺广告**（已预处理，但如仍发现请忽略）
2. 对每位老师进行详细分析：
   - 基本信息：区域、标签、价格（如有）
   - 评价分析：从用户评价中提取关键词（服务质量、态度、外貌、体验等）
   - 优势亮点：大家称赞的方面（引用评价原文关键词）
   - 不足/争议：评价中提到的问题或不满
   - 综合评分：X/10（根据评价正面程度、评价数量、标签丰富度综合评定）
   - 评价热度：X 条评价
   - 推荐指数：⭐~⭐⭐⭐⭐⭐

3. 报告结构：

## 📊 今日概览
- 新发布老师数量、总评价数、平均评分
- 整体趋势（今日偏好评分分布）

## 👩‍🏫 老师详细分析
（按评分从高到低排列，每位老师独立一节）

### 第1名：[区域] [标签关键词] - ⭐⭐⭐⭐⭐ (X/10)
- **基本信息**：区域、价格、标签
- **评价分析**：
  - 好评关键词：...
  - 差/中评关键词：...（如有）
- **优势**：...
- **不足**：...（无则写"无明显负面评价"）
- **综合评价**：...
- **评价热度**：X 条

（依次列出所有老师）

## 🏆 今日推荐
- 评分最高的 1-2 位，说明推荐理由
- 评价数量最多、讨论最热的 1-2 位

## 📈 数据统计
- 各区域分布
- 热门标签出现频次
- 评分分布（优秀/良好/一般）

## 📝 一句话总结

要求：
- 严格基于帖子内容和评价，禁止编造
- 评价少的新老师注明"评价较少，仅供参考"
- 评分要有依据，不可全员高分
- 格式清晰，适合 Telegram 阅读"""

        try:
            return await self._chat(prompt, max_tokens=8000)
        except Exception as e:
            logger.error(f"AI API error in teacher report: {e}")
            return f"❌ 老师分析报告生成失败：{str(e)}"

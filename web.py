"""FastAPI Web 界面 - Telegram 频道分析平台。"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from db import Database
from collector import TelegramCollector, MEDIA_DIR
from gemini_service import GeminiService

load_dotenv()

app = FastAPI(title="Telegram 频道分析")
# 使用 Jinja2 原生 API 避免 Starlette Jinja2Templates 的崩溃
env = Environment(loader=FileSystemLoader("templates"), autoescape=True, enable_async=True)

# 全局状态
db: Optional[Database] = None
collector: Optional[TelegramCollector] = None
gemini: Optional[GeminiService] = None


async def render_template(template_name: str, **context) -> str:
    """渲染模板并返回 HTML 字符串。"""
    template = env.get_template(template_name)
    return await template.render_async(**context)


@app.on_event("startup")
async def startup():
    global db, collector, gemini
    db = Database(os.getenv("DATABASE_URL"))
    await db.connect()
    gemini = GeminiService(os.getenv("OPENAI_API_KEY"))
    # 初始化采集器（仅用于读取数据，不实时监听）
    try:
        collector = TelegramCollector(
            int(os.getenv("TELEGRAM_API_ID")),
            os.getenv("TELEGRAM_API_HASH"),
            os.getenv("TELEGRAM_PHONE"),
            db,
            os.getenv("WATCH_CHANNELS", "@SZnewls").split(","),
        )
        await collector.start()
        # 补采一次确保数据最新
        await collector.sync_recent(limit=100)
    except Exception as e:
        logger.warning(f"Collector init failed: {e}")


@app.on_event("shutdown")
async def shutdown():
    if db:
        await db.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """首页 - 统计概览。"""
    stats = await get_stats()
    html = await render_template("index.html", stats=stats)
    return HTMLResponse(html)


@app.get("/posts", response_class=HTMLResponse)
async def posts_page(request: Request, page: int = 1, limit: int = 20):
    """帖子列表页。"""
    offset = (page - 1) * limit
    chat_ids = list(collector.watched_chat_ids) if collector else []
    all_posts = []
    total = 0
    for chat_id in chat_ids:
        posts = await db.get_messages_with_media(chat_id, limit=limit, offset=offset)
        total += await db.get_post_count(chat_id)
        all_posts.extend(posts)
    all_posts.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    channel_username = os.getenv("CHANNEL_USERNAME", "SZnewls")

    html = await render_template("posts.html",
        posts=all_posts,
        page=page,
        limit=limit,
        total=total,
        channel=channel_username,
    )
    return HTMLResponse(html)


@app.get("/post/{post_id}", response_class=HTMLResponse)
async def post_detail(request: Request, post_id: int):
    """帖子详情（含评论和媒体）。"""
    async with db.pool.acquire() as conn:
        post = await conn.fetchrow("SELECT * FROM messages WHERE id = $1", post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        comments = await conn.fetch(
            "SELECT * FROM messages WHERE parent_message_id = $1 ORDER BY created_at", post_id
        )
        media = await conn.fetch("SELECT * FROM media WHERE message_id = $1 ORDER BY id", post_id)

    # 构造 Telegram 链接
    channel_username = os.getenv("CHANNEL_USERNAME", "SZnewls")
    post_link = f"https://t.me/{channel_username}/{post['message_id']}" if post['message_id'] else None

    html = await render_template("post_detail.html",
        post=dict(post),
        comments=[dict(c) for c in comments],
        media=[dict(m) for m in media],
        post_link=post_link,
        channel_username=channel_username,
    )
    return HTMLResponse(html)


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    """报告生成页面。"""
    html = await render_template("report.html", report=None)
    return HTMLResponse(html)


@app.post("/api/report")
async def generate_report(request: Request):
    """API: 生成高分老师精华报告。"""
    form = await request.form()
    min_score = float(form.get("min_score", 9))
    limit = int(form.get("limit", 50))

    chat_ids = list(collector.watched_chat_ids) if collector else []
    all_items = []
    for chat_id in chat_ids:
        items = await db.get_all_posts_with_comments(chat_id)
        all_items.extend(items)

    # 取最近 limit 个有评论的
    all_items.sort(key=lambda x: x["post"].get("created_at", ""), reverse=True)
    all_items = all_items[:limit]

    chat_username = os.getenv("CHANNEL_USERNAME", "SZnewls")

    def link_fn(post):
        mid = post.get("message_id")
        if chat_username and mid:
            return f"https://t.me/{chat_username}/{mid}"
        return ""

    report = await gemini.generate_essence_report(
        all_items,
        link_fn=link_fn,
        date_str="全历史",
        source_names="苏州硬了么认证老师榜",
    )

    return JSONResponse({"report": report})


@app.get("/api/stats")
async def api_stats():
    """API: 统计数据。"""
    return JSONResponse(await get_stats())


@app.get("/media/{file_path:path}")
async def serve_media(file_path: str):
    """提供媒体文件访问。"""
    full_path = MEDIA_DIR / file_path
    if not full_path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(full_path))


async def get_stats() -> dict:
    """获取统计信息。"""
    async with db.pool.acquire() as conn:
        total_posts = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type = 'post'")
        total_comments = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type = 'comment'")
        total_media = await conn.fetchval("SELECT COUNT(*) FROM media")
        posts_with_comments = await conn.fetchval(
            "SELECT COUNT(DISTINCT parent_message_id) FROM messages WHERE message_type = 'comment'"
        )
        posts_with_media = await conn.fetchval(
            "SELECT COUNT(DISTINCT message_id) FROM media"
        )
        earliest = await conn.fetchval("SELECT MIN(created_at) FROM messages WHERE message_type = 'post'")
        latest = await conn.fetchval("SELECT MAX(created_at) FROM messages WHERE message_type = 'post'")

    return {
        "total_posts": total_posts,
        "total_comments": total_comments,
        "total_media": total_media,
        "posts_with_comments": posts_with_comments,
        "posts_with_media": posts_with_media,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
    }


def run():
    """启动 Web 服务器。"""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

"""简化版评论补抓脚本 - 带错误处理。"""
import asyncio
import os
from datetime import timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User
from telethon.errors import MsgIdInvalidError, ChannelPrivateError, FloodWaitError

from db import Database

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL")


def build_proxy():
    import socks
    raw = os.getenv("TELEGRAM_PROXY", "").strip()
    if not raw:
        return None
    scheme, _, rest = raw.partition("://")
    host_port, _, auth = rest.partition("@")
    host, _, port = host_port.partition(":")
    port = int(port) if port else 0
    cls = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP}.get(scheme.lower())
    if auth:
        user, _, pwd = auth.partition(":")
        return (cls, host, port, True, user, pwd)
    return (cls, host, port)


async def main():
    print("🔄 启动...")
    
    # 数据库
    db = Database(DATABASE_URL)
    await db.connect()
    print("✅ 数据库")
    
    # Telegram
    client = TelegramClient("telegram", API_ID, API_HASH, proxy=build_proxy())
    await client.start(phone=PHONE, password=PASSWORD)
    print("✅ Telegram")
    
    # 频道
    entity = await client.get_entity("@SZnewls")
    raw_id = entity.id
    chat_id = int(f"-100{raw_id}") if raw_id > 0 else raw_id
    print(f"✅ 频道: {chat_id}")
    
    # 获取帖子
    async with db.pool.acquire() as conn:
        posts = await conn.fetch("""
            SELECT id, message_id FROM messages
            WHERE chat_id = $1 AND message_type = 'post' AND message_id IS NOT NULL
            ORDER BY created_at ASC
        """, chat_id)
    print(f"📊 帖子数: {len(posts)}")
    
    # 补抓评论
    total = 0
    processed = 0
    skipped = 0
    errors = 0
    
    for i, post in enumerate(posts):
        post_db_id = post['id']
        post_msg_id = post['message_id']
        
        # 检查是否已有评论
        async with db.pool.acquire() as conn:
            existing = await conn.fetchval("""
                SELECT COUNT(*) FROM messages 
                WHERE parent_message_id = $1 AND message_type = 'comment'
            """, post_db_id)
        
        if existing and existing > 0:
            skipped += 1
            processed += 1
            if processed % 50 == 0:
                print(f"  进度: {processed}/{len(posts)} (跳过 {skipped})")
            continue
        
        # 拉取评论
        count = 0
        try:
            async for reply in client.iter_messages(entity, reply_to=post_msg_id):
                reply_text = reply.text or ""
                if not reply_text:
                    continue
                
                reply_created = reply.date
                if reply_created and reply_created.tzinfo is None:
                    reply_created = reply_created.replace(tzinfo=timezone.utc)
                
                await db.insert_message(
                    chat_id=chat_id,
                    message_id=reply.id,
                    message_text=reply_text,
                    created_at=reply_created,
                    parent_message_id=post_db_id,
                    message_type="comment",
                )
                count += 1
        except MsgIdInvalidError:
            errors += 1
            if errors <= 5:
                print(f"  ⚠️ 帖子 {post_msg_id}: MsgIdInvalid")
        except FloodWaitError as e:
            print(f"  ⏳ FloodWait: {e.seconds}秒")
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ⚠️ 帖子 {post_msg_id}: {str(e)[:60]}")
        
        if count > 0:
            total += count
        
        processed += 1
        if processed % 10 == 0:
            print(f"  进度: {processed}/{len(posts)}, 新增评论: {total}, 跳过: {skipped}, 错误: {errors}")
        
        await asyncio.sleep(0.3)
    
    print(f"\n✅ 完成!")
    print(f"  📊 处理: {processed}/{len(posts)}")
    print(f"  📊 跳过已有评论: {skipped}")
    print(f"  📊 新增评论: {total}")
    print(f"  📊 错误: {errors}")
    
    # 最终统计
    async with db.pool.acquire() as conn:
        comments = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type = 'comment'")
    print(f"  📊 评论总数: {comments}")
    
    await client.disconnect()
    await db.close()


asyncio.run(main())

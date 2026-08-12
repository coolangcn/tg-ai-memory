"""
全量补抓评论脚本。
数据库中已有帖子，但缺少评论。
"""
import asyncio
import os
import sys
from datetime import timezone
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Channel, User

from db import Database

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL")
WATCH_CHANNELS = os.getenv("WATCH_CHANNELS", "").split(",")

print(f"📋 配置加载完成")
print(f"  API_ID: {API_ID}")
print(f"  PHONE: {PHONE}")
print(f"  WATCH_CHANNELS: {WATCH_CHANNELS}")
print(f"  DATABASE_URL: {DATABASE_URL[:50]}...")


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
    if cls is None:
        raise ValueError(f"不支持的代理类型: {scheme}")
    if auth:
        user, _, pwd = auth.partition(":")
        return (cls, host, port, True, user, pwd)
    return (cls, host, port)


async def sync_all_comments(client, db, entity, chat_id: int):
    """补抓所有帖子的评论。"""
    print(f"\n💬 开始补抓评论...")
    
    # 获取所有帖子
    print("  📥 正在从数据库获取帖子列表...")
    async with db.pool.acquire() as conn:
        posts = await conn.fetch("""
            SELECT id, message_id, message_text, created_at
            FROM messages
            WHERE chat_id = $1 AND message_type = 'post' AND message_id IS NOT NULL
            ORDER BY created_at ASC
        """, chat_id)
    
    print(f"  📊 共 {len(posts)} 个帖子需要检查评论")
    
    if len(posts) == 0:
        print("  ⚠️ 没有找到帖子，跳过")
        return 0
    
    total_comments = 0
    processed = 0
    skipped = 0
    has_comments = 0
    errors = 0
    
    for post in posts:
        post_db_id = post['id']
        post_msg_id = post['message_id']
        
        try:
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
                    print(f"  ⏭️ 已处理 {processed}/{len(posts)} (跳过 {skipped} 个已有评论)")
                continue
            
            # 拉取评论
            comment_count = 0
            async for reply in client.iter_messages(entity, reply_to=post_msg_id):
                reply_text = reply.text or ""
                if not reply_text:
                    continue
                
                reply_user_name = None
                reply_user_id = None
                try:
                    sender = await reply.get_sender()
                    if sender is not None:
                        reply_user_id = str(sender.id)
                        if isinstance(sender, User):
                            reply_user_name = sender.username or sender.first_name or f"User{sender.id}"
                except Exception:
                    pass
                
                reply_created = reply.date
                if reply_created and reply_created.tzinfo is None:
                    reply_created = reply_created.replace(tzinfo=timezone.utc)
                
                await db.insert_message(
                    chat_id=chat_id,
                    message_id=reply.id,
                    user_id=reply_user_id,
                    user_name=reply_user_name,
                    message_text=reply_text,
                    created_at=reply_created,
                    parent_message_id=post_db_id,
                    message_type="comment",
                )
                comment_count += 1
            
            if comment_count > 0:
                has_comments += 1
                total_comments += comment_count
            
            processed += 1
            if processed % 10 == 0:
                print(f"  📊 已处理 {processed}/{len(posts)}, 新增 {total_comments} 条评论, 跳过 {skipped}")
            
            await asyncio.sleep(0.3)
            
        except Exception as e:
            errors += 1
            if errors <= 10:
                print(f"  ⚠️ 帖子 {post_msg_id} 评论抓取失败: {str(e)[:80]}")
            await asyncio.sleep(2)
    
    print(f"\n✅ 评论补抓完成:")
    print(f"  📊 处理帖子: {processed}/{len(posts)}")
    print(f"  📊 跳过已有评论: {skipped}")
    print(f"  📊 有评论的帖子: {has_comments}")
    print(f"  📊 新增评论总数: {total_comments}")
    print(f"  📊 失败: {errors}")
    return total_comments


async def show_stats(db, chat_id: int):
    """显示统计信息。"""
    async with db.pool.acquire() as conn:
        posts = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND message_type = 'post'", chat_id) or 0
        comments = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE message_type = 'comment' AND parent_message_id IN (SELECT id FROM messages WHERE chat_id = $1)", chat_id) or 0
    
    print(f"\n{'='*50}")
    print(f"📊 数据库统计 (chat_id={chat_id}):")
    print(f"  📝 帖子总数: {posts}")
    print(f"  💬 评论总数: {comments}")
    if posts > 0:
        print(f"  📈 平均每帖评论: {comments/posts:.1f}")
    print(f"{'='*50}")


async def main():
    print("="*50)
    print("🔄 全量补抓评论")
    print("="*50)
    
    # 连接数据库
    print("📦 连接数据库...")
    db = Database(DATABASE_URL)
    await db.connect()
    print("✅ 数据库连接成功")
    
    # 连接 Telegram
    print("🤖 连接 Telegram...")
    client = TelegramClient("telegram", API_ID, API_HASH, proxy=build_proxy())
    await client.start(phone=PHONE, password=PASSWORD)
    me = await client.get_me()
    print(f"✅ Telegram 登录成功: {me.first_name} (@{me.username or me.id})")
    
    for channel in WATCH_CHANNELS:
        channel = channel.strip()
        if not channel:
            continue
        
        print(f"\n{'='*50}")
        print(f"📺 处理频道: {channel}")
        print(f"{'='*50}")
        
        try:
            print(f"📡 获取频道实体...")
            entity = await client.get_entity(channel)
            # Telegram 频道 ID 需要转换为标准格式：-100 + channel_id
            raw_id = entity.id
            if raw_id > 0 and raw_id < 1000000000000:
                chat_id = int(f"-100{raw_id}")
            else:
                chat_id = raw_id
            print(f"📋 频道 ID: {entity.id} -> 转换为 {chat_id}")
            
            # 显示当前统计
            await show_stats(db, chat_id)
            
            # 补抓评论
            await sync_all_comments(client, db, entity, chat_id)
            
            # 显示最终统计
            await show_stats(db, chat_id)
            
        except Exception as e:
            print(f"❌ 频道 {channel} 处理失败: {e}")
            import traceback
            traceback.print_exc()
    
    await client.disconnect()
    await db.close()
    print("\n✅ 全部完成！")


if __name__ == "__main__":
    asyncio.run(main())

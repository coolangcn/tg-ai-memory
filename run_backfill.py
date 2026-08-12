"""立即执行评论补抓任务"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from db import Database
from collector import TelegramCollector
from scheduler import BotScheduler

load_dotenv()

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

async def main():
    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:cncncncn@10.88.0.3:5433/tg_bot")
    db = Database(database_url)
    await db.connect()
    
    collector = TelegramCollector(
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
        os.getenv("TELEGRAM_PHONE"),
        db,
        os.getenv("WATCH_CHANNELS").split(","),
    )
    
    # 创建调度器（不需要 gemini 来执行 backfill）
    scheduler = BotScheduler(collector, db, gemini=None, summary_chat_id=0)
    
    try:
        await collector.start()
        
        print("=" * 60)
        print("📥 开始全量评论补抓")
        print("=" * 60)
        
        await scheduler.daily_comment_backfill_task()
        
        print("\n" + "=" * 60)
        print("✅ 评论补抓完成！")
        print("=" * 60)
        
    finally:
        await collector.stop()
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())

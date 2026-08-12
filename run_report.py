"""重新生成最新的评论排名报告"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from db import Database
from collector import TelegramCollector
from gemini_service import GeminiService
from send_report import generate_and_send, _clear_history

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
    
    gemini = GeminiService(os.getenv("OPENAI_API_KEY"))
    
    collector = TelegramCollector(
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
        os.getenv("TELEGRAM_PHONE"),
        db,
        os.getenv("WATCH_CHANNELS").split(","),
    )
    
    try:
        await collector.start()
        
        # 第一步：清除历史
        print("=" * 60)
        print("🗑️ 第一步：清除历史收藏消息")
        print("=" * 60)
        await _clear_history(collector)
        
        # 第二步：生成报告
        print("\n" + "=" * 60)
        print("📊 第二步：生成高分榜单报告")
        print("=" * 60)
        await generate_and_send(db, collector, gemini)
        
        print("\n✅ 全部完成！")
        
    finally:
        await collector.stop()
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())

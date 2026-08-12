"""测试帖子内容同步 + 评分更新 + 去重报告"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from db import Database
from collector import TelegramCollector
from gemini_service import GeminiService
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
    
    scheduler = BotScheduler(collector, db, gemini=None, summary_chat_id=0)
    
    try:
        await collector.start()
        
        # 第一步：同步帖子内容
        print("=" * 60)
        print("📝 第一步：同步帖子内容（检测编辑/评分变更）")
        print("=" * 60)
        await scheduler.daily_post_content_sync_task()
        
        # 第二步：更新评论评分
        print("\n" + "=" * 60)
        print("📊 第二步：更新评论评分")
        print("=" * 60)
        await scheduler.daily_score_update_task()
        
        # 第三步：生成报告
        print("\n" + "=" * 60)
        print("📋 第三步：生成报告")
        print("=" * 60)
        from send_report import generate_and_send, _clear_history
        
        gemini = GeminiService(os.getenv("OPENAI_API_KEY"))
        await _clear_history(collector)
        await generate_and_send(db, collector, gemini)
        
        print("\n✅ 全部完成！")
        
    finally:
        await collector.stop()
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())

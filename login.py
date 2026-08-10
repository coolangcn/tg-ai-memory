"""
一次性交互式登录，保存 Telethon 会话。

用法：
1. 在 .env 配置 TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE
   （API ID/Hash 在 https://my.telegram.org → API development tools 获取）
2. 运行：python login.py
3. 按提示输入登录验证码（手机收到）和两步验证密码（如开启）
4. 成功后生成 telegram.session，之后运行 python main.py 即可
"""
import asyncio
import os
import logging
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
logging.basicConfig(level=logging.INFO)


def _build_proxy():
    """从 TELEGRAM_PROXY 环境变量构建 Telethon 代理元组，如 socks5://127.0.0.1:7890"""
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


async def main():
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")
    password = os.getenv("TELEGRAM_PASSWORD") or None

    if not api_id or not api_hash or not phone:
        print("❌ 请在 .env 中配置 TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE")
        print("   获取 API ID/Hash: https://my.telegram.org → API development tools")
        return

    proxy = _build_proxy()
    if proxy:
        print(f"🌐 使用代理: {os.getenv('TELEGRAM_PROXY')}")

    client = TelegramClient("telegram", int(api_id), api_hash, proxy=proxy)
    await client.start(phone=phone, password=password)
    me = await client.get_me()
    print(f"✅ 登录成功: {me.first_name} (@{me.username or me.id})")
    print("   Session 已保存到 telegram.session")
    print("   之后运行: python main.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

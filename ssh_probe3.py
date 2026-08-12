"""检查 NAS 代理端口 + 本地 session 文件"""
import paramiko, os

HOST = "10.88.0.3"
USER = "admin"
PASSWORD = "74123698cN"

def run(cmd, timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASSWORD, timeout=15, look_for_keys=False, allow_agent=False)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    client.close()
    return out.strip(), err.strip()

cmds = [
    ("ss -tlnp 2>/dev/null | grep -E ':(7890|7891|1080|8080|10809)' || echo '无常见代理端口'", "代理端口"),
    ("docker ps -a 2>/dev/null | head; echo '---'; docker images 2>/dev/null | head", "Docker 容器/镜像"),
    ("ls -la /app 2>/dev/null", "/app 目录"),
    ("iptables -L -n 2>/dev/null | head -5; echo '---'; nft list ruleset 2>/dev/null | head -5 || echo '无nft'", "防火墙"),
]

for cmd, desc in cmds:
    out, err = run(cmd)
    print(f"── {desc} ──")
    print(out if out else err)
    print()

# 本地 session 文件
print("── 本地 telegram.session ──")
for f in os.listdir("."):
    if "session" in f.lower() or "telegram" in f.lower():
        size = os.path.getsize(f) if os.path.isfile(f) else 0
        print(f"  {f} ({size} bytes)")

# 检查 requirements.txt
print("── requirements.txt ──")
print(open("requirements.txt").read())

"""检查 NAS 环境：PostgreSQL 数据库、目录、时间等"""
import paramiko

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
    ("date", "NAS 当前时间"),
    ("which psql; psql --version 2>/dev/null", "psql 客户端"),
    ("ls -la / | head -20", "根目录"),
    ("df -h / | tail -1", "磁盘空间"),
    ("free -h | head -2", "内存"),
    ("ls -la ~", "家目录"),
    ("which python3; python3 --version 2>/dev/null", "Python"),
]

for cmd, desc in cmds:
    out, err = run(cmd)
    print(f"── {desc} ──")
    print(out if out else err)
    print()

# 测试 PostgreSQL 连接（用 .env 里的密码）
out, err = run("PGPASSWORD=cncncncn psql -h 127.0.0.1 -p 5433 -U postgres -c '\\l' 2>&1 | head -20")
print("── PostgreSQL 数据库列表 ──")
print(out if out else err)

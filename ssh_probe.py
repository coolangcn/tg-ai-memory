"""SSH 连接 NAS 测试 + 环境探测"""
import paramiko

HOST = "10.88.0.3"
USER = "admin"
PASSWORD = "74123698cN"
PORT = 22

def run(cmd, timeout=30):
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(HOST, PORT, USER, PASSWORD, timeout=15, look_for_keys=False, allow_agent=False)
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        client.close()
        return out.strip(), err.strip()
    except Exception as e:
        return "", f"连接失败: {e}"

# 基本探测
cmds = [
    ("uname -a", "系统信息"),
    ("cat /etc/os-release 2>/dev/null | head -3", "OS 发行版"),
    ("uname -m", "CPU 架构"),
    ("which docker && docker --version", "Docker 版本"),
    ("which docker-compose; which docker 2>/dev/null; docker compose version 2>/dev/null", "Compose"),
    ("docker ps -a 2>/dev/null | head -10", "运行容器"),
]

for cmd, desc in cmds:
    out, err = run(cmd)
    print(f"── {desc} ──")
    print(out if out else err)
    print()

# 检查 PostgreSQL 是否在 NAS
out, err = run("docker ps -a 2>/dev/null | grep -i postgres; ss -tlnp 2>/dev/null | grep 5433")
print("── PostgreSQL 5433 ──")
print(out if out else err)

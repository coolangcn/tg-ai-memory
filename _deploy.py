"""部署：上传修改后的代码到容器并重启 tg-analyzer。"""
import paramiko

HOST = "10.88.0.3"
USER = "admin"
PASSWORD = "74123698cN"

FILES = ["collector.py", "scheduler.py", "main.py"]
LOCAL_DIR = "d:/tg-bot"
REMOTE_DIR = "/tmp/tgfix"


def run(cmd, timeout=120):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASSWORD, timeout=15, look_for_keys=False, allow_agent=False)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    client.close()
    return out.strip(), err.strip()


def sftp_put(remote_path, local_path):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASSWORD, timeout=15, look_for_keys=False, allow_agent=False)
    sftp = client.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
    client.close()
    print(f"  ✅ 上传 {local_path} -> {remote_path}")


# 1. 上传到 NAS 临时目录
print("=== 1. 上传到 NAS /tmp/tgfix ===")
run(f"echo 74123698cN | sudo -S mkdir -p {REMOTE_DIR} && echo 74123698cN | sudo -S chown admin {REMOTE_DIR}")
for f in FILES:
    sftp_put(f"{REMOTE_DIR}/{f}", f"{LOCAL_DIR}/{f}")

# 2. 复制进容器并重启
print("=== 2. 复制进容器 /app 并重启 ===")
cmd = (
    "echo 74123698cN | sudo -S bash -c '"
    "docker cp /tmp/tgfix/collector.py tg-analyzer:/app/collector.py && "
    "docker cp /tmp/tgfix/scheduler.py tg-analyzer:/app/scheduler.py && "
    "docker cp /tmp/tgfix/main.py tg-analyzer:/app/main.py && "
    "docker restart tg-analyzer"
    "'"
)
out, err = run(cmd, timeout=180)
print("OUT:", out)
print("ERR:", err[:500])

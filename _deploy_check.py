"""查看 tg-analyzer 容器挂载与代码位置。"""
import paramiko

HOST = "10.88.0.3"
USER = "admin"
PASSWORD = "74123698cN"


def run(cmd, timeout=120):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASSWORD, timeout=15, look_for_keys=False, allow_agent=False)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    client.close()
    return out.strip(), err.strip()


# 挂载信息
out, err = run("echo 74123698cN | sudo -S docker inspect tg-analyzer --format '{{json .Mounts}}'")
print("MOUNTS:", out)
print("ERR:", err[:500])

# 工作目录
out, err = run("echo 74123698cN | sudo -S docker inspect tg-analyzer --format '{{.Config.WorkingDir}}'")
print("WORKDIR:", out)

# 容器内文件
out, err = run("echo 74123698cN | sudo -S docker exec tg-analyzer sh -c 'pwd; ls -la | head -30'")
print("CONTAINER:", out)

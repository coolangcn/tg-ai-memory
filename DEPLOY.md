# NAS 部署快速指南（纯定时报告版）

## 前置条件
- NAS 已安装 Docker + Docker Compose（群晖/威联通等均支持）
- PostgreSQL 已运行（127.0.0.1:5433，数据库 tg_bot 已创建）

## 部署步骤

### 1. 在 NAS 上创建项目目录

```bash
mkdir -p /vol1/1000/docker/tg-analyzer
cd /vol1/1000/docker/tg-analyzer
```

### 2. 上传项目文件

将以下文件上传到 NAS：
- `Dockerfile`
- `docker-compose.yml`
- `requirements.txt`
- `*.py`（所有 Python 文件）
- `.env`（环境变量文件，含 API 密钥）
- `telegram.session`（已登录的会话文件）

### 3. 启动服务

```bash
docker-compose up -d
```

### 4. 查看日志确认运行

```bash
docker-compose logs -f tg-analyzer
```

## 定时任务（自动运行）

容器启动后常驻运行，按服务器本地时区定时执行：

| 时间 | 任务 |
|------|------|
| 09:00 | 高分老师榜单报告（评分≥9.18 且评价≥10 条，TOP20 图文并茂发送到收藏夹） |
| REPORT_TIME（默认 23:59） | 每日精华总结 |
| 00:30 | 数据库清理（删除 14 天前旧消息） |

如需调整榜单发送时间，修改 `scheduler.py` 中的 CronTrigger（`hour=9, minute=0`）。

## 首次部署：全量采集历史报告

数据库为空时，先运行一次全量采集（约 10-15 分钟）：

```bash
docker-compose run --rm tg-analyzer python collect_reports.py
```

## 日常运维

### 查看日志
```bash
docker-compose logs -f tg-analyzer
```

### 重启服务
```bash
docker-compose restart tg-analyzer
```

### 更新代码
```bash
docker-compose down
# 上传新文件
docker-compose build
docker-compose up -d
```

### 重新登录（session 失效时）
```bash
docker-compose run --rm tg-analyzer python login.py
```

## 数据备份

- 数据库：通过 PostgreSQL 定期备份
- 会话文件：`telegram.session`（丢失需重新登录）
- 媒体引用：存储在数据库中，实际文件在 Telegram

## 常见问题

**Q: Telegram session 失效？**
A: 运行 `docker-compose run --rm tg-analyzer python login.py` 重新登录。

**Q: 数据库连接失败？**
A: 确认 PostgreSQL 已启动，且 DATABASE_URL 配置正确。

**Q: 需要手动触发一次榜单报告？**
A: 运行 `docker-compose run --rm tg-analyzer python send_report.py`。

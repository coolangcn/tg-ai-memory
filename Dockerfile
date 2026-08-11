# Telegram 频道分析平台 - Dockerfile（纯定时报告，无 Web 页面）
FROM python:3.13-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY *.py .

# 创建媒体目录
RUN mkdir -p media/image media/video media/file

# 环境变量
ENV PYTHONUNBUFFERED=1
ENV MEDIA_DIR=/app/media

# 启动命令：采集器 + 定时报告
CMD ["python", "main.py"]

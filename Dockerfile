# Telegram 频道分析平台 - Dockerfile
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
COPY templates/ templates/

# 创建媒体目录
RUN mkdir -p media/image media/video media/file

# 环境变量
ENV PYTHONUNBUFFERED=1
ENV MEDIA_DIR=/app/media

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "-m", "uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000"]

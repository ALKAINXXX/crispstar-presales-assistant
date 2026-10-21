FROM python:3.11-slim-bookworm

# 使用 UTF-8，直接输出日志，并避免生成 Python 字节码缓存。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ONNX Runtime 等本地计算依赖需要的 Linux 运行库。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 先安装依赖；仅修改业务源码时，可以复用这一层的构建缓存。
COPY requirements.txt requirements.docker.txt ./
RUN python -m pip install --no-cache-dir -r requirements.docker.txt \
    && python -m pip check

# 使用普通用户运行应用和缓存准备命令。
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home \
        --shell /usr/sbin/nologin appuser

# 复制运行所需源码、已构建前端、检查脚本及正式索引。
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY run.py LICENSE ./
COPY data/vector_store/chengxing_index.json ./data/vector_store/chengxing_index.json

# 后续由 Compose 为以下三个目录分别挂载正式版数据卷。
# 保留镜像内的 data/vector_store，避免挂载整个 data 目录遮蔽索引。
RUN mkdir -p /app/data/database /app/data/embedding_models /app/data/tiktoken \
    && chown -R appuser:appuser /app/data

ENV SAAS_DATABASE_URL=sqlite:////app/data/database/saas_assistant.db \
    EMBEDDING_CACHE_DIR=/app/data/embedding_models \
    TIKTOKEN_CACHE_DIR=/app/data/tiktoken \
    HF_HUB_DISABLE_TELEMETRY=1

USER appuser

# 声明容器内部端口；电脑端口和本机访问限制由 Compose 配置。
EXPOSE 8000

# 保留原 run.py，通过 Uvicorn 在容器内监听所有网卡，固定单进程。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

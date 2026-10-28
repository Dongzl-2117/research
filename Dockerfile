FROM python:3.12-slim-bookworm AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 段永平博客 PDF 用 poppler 抽文字（pdftohtml 保留加粗，pdftotext 兜底，pdfinfo 看页数）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 先按锁文件装依赖（版本和 hash 都锁死）：只有 requirements.lock 变了才重装，
# 改代码重新部署不会顺手拉到新版本的 FastAPI / Starlette。升级依赖见 docs/deploy.md。
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir --no-deps .

# 自动上线（scripts/deploy.sh）传进这次的提交号，/health 报出来，上线脚本据此确认换上的是新代码；
# 手动构建是 unknown。放在最后，提交号每次都变，放前面会让装依赖那几层的缓存失效。
# 标签给上线后清理旧镜像用：只清这个项目建的。
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
LABEL com.zldong.research=app

EXPOSE 8088
CMD ["research-web"]

# RapidOCR 回退镜像：在 app 之上加 onnxruntime + OpenCV。默认识图不进这个阶段。
#   docker compose --profile ocr build ocr
#   docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr --limit 500
FROM app AS ocr

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-ocr.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-ocr.lock

CMD ["research-ocr", "--engine", "rapidocr"]

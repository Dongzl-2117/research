FROM python:3.12-slim-bookworm AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先按锁文件装依赖（版本和 hash 都锁死）：只有 requirements.lock 变了才重装，
# 改代码重新部署不会顺手拉到新版本的 FastAPI / Starlette。升级依赖见 docs/deploy.md。
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8088
CMD ["research-web"]

# 图片文字（OCR）用的镜像：在 app 之上加 RapidOCR（onnxruntime + OpenCV）。不进主镜像。
#   docker compose --profile ocr build ocr
FROM app AS ocr

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-ocr.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-ocr.lock

CMD ["research-ocr"]

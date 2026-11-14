# 底座按 digest 钉住，道理同 docker/postgres/Dockerfile：上游重推标签以后，下一次上线会重拉底座、
# 重装 requirements.lock 里的全部包。升级单开 PR，见 docs/deploy.md「基础镜像」。
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS app

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

# 自动上线（scripts/deploy.sh）传进这次的提交号，/health 报出来，上线脚本据此确认换上的是新代码；
# 手动构建是 unknown。放在最后，提交号每次都变，放前面会让装依赖那几层的缓存失效。
# 标签给上线后清理旧镜像用：只清这个项目建的。
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
LABEL com.zldong.research=app

EXPOSE 8088
CMD ["research-web"]

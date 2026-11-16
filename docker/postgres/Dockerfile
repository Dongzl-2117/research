# 官方 Postgres 16 上编译 SCWS + zhparser。同一份 Dockerfile 在 arm64（Mac mini）和 amd64 上本地编译。
# pgvector（语义检索的向量）直接装官方镜像自带的 PGDG 源里的包，不编译。
# 底座按 digest 钉住（多架构总清单，arm64、amd64 都认），现在是上游 2026-11-12 推的 PostgreSQL 16.16。
# 只写标签时上游一重推，下一次自动上线就拉新底座、下面几层全部重建，Mac mini 上要三个多小时。
# 升级单开 PR，见 docs/deploy.md「基础镜像」。
FROM postgres:16-bookworm@sha256:a49f7438ba03589f9b1ca062f2da82e899b48ea1dc50d2449fa09a70be83ebca

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        bzip2 \
        ca-certificates \
        curl \
        postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/scws-1.2.3.tar.bz2 /tmp/scws.tar.bz2
RUN tar -xjf /tmp/scws.tar.bz2 -C /tmp \
    && cd /tmp/scws-1.2.3 \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig \
    && rm -rf /tmp/scws-1.2.3 /tmp/scws.tar.bz2

ARG ZHPARSER_COMMIT=2e995c4df672563992b4d7a147b8fa2d0d4cda6c
RUN curl -fsSL "https://github.com/amutu/zhparser/archive/${ZHPARSER_COMMIT}.tar.gz" \
        | tar -xz -C /tmp \
    && cd /tmp/zhparser-${ZHPARSER_COMMIT} \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf /tmp/zhparser-${ZHPARSER_COMMIT} \
    && apt-get update \
    && apt-get purge -y build-essential bzip2 curl postgresql-server-dev-16 \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY init/01-zhparser.sql /docker-entrypoint-initdb.d/01-zhparser.sql

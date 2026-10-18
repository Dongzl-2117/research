# 官方 Postgres 16 上编译 SCWS + zhparser。同一份 Dockerfile 在 arm64（Mac mini）和 amd64 上本地编译。
FROM postgres:16-bookworm

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

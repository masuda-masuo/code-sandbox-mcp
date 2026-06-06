# code-sandbox-mcp MCP サーバー Dockerfile
FROM python:3.12-slim-bookworm

WORKDIR /app

# システムパッケージ更新
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python パッケージのインストール
# code-sandbox-mcp をインストール
RUN pip install --no-cache-dir \
    git+https://github.com/masuda-masuo/code-sandbox-mcp

# Docker ソケットのバインドマウント用に docker クライアントをインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# MCP サーバーをエントリポイントとして実行
ENTRYPOINT ["code-sandbox-mcp"]
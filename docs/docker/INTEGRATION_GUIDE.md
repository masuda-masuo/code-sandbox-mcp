# code-sandbox-mcp 統合セットアップガイド（Windows）

Claude Desktop から code-sandbox-mcp サーバーに、`mcp-launcher` でセキュアにアクセスします。`mcp-launcher` が起動時にコンテナを管理するため、事前のコンテナ起動は不要です。

---

## 📦 前提条件

- Docker Desktop がインストール済みで起動している
- [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher/releases) をダウンロード済み
- Claude Desktop がインストール済み

---

## 🚀 セットアップ

### 1️⃣ Docker イメージをビルド

リポジトリのルートディレクトリで以下を実行します。

```powershell
docker build -f Dockerfile -t ghcr.io/masuda-masuo/code-sandbox-mcp:latest .
```

イメージをビルド確認:

```powershell
docker images | findstr code-sandbox-mcp
```

> **注意**: 最初はローカルのみです。リモートレジストリ（GHCR）に push する場合は後述参照。

### 2️⃣ launcher.json を作成

```powershell
mkdir %USERPROFILE%\.mcp-launcher
notepad %USERPROFILE%\.mcp-launcher\launcher.json
```

内容:

```json
{
  "code-sandbox-mcp": {
    "command": "docker",
    "args": [
      "run",
      "--rm",
      "-i",
      "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
      "ghcr.io/masuda-masuo/code-sandbox-mcp:latest",
      "stdio"
    ],
    "check_interval_seconds": 60
  }
}
```

> **Docker ソケットのバインドについて**: `code-sandbox-mcp` は内部で Docker コマンドを実行するため、ホストの Docker ソケットをコンテナにマウントする必要があります。上記の `--mount` オプションはこの接続を確立します。

### 3️⃣ Claude Desktop を設定

```powershell
notepad %APPDATA%\Claude\claude_desktop_config.json
```

内容（既存の `mcpServers` に追加）:

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "C:\\work\\mcp\\mcp-launcher.exe",
      "args": ["code-sandbox-mcp"]
    }
  }
}
```

> **注意**: `mcp-launcher.exe` の絶対パスを指定してください。

### 4️⃣ Claude Desktop を再起動

タスクトレイのアイコンを右クリック → Quit して再度起動。

### 5️⃣ 動作確認

Claude Desktop チャットで:

```
以下の Python コードを実行してください:

print("Hello from code-sandbox-mcp!")
import platform
print(f"Python: {platform.python_version()}")
```

コンテナ内で実行された結果が返ればOK。

---

## 🔧 環境変数の渡し方

`code-sandbox-mcp` が GitHub Token などの環境変数を必要とする場合、`launcher.json` と `claude_desktop_config.json` で指定できます。

### launcher.json （トークン管理用）

```json
{
  "code-sandbox-mcp": {
    "command": "docker",
    "args": [
      "run",
      "--rm",
      "-i",
      "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
      "-e", "GITHUB_TOKEN",
      "ghcr.io/masuda-masuo/code-sandbox-mcp:latest",
      "stdio"
    ],
    "env_keys": {
      "GITHUB_TOKEN": "mcp-launcher/code-sandbox-mcp/github-token"
    },
    "check_interval_seconds": 60
  }
}
```

### Credential Manager に登録

```powershell
mcp-launcher register code-sandbox-mcp github-token "github_pat_xxxx"
```

### claude_desktop_config.json でパススルー

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "C:\\work\\mcp\\mcp-launcher.exe",
      "args": ["code-sandbox-mcp"],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

> **セキュリティベストプラクティス**: トークンを `claude_desktop_config.json` に直接記述するより、Credential Manager（`mcp-launcher register`）での管理を推奨します。

---

## 📦 Docker イメージの公開（オプション）

GitHub Container Registry（GHCR）に push する場合：

### 1. GHCR にログイン

```powershell
# GitHub PAT を用意してから
docker login ghcr.io -u <GitHub-username>
# PAT をパスワードプロンプトで入力
```

### 2. イメージ をタグ付けして push

```powershell
docker tag ghcr.io/masuda-masuo/code-sandbox-mcp:latest ghcr.io/<github-username>/code-sandbox-mcp:latest
docker push ghcr.io/<github-username>/code-sandbox-mcp:latest
```

その後、リポジトリの README で:

```markdown
## Docker イメージ

イメージは GitHub Container Registry で公開されています：

```powershell
docker pull ghcr.io/masuda-masuo/code-sandbox-mcp:latest
```
```

---

## 🔧 トラブルシューティング

| 問題 | 解決方法 |
|---|---|
| `イメージが見つからない` | `docker images \| findstr code-sandbox-mcp` で確認。`docker build` を再実行 |
| `Docker ソケットエラー` | Docker Desktop が起動しているか確認。`--mount` オプションが正しく指定されているか確認 |
| `mcp-launcher が見つからない` | `claude_desktop_config.json` の絶対パスを確認 |
| Claude Desktop が接続できない | タスクトレイから完全に Quit して再起動 |
| `permission denied while trying to connect to Docker daemon` | Docker Desktop が起動していない、または Windows で Docker Desktop が Docker CLI アクセスを許可していない |

**詳細ログの確認:**

```powershell
# launcher.json の構文チェック
type %USERPROFILE%\.mcp-launcher\launcher.json

# Docker イメージが存在するか確認
docker images | findstr code-sandbox-mcp

# 手動でコンテナ起動テスト
docker run --rm -i --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock ghcr.io/masuda-masuo/code-sandbox-mcp:latest stdio
```

---

## ✅ チェックリスト

- [ ] Docker Desktop が起動している
- [ ] `docker build -f Dockerfile -t ghcr.io/masuda-masuo/code-sandbox-mcp:latest .` でイメージをビルドした
- [ ] `docker images | findstr code-sandbox-mcp` でイメージが表示される
- [ ] `launcher.json` を `%USERPROFILE%\.mcp-launcher\` に作成した
- [ ] `claude_desktop_config.json` に正しいパスで `mcp-launcher.exe` を指定した
- [ ] Claude Desktop を完全に再起動した
- [ ] チャットで簡単な Python コードを実行して動作確認した

---

## 📚 参考資料

- [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher)
- [code-sandbox-mcp](https://github.com/masuda-masuo/code-sandbox-mcp)
- [MCP セキュリティベストプラクティス](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
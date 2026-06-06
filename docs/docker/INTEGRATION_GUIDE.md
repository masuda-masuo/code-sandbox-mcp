# code-sandbox-mcp 統合セットアップガイド（Windows + Docker）

Claude Desktop から code-sandbox-mcp サーバーに、`mcp-launcher` でセキュアにアクセスします。
GitHub App の短期トークン（最大1時間）を自動取得し、Docker コンテナに注入します。
Claude Desktop 起動時にコンテナが自動起動するため、事前のコンテナ起動は不要です。

---

## 📦 前提条件

- Docker Desktop がインストール済みで起動している
- [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher/releases) をダウンロード済み
- Claude Desktop がインストール済み
- GitHub App を作成済み（App ID・インストール ID・秘密鍵を取得済み）

---

## 🚀 セットアップ

### 1️⃣ Docker イメージをビルド

リポジトリのルートディレクトリで:

```powershell
docker build -f Dockerfile -t ghcr.io/masuda-masuo/code-sandbox-mcp:latest .
```

確認:

```powershell
docker images | findstr code-sandbox-mcp
```

### 2️⃣ mcp-launcher を配置

```
C:\work\mcp\mcp-launcher.exe
```

### 3️⃣ GitHub App の認証情報を Credential Manager に登録

```powershell
mcp-launcher register github APP_ID 123456
mcp-launcher register github INSTALLATION_ID 789012
mcp-launcher register github PRIVATE_KEY "-----BEGIN RSA PRIVATE KEY-----..."
mcp-launcher register github GITHUB_PERSONAL_ACCESS_TOKEN ""
```

> **注意**: `GITHUB_PERSONAL_ACCESS_TOKEN` は空文字で登録します。`token_source` が GitHub App から取得したトークンで自動上書きします。

### 4️⃣ launcher.json を作成

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
      "run", "--rm", "-i",
      "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
      "-e", "GITHUB_TOKEN",
      "ghcr.io/masuda-masuo/code-sandbox-mcp:latest",
      "--pass-through-env", "GITHUB_TOKEN"
    ],
    "env_keys": {
      "GITHUB_TOKEN": "mcp-launcher/github/GITHUB_PERSONAL_ACCESS_TOKEN"
    },
    "token_source": {
      "type": "github_app",
      "app_id_key": "mcp-launcher/github/APP_ID",
      "private_key_key": "mcp-launcher/github/PRIVATE_KEY",
      "installation_id_key": "mcp-launcher/github/INSTALLATION_ID",
      "target_env_key": "GITHUB_TOKEN",
      "refresh_before_seconds": 600
    },
    "check_interval_seconds": 60
  }
}
```

#### 各パラメータの説明

| パラメータ | 説明 |
|---|---|
| `--mount /var/run/docker.sock` | コンテナ内から Docker を操作するためにホストのソケットをマウント |
| `-e GITHUB_TOKEN` | mcp-launcher が取得したトークンをコンテナ環境変数として渡す |
| `--pass-through-env GITHUB_TOKEN` | code-sandbox-mcp がサンドボックスコンテナにさらに渡す |
| `env_keys.GITHUB_TOKEN` | Credential Manager の読み込みキー（token_source が上書き） |
| `token_source.type: github_app` | GitHub App から短期トークンを自動取得 |
| `refresh_before_seconds: 600` | 期限の 10 分前に自動更新 |

#### トークンの流れ

```
GitHub App
    ↓ (短期トークン自動取得)
mcp-launcher
    ↓ -e GITHUB_TOKEN
Docker コンテナ (code-sandbox-mcp)
    ↓ --pass-through-env GITHUB_TOKEN
サンドボックスコンテナ (python:3.12-slim-bookworm 等)
```

### 5️⃣ Claude Desktop を設定

```powershell
notepad %APPDATA%\Claude\claude_desktop_config.json
```

内容:

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

> **注意**: Claude Desktop は PATH を継承しないため、`mcp-launcher.exe` の絶対パスを指定してください。

### 6️⃣ Claude Desktop を再起動

タスクトレイのアイコンを右クリック → Quit して再度起動。

### 7️⃣ 動作確認

Claude Desktop チャットで:

```
以下の Python コードを実行してください:

print("Hello from code-sandbox-mcp!")
import platform
print(f"Python: {platform.python_version()}")
```

結果が返ればOK。

---

## 🔧 トラブルシューティング

| 問題 | 解決方法 |
|---|---|
| `token error` | App ID・Installation ID・秘密鍵の登録を確認 |
| `image not found` | `docker images \| findstr code-sandbox-mcp` で確認。`docker build` を再実行 |
| `Docker ソケットエラー` | Docker Desktop が起動しているか確認 |
| `mcp-launcher が見つからない` | `claude_desktop_config.json` の絶対パスを確認 |
| Claude Desktop が接続できない | タスクトレイから完全に Quit して再起動 |

**詳細ログ:**

```powershell
# launcher.json の構文チェック
type %USERPROFILE%\.mcp-launcher\launcher.json

# 手動でコンテナ起動テスト（GITHUB_TOKEN は適当な値で）
$env:GITHUB_TOKEN="test"
docker run --rm -i --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock -e GITHUB_TOKEN ghcr.io/masuda-masuo/code-sandbox-mcp:latest --pass-through-env GITHUB_TOKEN
```

---

## ✅ チェックリスト

- [ ] Docker Desktop が起動している
- [ ] `docker build` でイメージをビルドした
- [ ] GitHub App を作成した
- [ ] `mcp-launcher register github APP_ID` を登録した
- [ ] `mcp-launcher register github INSTALLATION_ID` を登録した
- [ ] `mcp-launcher register github PRIVATE_KEY` を登録した
- [ ] `launcher.json` を作成した
- [ ] `claude_desktop_config.json` に絶対パスで `mcp-launcher.exe` を指定した
- [ ] Claude Desktop を完全に再起動した

---

## 📚 参考資料

- [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher)
- [GitHub App setup](https://github.com/masuda-masuo/mcp-launcher/blob/main/docs/setup/github-app-setup.md)
- [MCP セキュリティベストプラクティス](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)

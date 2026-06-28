# Package Install Tool — 設計ドキュメント

> Issue: #262
> 位置づけ: `sandbox_exec` 経由の `pip install` を専用ツールに置き換える。
> `sandbox_exec` は git コマンド専用に近づけ、pip はこのツールに集約する。

## 背景

`sandbox_exec` で `pip install --quiet -e .[dev] 2>&1 | tail -3` とやっているが、
LLM が依存解決ログの大半を読む必要はなく、コンテキストを無駄に消費している。

## 要件

- **入力**: `container_id`（必須）、`packages`（パッケージ指定, `str | list[str]`）、`constraints`、`requirements`、`editable`、`extras`、`upgrade` 等
- **出力**: 成功/失敗 + `installed_packages`（インストール済みパッケージ一覧のサマリ） + エラー詳細
- 内部で `pip install` を呼ぶが、出力を構造化して返す
- `sandbox_exec` の雑務から pip を分離する第一歩

## 出力形式

```json
{
  "status": "ok",
  "installed_packages": ["package1==1.0.0", "package2==2.1.0"],
  "changed": 2,
  "output": "Successfully installed package1-1.0.0 package2-2.1.0"
}
```

エラー時:
```json
{
  "status": "error",
  "error": "pip install failed (exit code 1)",
  "stderr": "ERROR: Could not find a version that satisfies the requirement nonexistent-package"
}
```

## 実装方針

- 新しいツールファイル `tools/package.py` に `package_install` 関数として実装
- `server.py` で `mcp.tool()` 登録
- 内部では `pip install` を `subprocess` 的に `exec_run` で実行
- 出力は `pip list --format=json` でインストール済みパッケージ一覧を取得し、インストール前後の diff を返す
- `uv` がコンテナ内にあれば `uv pip install` を優先（高速）

## スコープ（やらないこと）

- pip 以外のパッケージマネージャ（npm, cargo, go 等）は対象外
- 仮想環境の自動作成・管理は対象外（コンテナが既に仮想環境）
- 依存関係のツリー表示・監査は対象外

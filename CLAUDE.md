# このプロジェクトの作業ルール

## サンドボックス内で作業すること

**すべての実装・編集・テストはsandbox内で完結させる。ローカルファイルを直接編集しない。**

### PRブランチの修正作業
```
sandbox_initialize(allow_network=True, inject_vcs_token=True)
# → sandbox内でgit clone → checkout → pip install → 編集 → テスト
```

### issue着手（mainから）
```
sandbox_initialize(clone_repo="masuda-masuo/code-sandbox-mcp")
```

「sandboxで完結できないならツールの問題」という姿勢で臨む。
不便を感じたらそれ自体がissueのタネ。

# User stories

ストーリーの正本は `scripts/user-story-catalog.py` です。CSV は生成物です。

| 列 | 意味 |
|---|---|
| story_id | `{EPIC}-{NNN}` |
| epic | RAG / FILE / MYSQL / SEARCH / DIFY / EVAL / PURGE / UI |
| steps | 番号付きの操作 |
| expected_behavior | 検証できる結果 |
| impl_status | implemented / planned |
| test_method | unit / build / api / manual |
| test_ref | pytest の対象、または ruff / mypy |

再実行:

```powershell
uv run python scripts/user-story-catalog.py
uv run python scripts/run-user-story-tests.py
```

`fail > 0` のときランナーは exit 1 です。集計は `summary.md` です。

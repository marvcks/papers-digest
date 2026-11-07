# 论文速览

面向个人阅读的论文与新闻速览工具。论文直接从 Crossref 获取，缺少摘要时通过 OpenAlex 补齐；支持中文标题、按需摘要翻译、历史检索、想读标记及阅读状态。新闻使用独立 RSS 来源、历史库与页面。

## 功能

- 按期刊、日期、关键词和阅读状态筛选论文，支持整月与自定义日期范围。
- 更新时批量翻译标题，点击「显示摘要」时才翻译摘要，复用本地缓存。
- 历史按 DOI 增量保存；窗口过期不会删除旧论文。
- 手动触发更新，服务启动和打开页面不会自动抓取或调用模型。
- 自动、浅色、深色三档主题；卡片分批渲染。
- 独立新闻入口，支持 RSS/Atom、翻译和历史检索。
- 可选 Zotero 命令行工具，检查已有条目与 PDF，仅在显式指定 `--import` 时导入。

## 本地使用

需要 Python 3.13+，推荐使用 macOS 或 Linux（缓存文件锁使用 `fcntl`）。

```sh
uv sync
cp .env.example .env
```

在 `.env` 填写自己的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 和 `CROSSREF_MAILTO`。论文和新闻速览不需要 Miniflux；其配置仅用于仓库保留的旧版脚本。

```sh
# 首次生成论文页面；不调用模型时加 --no-llm
uv run python papers_digest.py --days 30

# 启动本地服务，默认 http://127.0.0.1:8770
uv run python papers_server.py
```

此后从页面点击「更新文献」或新闻入口的更新按钮。生成的 `papers-digest.html` 也可以直接打开；手动更新、按需翻译与服务端状态保存需要本地服务。

ECNU 接口每模型最多并发 3：命令行默认 3 workers，服务后台论文更新使用 2 workers，新闻及按需翻译共用另一个通道。

## 文件

| 文件 | 用途 |
| --- | --- |
| `papers_digest.py` | 论文抓取、翻译、历史保存与页面生成 |
| `papers_server.py` | 本地 HTTP 服务与后台更新 |
| `papers_viewer.template.html` | 论文页面模板 |
| `journals.json` | 期刊名称及 ISSN 配置 |
| `news_digest.py` | 新闻抓取、翻译与页面生成 |
| `news_sources.json` | 新闻来源配置 |
| `news_viewer.template.html` | 新闻页面模板 |
| `papers_to_zotero.py` | Zotero 本地条目和 PDF 核验及可选导入 |

`.env`、翻译缓存、论文/新闻历史、阅读记录、想读记录、生成页面和本机审计输出留在本地，不上传 GitHub。

## 验证

```sh
uv run python -m unittest test_papers_digest test_news_digest test_papers_to_zotero -v
```

回归使用临时目录和模拟接口。修改页面模板后，还需将内联 JavaScript 提取后执行 `node --check`。

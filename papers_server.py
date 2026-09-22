#!/usr/bin/env python3
"""论文速览的本地服务。

静态的 papers-digest.html 双击就能看，但「按需翻译」「想读标记」「存入 Zotero」
这几个功能需要一个本地进程，因为：
- 浏览器从 file:// 打开的页面调不了 LLM 接口（跨域），也不该拿着你的 API key
- Zotero 的本地接口只在 127.0.0.1 上，页面直连同样有跨域问题

只用标准库，没有额外依赖。

用法：
    python papers_server.py            # 起服务并打开浏览器
    python papers_server.py --port 8899 --no-open
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import papers_digest as pd

ROOT = Path(__file__).resolve().parent
MARKS_FILE = ROOT / "papers-marks.json"
ZOTERO_FILE = ROOT / "papers-zotero.json"
DEFAULT_PAGE = "papers-digest.html"

ZOTERO_BASE = "http://127.0.0.1:23119"
ZOTERO_SAVE = f"{ZOTERO_BASE}/connector/saveItems"
ZOTERO_LIBRARY = f"{ZOTERO_BASE}/api/users/0/items"

# 只允许访问这些后缀的文件，避免把 .env 之类的东西暴露出去
SERVABLE_SUFFIXES = {".html", ".css", ".js", ".json", ".png", ".svg"}

_marks_lock = threading.Lock()
_zotero_lock = threading.Lock()
_translate_lock = threading.Lock()


# --------------------------------------------------------------------------
# 想读标记
# --------------------------------------------------------------------------


def load_marks() -> list[str]:
    with _marks_lock:
        if not MARKS_FILE.exists():
            return []
        try:
            data = json.loads(MARKS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return sorted(set(data.get("starred", [])))


def save_marks(dois: list[str]) -> list[str]:
    cleaned = sorted({d for d in dois if isinstance(d, str) and d})
    with _marks_lock:
        MARKS_FILE.write_text(
            json.dumps({"starred": cleaned}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return cleaned


# --------------------------------------------------------------------------
# 已入库 Zotero 的 DOI
# --------------------------------------------------------------------------


def load_zotero_dois() -> list[str]:
    with _zotero_lock:
        if not ZOTERO_FILE.exists():
            return []
        try:
            data = json.loads(ZOTERO_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return sorted(set(data.get("dois", [])))


def add_zotero_dois(dois: list[str]) -> list[str]:
    """追加记录。Zotero 里删掉了条目这里也不会知道，反正是给人看的筛选器。"""
    merged = sorted(set(load_zotero_dois()) | {d for d in dois if isinstance(d, str) and d})
    with _zotero_lock:
        ZOTERO_FILE.write_text(
            json.dumps({"dois": merged}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return merged


# --------------------------------------------------------------------------
# Zotero
# --------------------------------------------------------------------------


def to_zotero_item(a: dict) -> dict:
    """把页面里的一条文献转成 Zotero 的 item JSON。"""
    creators = []
    for person in a.get("authors") or []:
        if not (person.get("family") or person.get("given")):
            continue
        creator = {"creatorType": "author"}
        if person.get("family"):
            creator["lastName"] = person["family"]
            if person.get("given"):
                creator["firstName"] = person["given"]
        else:
            creator["name"] = person["given"]  # 机构作者
            creator["creatorType"] = "author"
            creator["fieldMode"] = 1
        creators.append(creator)

    item = {
        "itemType": "journalArticle",
        "title": a.get("title", ""),
        "creators": creators,
        "publicationTitle": a.get("journalName", ""),
        "DOI": a.get("doi", ""),
        "url": a.get("url", ""),
    }
    published = a.get("published") or ""
    if published:
        item["date"] = published
    if a.get("volume"):
        item["volume"] = str(a["volume"])
    if a.get("issue"):
        item["issue"] = str(a["issue"])
    if a.get("page"):
        item["pages"] = str(a["page"])
    if a.get("abstractCn") or a.get("abstract"):
        item["abstractNote"] = a.get("abstractCn") or a.get("abstract", "")
    return item


def save_to_zotero(items: list[dict]) -> int:
    """写进正在运行的 Zotero。返回成功入库的条数。"""
    if not items:
        return 0
    payload = json.dumps(
        {
            "sessionID": "papers-digest",
            "uri": items[0].get("url") or "https://doi.org/",
            "items": items,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        ZOTERO_SAVE,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"Zotero 返回 {response.status}")
    return len(items)


def zotero_alive() -> bool:
    try:
        with urlopen(ZOTERO_LIBRARY + "?limit=1", timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# 按需翻译
# --------------------------------------------------------------------------


def translate_one(doi: str, title: str, abstract: str) -> tuple[str, str]:
    """翻译单篇，并写进缓存，下次不用再翻。"""
    llm = pd.get_llm_config()
    with _translate_lock:  # ECNU 单模型并发上限 3，串行最稳
        title_cn, abstract_cn = pd.translate(title, abstract, llm)
    article = {
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "title_cn": title_cn,
        "abstract_cn": abstract_cn,
        "translated_by": llm["model"],
    }
    from datetime import datetime

    article["translated_at"] = datetime.now().isoformat(timespec="seconds")
    pd.write_cache(pd.CACHE_DIR, article)
    return title_cn, abstract_cn


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "papers-digest/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # 默认会把每个静态请求都打出来，太吵
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- 工具
    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def serve_file(self, relative: str) -> None:
        if not relative or relative == "/":
            relative = DEFAULT_PAGE
        # 防目录穿越
        target = (ROOT / unquote(relative).lstrip("/")).resolve()
        if not str(target).startswith(str(ROOT)) or target.suffix not in SERVABLE_SUFFIXES:
            self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_json(
                {"ok": False, "error": f"{relative} 不存在，先跑一次 papers_digest.py"},
                HTTPStatus.NOT_FOUND,
            )
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/json":
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "starred": load_marks(),
                    "zoteroDois": load_zotero_dois(),
                    "zotero": zotero_alive(),
                    "page": DEFAULT_PAGE,
                }
            )
            return
        self.serve_file(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = self.read_json()
        try:
            if path == "/api/translate":
                self.handle_translate(payload)
            elif path == "/api/mark":
                self.handle_mark(payload)
            elif path == "/api/zotero":
                self.handle_zotero(payload)
            else:
                self.send_json({"ok": False, "error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            print(f"  ! {path} 出错：{message}", flush=True)
            # 页面上放不下长报错，截一下；完整信息在终端里
            self.send_json(
                {"ok": False, "error": message[:120]},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    # ---------------------------------------------------------------- 处理
    def handle_translate(self, payload: dict) -> None:
        doi = (payload.get("doi") or "").strip()
        title = (payload.get("title") or "").strip()
        abstract = (payload.get("abstract") or "").strip()
        if not doi or not title:
            self.send_json({"ok": False, "error": "缺少 doi 或 title"}, HTTPStatus.BAD_REQUEST)
            return
        print(f"  按需翻译：{doi}")
        title_cn, abstract_cn = translate_one(doi, title, abstract)
        self.send_json({"ok": True, "titleCn": title_cn, "abstractCn": abstract_cn})

    def handle_mark(self, payload: dict) -> None:
        saved = save_marks(payload.get("starred") or [])
        self.send_json({"ok": True, "count": len(saved)})

    def handle_zotero(self, payload: dict) -> None:
        raw_items = payload.get("items") or []
        pairs = [
            (a.get("doi", ""), to_zotero_item(a)) for a in raw_items
        ]
        pairs = [(doi, item) for doi, item in pairs if item.get("title")]
        if not pairs:
            self.send_json({"ok": False, "error": "没有可入库的条目"}, HTTPStatus.BAD_REQUEST)
            return
        print(f"  存入 Zotero：{len(pairs)} 篇")
        added = save_to_zotero([item for _, item in pairs])
        # 记下来，页面上的「已加入 Zotero」筛选器靠它
        dois = add_zotero_dois([doi for doi, _ in pairs if doi])
        self.send_json({"ok": True, "added": added, "zoteroDois": dois})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="论文速览本地服务。")
    parser.add_argument("--port", type=int, default=8770, help="监听端口（默认 8770）")
    parser.add_argument(
        "--host", default="127.0.0.1", help="监听地址（默认只监听本机）"
    )
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not (ROOT / DEFAULT_PAGE).exists():
        print(f"提示：{DEFAULT_PAGE} 还不存在，先跑 python papers_digest.py --days 30")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"论文速览本地服务已启动：{url}")
    print(f"  想读标记存在 {MARKS_FILE.name}，翻译缓存在 {pd.CACHE_DIR.name}/")
    print("  Ctrl-C 停止")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

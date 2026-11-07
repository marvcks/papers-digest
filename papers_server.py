#!/usr/bin/env python3
"""论文速览的本地服务。

静态的 papers-digest.html 双击就能看，但「手动更新」「按需翻译」「想读标记」
这几个功能需要一个本地进程，因为：
- 浏览器从 file:// 打开的页面调不了 LLM 接口（跨域），也不该拿着你的 API key

只用标准库，没有额外依赖。

用法：
    python papers_server.py            # 起服务并打开浏览器
    python papers_server.py --port 8899 --no-open
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import papers_digest as pd
import news_digest as nd

ROOT = Path(__file__).resolve().parent
MARKS_FILE = ROOT / "papers-marks.json"
READING_FILE = ROOT / "papers-reading.json"
DEFAULT_PAGE = "papers-digest.html"

# 只允许访问这些后缀的文件，避免把 .env 之类的东西暴露出去
SERVABLE_SUFFIXES = {".html", ".css", ".js", ".json", ".png", ".svg"}

_marks_lock = threading.Lock()
_translate_lock = threading.Lock()
_reading_lock = threading.Lock()
_update_lock = threading.Lock()
_update_state = {"running": False, "message": "", "error": "", "startedAt": ""}
_news_job_lock = threading.Lock()
_news_job_state = {"running": False, "message": "", "error": ""}


def get_news_state() -> dict:
    with _news_job_lock:
        state = dict(_news_job_state)
    if not state["running"] and "result" not in state:
        state["result"] = pd.read_json_file(nd.STATUS_FILE)
    return state


def run_news_update() -> None:
    def progress(message):
        with _news_job_lock:
            _news_job_state["message"] = message

    try:
        result = nd.update_news(progress, llm_lock=_translate_lock)
        with _news_job_lock:
            _news_job_state.update(running=False, result=result,
                                   error=result.get("message", "") if result["state"] == "failed" else "")
    except Exception as exc:
        # Persist a failure without replacing a last good archive/page.
        result = {"state": "failed", "attempted_at": nd.now_iso(),
                  "message": "更新新闻失败，已保留上次内容"}
        pd.atomic_write_text(nd.STATUS_FILE, json.dumps(result, ensure_ascii=False))
        print(f"  新闻更新失败：{type(exc).__name__}", flush=True)
        with _news_job_lock:
            _news_job_state.update(running=False, result=result, error=result["message"])


def start_news_update() -> dict:
    with _news_job_lock:
        if _news_job_state["running"]:
            return {"started": False, **_news_job_state}
        _news_job_state.clear()
        _news_job_state.update(running=True, message="正在获取新闻…", error="", startedAt=nd.now_iso())
    threading.Thread(target=run_news_update, daemon=True).start()
    return {"started": True, **get_news_state()}


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
# 阅读记录
# --------------------------------------------------------------------------


def load_reading() -> dict:
    with _reading_lock:
        return pd.read_json_file(READING_FILE).get("articles", {})


def save_reading(updates: dict) -> dict:
    """只合并提交的 DOI，时间戳避免离线/多标签页旧记录覆盖新状态。"""
    if not isinstance(updates, dict) or len(updates) > 20000:
        raise ValueError("阅读记录格式不正确")
    cleaned = {}
    for doi, entry in updates.items():
        if not isinstance(doi, str) or not doi or not isinstance(entry, dict):
            raise ValueError("阅读记录缺少 DOI 或状态")
        if entry.get("status") not in {"unread", "browsed", "full"}:
            raise ValueError("不支持的阅读状态")
        timestamp = datetime.fromisoformat(entry.get("updatedAt", "").replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("阅读记录时间必须带时区")
        cleaned[doi] = {"status": entry["status"], "updatedAt": timestamp.isoformat()}
    with _reading_lock:
        records = pd.read_json_file(READING_FILE).get("articles", {})
        for doi, entry in cleaned.items():
            old = records.get(doi)
            if not old or datetime.fromisoformat(entry["updatedAt"]) > datetime.fromisoformat(
                old["updatedAt"].replace("Z", "+00:00")
            ):
                records[doi] = entry
        pd.atomic_write_text(READING_FILE, json.dumps({"schema": 1, "articles": records},
                                                    ensure_ascii=False, indent=2))
    return records


# --------------------------------------------------------------------------
# 按需翻译
# --------------------------------------------------------------------------


def persist_translation(article: dict) -> None:
    with pd.file_lock(pd.ARCHIVE_FILE.with_suffix(".lock")):
        archive = pd.load_archive()
        for stored in archive.get("articles", []):
            if stored["doi"] != article["doi"]:
                continue
            # 后台更新期间原文可能变了，不能把旧译文套到新原文上。
            if stored["title"] == article["title"] and article.get("title_cn"):
                stored["title_cn"] = article["title_cn"]
            if not stored.get("abstract") or stored["abstract"] == article.get("abstract"):
                for key in ("abstract", "abstract_cn", "abstract_source"):
                    stored[key] = article.get(key, "")
            break
        pd.atomic_write_text(pd.ARCHIVE_FILE, json.dumps(archive, ensure_ascii=False))


def translate_one(doi: str, part: str = "abstract") -> dict:
    """读取权威原文，按需翻译一种内容；缓存命中时不调用模型。"""
    if part not in {"title", "abstract"}:
        raise ValueError("不支持的翻译内容")
    with _translate_lock:  # 手动更新使用 2 workers，这里 1 个，总并发不超过 3。
        article = next((dict(a) for a in pd.load_archive().get("articles", []) if a["doi"] == doi), None)
        if not article:
            raise ValueError("历史库中没有这篇论文，请先更新文献")
        if part == "abstract" and not article.get("abstract"):
            with pd.requests.Session() as session:
                abstract = pd.fetch_openalex_abstract(doi, session)
            if abstract:
                article.update(abstract=abstract, abstract_source="openalex")
        entry = pd.read_cache_entry(pd.CACHE_DIR, doi)
        pd.apply_cache(article, {doi: entry})
        field = "title_cn" if part == "title" else "abstract_cn"
        cached = bool(article.get(field))
        if not cached and (part == "title" or article.get("abstract")):
            llm = pd.get_llm_config()
            if part == "title":
                article[field] = pd.translate_title(article["title"], llm)
            else:
                article[field] = pd.translate_abstract(article["abstract"], llm)
            article.update(translated_by=llm["model"],
                           translated_at=datetime.now().isoformat(timespec="seconds"))
            pd.write_cache(pd.CACHE_DIR, article, title_only=part == "title", abstract_only=part == "abstract")
        persist_translation(article)
        return {"titleCn": article.get("title_cn", ""), "abstract": article.get("abstract", ""),
                "abstractCn": article.get("abstract_cn", ""),
                "abstractSource": article.get("abstract_source", ""), "cached": cached}


def get_update_state() -> dict:
    with _update_lock:
        return dict(_update_state)


def record_update_failure(message: str) -> dict:
    result = pd.read_json_file(pd.UPDATE_FILE)
    if result.get("state") != "failed":
        result["failedJournals"] = []
    result.update(state="failed", message=message,
                  attemptedAt=datetime.now().astimezone().isoformat())
    pd.atomic_write_text(pd.UPDATE_FILE, json.dumps(result, ensure_ascii=False, indent=2))
    return result


def run_manual_update(date_range: dict | None = None) -> None:
    try:
        command = [sys.executable, "-u", str(ROOT / "papers_digest.py"),
                   "--days", "30", "--workers", "2"]
        if date_range:
            command.extend(["--start-date", date_range["startDate"],
                            "--end-date", date_range["endDate"]])
        with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8") as process:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                print(f"  更新文献：{line}", flush=True)
                with _update_lock:
                    _update_state["message"] = line[:240]
            returncode = process.wait()
        result = pd.read_json_file(pd.UPDATE_FILE)
        if returncode:
            result = record_update_failure(get_update_state()["message"] or "更新失败，保留原有论文")
        with _update_lock:
            _update_state.update(running=False, finishedAt=datetime.now().astimezone().isoformat(),
                                 result=result, succeeded=returncode == 0)
            if returncode:
                _update_state["error"] = _update_state["message"] or "更新失败，保留原有论文"
    except Exception as exc:
        message = str(exc)[:240]
        try:
            result = record_update_failure(message)
        except Exception:
            result = {"state": "failed", "message": message}
        with _update_lock:
            _update_state.update(running=False, succeeded=False, error=message, result=result,
                                 finishedAt=datetime.now().astimezone().isoformat())


def start_manual_update(payload: dict | None = None) -> dict:
    payload = payload or {}
    selected_range = pd.resolve_date_range(payload.get("startDate"), payload.get("endDate"))
    date_range = {"startDate": selected_range[0].isoformat(),
                  "endDate": selected_range[1].isoformat()} if selected_range else {}
    with _update_lock:
        if _update_state["running"]:
            return {"started": False, **_update_state}
        _update_state.clear()
        _update_state.update(running=True, message="正在抓取文献；仅翻译新标题",
                             error="", startedAt=datetime.now().astimezone().isoformat(),
                             **date_range)
    threading.Thread(target=run_manual_update, args=(date_range,), daemon=True).start()
    return {"started": True, **get_update_state()}


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
        root = ROOT.resolve()
        target = (root / unquote(relative).lstrip("/")).resolve()
        if not target.is_relative_to(root) or target.suffix not in SERVABLE_SUFFIXES:
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
        if path == "/api/news/update":
            self.send_json({"ok": True, **get_news_state()})
            return
        if path in {"/news", "/news-digest.html"}:
            if not nd.PAGE_FILE.exists():
                nd.render_page()  # Saved content only; never fetch or call an LLM.
            self.serve_file("news-digest.html")
            return
        if path == "/api/update":
            self.send_json({"ok": True, **get_update_state()})
            return
        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "starred": load_marks(),
                    "page": DEFAULT_PAGE,
                    "reading": load_reading(),
                    "update": pd.read_json_file(pd.UPDATE_FILE),
                    "updateJob": get_update_state(),
                }
            )
            return
        self.serve_file(path)

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlparse(origin)
                same_origin = (parsed.scheme == "http" and
                               parsed.hostname in {"127.0.0.1", "localhost", "::1"} and
                               parsed.port == self.server.server_port)
            except ValueError:
                same_origin = False
            if not same_origin:
                self.send_json({"ok": False, "error": "请从本地阅读页面操作"}, HTTPStatus.FORBIDDEN)
                return
        path = urlparse(self.path).path
        payload = self.read_json()
        try:
            if path == "/api/news/update":
                self.send_json({"ok": True, **start_news_update()}, HTTPStatus.ACCEPTED)
            elif path == "/api/translate":
                self.handle_translate(payload)
            elif path == "/api/mark":
                self.handle_mark(payload)
            elif path == "/api/reading":
                saved = save_reading(payload.get("updates", {}))
                self.send_json({"ok": True, "reading": saved})
            elif path == "/api/update":
                try:
                    job = start_manual_update(payload)
                except ValueError as exc:
                    self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self.send_json({"ok": True, **job}, HTTPStatus.ACCEPTED)
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
        if not doi:
            self.send_json({"ok": False, "error": "缺少 DOI"}, HTTPStatus.BAD_REQUEST)
            return
        print(f"  按需翻译：{doi}")
        self.send_json({"ok": True, **translate_one(doi, payload.get("part", "abstract"))})

    def handle_mark(self, payload: dict) -> None:
        saved = save_marks(payload.get("starred") or [])
        self.send_json({"ok": True, "count": len(saved)})



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

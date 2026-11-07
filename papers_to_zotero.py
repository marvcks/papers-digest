#!/usr/bin/env python3
"""Import starred Papers Digest articles using Zotero's local HTTP interfaces.

The default checks the library without importing anything. --import saves new
items and asks Zotero's OA/custom resolvers for a PDF, with publisher HTML as a
fallback. No browser, LLM, database writes, or old papers-zotero.json is used.

    python papers_to_zotero.py --days 7
    python papers_to_zotero.py --days 7 --import

Zotero must be running with its local API enabled. New items go to the currently
selected personal-library collection. Existing items are never recreated. An
existing item without a local PDF is reported separately for follow-up, since
Connector save sessions cannot attach files to arbitrary pre-existing items.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
TIMEZONE = ZoneInfo("Asia/Shanghai")
PDF_LIMIT = 100 * 1024 * 1024


def normalize_doi(value: str) -> str:
    value = unquote(value.strip()).lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
        raise ValueError(f"Invalid DOI: {value!r}")
    return value


def select_articles(archive: dict, marks: dict, days: int, today: date,
                    dois: list[str] | None = None) -> list[dict]:
    wanted = {normalize_doi(d) for d in (dois or marks.get("starred", []))}
    found = {}
    for article in archive.get("articles", []):
        doi = normalize_doi(article["doi"])
        if doi not in wanted:
            continue
        # Match the viewer's daysAgo <= days convention, including both ends.
        if not dois:
            try:
                published = date.fromisoformat(article.get("published", ""))
            except ValueError:
                continue
            if not today - timedelta(days=days) <= published <= today:
                continue
        found[doi] = dict(article, doi=doi)
    if dois and wanted - found.keys():
        raise ValueError("DOI missing from archive: " + ", ".join(sorted(wanted - found.keys())))
    return sorted(found.values(), key=lambda a: (a.get("published", ""), a["doi"]),
                  reverse=True)


def zotero_item(article: dict, connector_id: str) -> dict:
    creators = []
    for author in article.get("authors", []):
        if author.get("family"):
            creators.append({"creatorType": "author", "firstName": author.get("given", ""),
                             "lastName": author["family"]})
        elif author.get("name"):
            creators.append({"creatorType": "author", "lastName": author["name"],
                             "fieldMode": 1})
    return {"id": connector_id, "itemType": "journalArticle", "DOI": article["doi"],
            "title": article["title"], "creators": creators,
            "publicationTitle": article.get("journal_name", ""),
            "date": article.get("published", ""), "volume": article.get("volume", ""),
            "issue": article.get("issue", ""), "pages": article.get("page", ""),
            "url": "https://doi.org/" + article["doi"],
            "abstractNote": article.get("abstract", ""), "attachments": [], "tags": []}


class ZoteroClient:
    def __init__(self, base_url="http://127.0.0.1:23119", timeout=30):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError("Zotero URL must be a local HTTP origin")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        self.http.trust_env = False  # Keep loopback traffic out of system proxies.
        self.http.headers.update({"Zotero-API-Version": "3",
                                  "X-Zotero-Connector-API-Version": "3"})

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.base_url + path,
                                     timeout=self.timeout, allow_redirects=False, **kwargs)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise RuntimeError("Unexpected redirect from Zotero local API")
        return response

    def check(self, importing: bool):
        self.request("GET", "/connector/ping")
        self.request("GET", "/api/users/0/items", params={"limit": 1, "format": "json"})
        if importing:
            target = self.request("POST", "/connector/getSelectedCollection", json={}).json()
            # The reader below checks the personal library. Do not silently save
            # to a group library and then miss those entries on subsequent runs.
            if target.get("libraryID") != 1 or not target.get("filesEditable"):
                raise RuntimeError("Select an editable collection in My Library before importing")
            return target.get("name")
        return None

    def find_items(self, doi: str) -> list[dict]:
        matches = {}
        for start in range(0, 10000, 100):
            page = self.request("GET", "/api/users/0/items/top", params={
                "q": doi, "qmode": "everything", "format": "json",
                "limit": 100, "start": start}).json()
            for item in page:
                raw = item.get("data", {}).get("DOI", "")
                if raw and normalize_doi(raw) == doi and not item["data"].get("deleted"):
                    matches[item["key"]] = item
            if len(page) < 100:
                return list(matches.values())
        raise RuntimeError("Zotero search exceeded pagination limit; import stopped")

    def pdfs(self, item: dict) -> list[dict]:
        children = self.request("GET", f"/api/users/0/items/{item['key']}/children",
                                params={"format": "json", "limit": 100}).json()
        result = []
        for child in children:
            data = child["data"]
            if data.get("contentType") != "application/pdf" or data.get("deleted"):
                continue
            uri = child.get("links", {}).get("enclosure", {}).get("href", "")
            if not uri.startswith("file:"):
                uri = self.request("GET", f"/api/users/0/items/{child['key']}/file/view/url").text
            parsed = urlsplit(uri)
            record = {"key": child["key"], "verified": False}
            if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
                path = Path(unquote(parsed.path))
                record["path"] = str(path)
                try:
                    with path.open("rb") as file:
                        record["verified"] = file.read(5) == b"%PDF-"
                    record["bytes"] = path.stat().st_size
                except OSError as error:
                    record["error"] = str(error)
            result.append(record)
        return result

    def save(self, article: dict) -> tuple[str, str]:
        session_id, connector_id = str(uuid.uuid4()), str(uuid.uuid4())
        # Connector API v3 ignores attachment URLs in saveItems. Save metadata,
        # then explicitly request an OA/custom resolver or upload PDF bytes.
        self.request("POST", "/connector/saveItems", json={"sessionID": session_id,
                     "uri": "https://doi.org/" + article["doi"],
                     "items": [zotero_item(article, connector_id)]})
        return session_id, connector_id

    def resolve_pdf(self, session_id: str, connector_id: str):
        self.request("POST", "/connector/saveAttachmentFromResolver",
                     json={"sessionID": session_id, "itemID": connector_id})

    def upload_pdf(self, session_id: str, connector_id: str, url: str, content: bytes):
        if not content.startswith(b"%PDF-"):
            raise ValueError("Attachment is not a PDF")
        self.request("POST", "/connector/saveAttachment", data=content, headers={
            "Content-Type": "application/pdf",
            "X-Metadata": json.dumps({"sessionID": session_id, "parentItemID": connector_id,
                                      "title": "Full Text PDF", "url": url})})


def download_publisher_pdf(doi: str) -> tuple[str, bytes]:
    """Try publisher-provided PDF links; do not bypass login or anti-bot pages."""
    with requests.Session() as http:
        http.headers["User-Agent"] = "papers-to-zotero/1.0"
        with http.get("https://doi.org/" + doi, timeout=30, stream=True) as response:
            response.raise_for_status()
            html = read_bounded(response, 5 * 1024 * 1024)
            if html.startswith(b"%PDF-"):
                return response.url, html
            page_url = response.url
        soup = BeautifulSoup(html, "html.parser")
        links = [m.get("content", "") for m in soup.find_all(
            "meta", attrs={"name": re.compile(r"^citation_pdf_url$", re.I)})]
        # Follow explicit PDF links only, excluding supplementary material.
        for link in soup.find_all("a", href=True):
            label = " ".join([link.get_text(" ", strip=True), link.get("aria-label", "")])
            if (re.search(r"\b(?:download|open)\b.*\bpdf\b", label, re.I)
                    and not re.search(r"supplement|supporting", label, re.I)):
                links.append(link["href"])
        for url in dict.fromkeys(urljoin(page_url, link) for link in links if link):
            if urlsplit(url).scheme not in {"http", "https"}:
                continue
            try:
                with http.get(url, timeout=45, stream=True) as response:
                    response.raise_for_status()
                    content = read_bounded(response, PDF_LIMIT)
                    if content.startswith(b"%PDF-"):
                        return response.url, content
            except requests.RequestException:
                continue
        raise RuntimeError("No accessible publisher PDF; browser access may be required")


def read_bounded(response, limit: int) -> bytes:
    chunks, length = [], 0
    for chunk in response.iter_content(64 * 1024):
        length += len(chunk)
        if length > limit:
            raise ValueError("Response exceeds download size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def process_article(client: ZoteroClient, article: dict, importing: bool) -> dict:
    doi = article["doi"]
    result = {"doi": doi, "title": article["title"], "status": "error", "pdfs": []}
    items = client.find_items(doi)
    if len(items) > 1:
        return dict(result, status="existing_duplicates", keys=[i["key"] for i in items])
    if items:
        pdfs = client.pdfs(items[0])
        return dict(result, status="existing_pdf" if any(p["verified"] for p in pdfs)
                    else "existing_missing_pdf", key=items[0]["key"], pdfs=pdfs)
    if not importing:
        return dict(result, status="would_import")

    # Never automatically repeat a timed-out write: it may already have saved.
    # A later run will find the DOI above, rather than create a duplicate.
    session_id, connector_id = client.save(article)
    result["session_id"] = session_id
    errors = []
    try:
        client.resolve_pdf(session_id, connector_id)
    except (requests.RequestException, RuntimeError) as error:
        errors.append(str(error))
    items = client.find_items(doi)
    pdfs = client.pdfs(items[0]) if len(items) == 1 else []
    if not any(p["verified"] for p in pdfs):
        try:
            url, content = download_publisher_pdf(doi)
            client.upload_pdf(session_id, connector_id, url, content)
        except (requests.RequestException, RuntimeError, ValueError) as error:
            errors.append(str(error))
        items = client.find_items(doi)
        pdfs = client.pdfs(items[0]) if len(items) == 1 else []
    result.update(status="imported_pdf" if any(p["verified"] for p in pdfs)
                  else "imported_missing_pdf", pdfs=pdfs,
                  keys=[item["key"] for item in items], errors=errors)
    return result


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".zotero-report-", delete=False) as file:
        temporary = Path(file.name)
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="收藏文章的发布日期窗口，默认近7天")
    parser.add_argument("--doi", action="append", help="指定历史库 DOI，可重复；不受收藏/日期限制")
    parser.add_argument("--import", dest="importing", action="store_true", help="导入缺少的条目；默认只核验")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--zotero-url", default="http://127.0.0.1:23119")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.days < 0 or (args.limit is not None and args.limit < 1):
        parser.error("--days must be nonnegative and --limit must be positive")
    today = datetime.now(TIMEZONE).date()
    report_path = args.report or ROOT / "outputs" / f"zotero-script-{today}.json"
    started = time.monotonic()
    try:
        archive = json.loads((ROOT / "papers-archive.json").read_text(encoding="utf-8"))
        marks = json.loads((ROOT / "papers-marks.json").read_text(encoding="utf-8"))
        articles = select_articles(archive, marks, args.days, today, args.doi)
        if args.limit:
            articles = articles[:args.limit]
        client = ZoteroClient(args.zotero_url)
        # Coordinate simultaneous runs of this importer without touching Zotero's DB.
        with (ROOT / ".papers-to-zotero.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            target = client.check(args.importing)
            report = {"checked_at": datetime.now(TIMEZONE).isoformat(),
                      "mode": "import" if args.importing else "check", "target": target,
                      "days": args.days, "total": len(articles), "results": []}
            for index, article in enumerate(articles, 1):
                try:
                    result = process_article(client, article, args.importing)
                except (requests.RequestException, RuntimeError, ValueError, OSError) as error:
                    result = {"doi": article["doi"], "title": article["title"],
                              "status": "error", "error": str(error), "pdfs": []}
                report["results"].append(result)
                atomic_json(report_path, report)
                print(f"[{index}/{len(articles)}] {result['status']}: {article['doi']}")
            report["pdfs_verified"] = sum(any(p["verified"] for p in r["pdfs"])
                                           for r in report["results"])
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            atomic_json(report_path, report)
            print(f"PDF {report['pdfs_verified']}/{len(articles)}; report: {report_path}")
            good = {"existing_pdf", "imported_pdf"}
            if not args.importing:
                good.add("would_import")
            return 0 if all(r["status"] in good for r in report["results"]) else 1
    except (requests.RequestException, RuntimeError, ValueError, OSError) as error:
        print(f"Zotero import stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

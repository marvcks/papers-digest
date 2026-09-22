#!/usr/bin/env python3
"""
Generate yesterday's summaries for a Miniflux category.
Output format: title + link + Chinese summary in Markdown.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()


def get_miniflux_config():
    return {
        "url": os.getenv("MINIFLUX_URL", "http://localhost:8080").rstrip("/"),
        "username": os.getenv("MINIFLUX_USERNAME", "admin"),
        "password": os.getenv("MINIFLUX_PASSWORD", "test123"),
    }


def get_llm_config():
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    model_name = os.getenv("MODEL_NAME")
    if not all([api_key, base_url, model_name]):
        raise ValueError(
            "Missing LLM config. Set OPENAI_API_KEY, OPENAI_BASE_URL, MODEL_NAME."
        )
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model_name": model_name,
    }


def strip_html(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def parse_iso_datetime(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def get_category(config, category_name):
    auth = (config["username"], config["password"])
    resp = requests.get(f"{config['url']}/v1/categories", auth=auth, timeout=30)
    resp.raise_for_status()
    for category in resp.json():
        title = category.get("title", "")
        if title == category_name or title.lower() == category_name.lower():
            return category
    return None


def fetch_entries_for_yesterday(config, category_id, local_tz):
    auth = (config["username"], config["password"])

    now_local = datetime.now(local_tz)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start_local = today_start_local - timedelta(days=1)
    yesterday_start_utc = yesterday_start_local.astimezone(timezone.utc)

    params_base = {
        "category_id": category_id,
        "order": "published_at",
        "direction": "desc",
        "published_after": int(yesterday_start_utc.timestamp()),
        "limit": 100,
    }

    matched = []
    offset = 0

    while True:
        params = dict(params_base)
        params["offset"] = offset
        resp = requests.get(
            f"{config['url']}/v1/entries", params=params, auth=auth, timeout=30
        )
        resp.raise_for_status()
        entries = resp.json().get("entries", [])
        if not entries:
            break

        for entry in entries:
            published_at = entry.get("published_at")
            if not published_at:
                continue
            dt_local = parse_iso_datetime(published_at).astimezone(local_tz)
            if dt_local.date() == yesterday_start_local.date():
                matched.append(entry)

        if len(entries) < params_base["limit"]:
            break
        offset += params_base["limit"]

    matched.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    return matched, yesterday_start_local.date()


def summarize_in_chinese(llm_config, title, content, url):
    clean_content = strip_html(content)
    if len(clean_content) > 1800:
        clean_content = clean_content[:1800] + "..."

    prompt = f"""请基于以下博客内容写一个中文概括（2-3句话，简洁准确，不夸张）：

标题：{title}
链接：{url}
正文：{clean_content}
"""

    api_url = f"{llm_config['base_url']}/chat/completions"
    resp = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": llm_config["model_name"],
            "messages": [
                {"role": "system", "content": "你是一个专业且克制的中文科技编辑。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def write_markdown(entries, summaries, output_path, category_name, target_date):
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {category_name} 分类昨日文章汇总（{target_date}）",
        "",
        f"- 文章数：{len(entries)}",
        f"- 生成时间：{generated_at}",
        "",
        "---",
        "",
    ]

    for idx, (entry, summary) in enumerate(zip(entries, summaries), start=1):
        title = entry.get("title", "No Title")
        url = entry.get("url", "")
        lines.append(f"## {idx}. {title}")
        lines.append("")
        lines.append(f"- 链接：{url}")
        lines.append(f"- 中文概括：{summary}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate markdown summaries for yesterday's entries in a category"
    )
    parser.add_argument("--category", default="blogs", help="Miniflux category name")
    parser.add_argument("--output", help="Output markdown path")
    args = parser.parse_args()

    local_tz = datetime.now().astimezone().tzinfo
    if local_tz is None:
        local_tz = timezone.utc

    try:
        miniflux = get_miniflux_config()
        llm = get_llm_config()

        category = get_category(miniflux, args.category)
        if not category:
            raise ValueError(f"Category not found: {args.category}")

        entries, target_date = fetch_entries_for_yesterday(
            miniflux, category["id"], local_tz
        )
        if not entries:
            print(f"No entries found for {category['title']} on {target_date}.")
            return

        output = args.output or f"blogs-{target_date}.md"

        summaries = []
        total = len(entries)
        for i, entry in enumerate(entries, start=1):
            title = entry.get("title", "No Title")
            print(f"[{i}/{total}] Summarizing: {title[:80]}...")
            try:
                summary = summarize_in_chinese(
                    llm, title, entry.get("content", ""), entry.get("url", "")
                )
            except Exception as exc:
                summary = f"概括失败：{exc}"
            summaries.append(summary)

        write_markdown(entries, summaries, output, category["title"], target_date)
        print(f"Saved: {output}")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

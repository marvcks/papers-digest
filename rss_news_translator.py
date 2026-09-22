#!/usr/bin/env python3
"""
RSS News Translator
从 Miniflux 获取最近 24 小时的 RSS 新闻并翻译为中文
"""

import os
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup

# 加载 .env 文件
load_dotenv()


def get_miniflux_config():
    """获取 Miniflux 配置"""
    return {
        'url': os.getenv('MINIFLUX_URL', 'http://localhost:8080'),
        'username': os.getenv('MINIFLUX_USERNAME', 'admin'),
        'password': os.getenv('MINIFLUX_PASSWORD', 'test123'),
    }


def get_llm_config():
    """获取 LLM 配置"""
    api_key = os.getenv('OPENAI_API_KEY')
    base_url = os.getenv('OPENAI_BASE_URL')
    model_name = os.getenv('MODEL_NAME')

    if not all([api_key, base_url, model_name]):
        raise ValueError("缺少 LLM 配置，请检查 .env 文件中的 OPENAI_API_KEY, OPENAI_BASE_URL, MODEL_NAME")

    return {
        'api_key': api_key,
        'base_url': base_url,
        'model_name': model_name,
    }


def fetch_news(config, hours=24, limit=10, category_id=None, feed_id=None):
    """从 Miniflux 获取最近 N 小时的新闻"""
    auth = (config['username'], config['password'])
    base_url = config['url']

    # 计算 N 小时前的时间戳
    after_timestamp = int((datetime.now() - timedelta(hours=hours)).timestamp())

    # 构建查询参数
    params = {
        'status': 'unread',
        'published_after': after_timestamp,
        'order': 'published_at',
        'direction': 'desc',
        'limit': limit,
    }

    if category_id:
        params['category_id'] = category_id
    if feed_id:
        params['feed_id'] = feed_id

    response = requests.get(f'{base_url}/v1/entries', params=params, auth=auth)
    response.raise_for_status()

    data = response.json()
    return data.get('entries', [])


def strip_html(html_content):
    """移除 HTML 标签，保留纯文本"""
    if not html_content:
        return ''
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator=' ', strip=True)


def translate_news(title, content, url, llm_config):
    """使用 LLM 翻译新闻"""
    # 清理内容
    clean_content = strip_html(content)
    if len(clean_content) > 3000:
        clean_content = clean_content[:3000] + '...'

    prompt = f"""请将以下新闻翻译为中文。保留原文链接，翻译时保持专业性：

标题: {title}

原文内容:
{clean_content}

原文链接: {url}

请按以下格式输出：

## 标题
[翻译后的标题]

## 摘要
[翻译后的内容摘要，保持简洁]

## 原文链接
{url}
"""

    # 处理 base_url，确保没有重复的斜杠
    base_url = llm_config['base_url'].rstrip('/')
    api_url = f"{base_url}/chat/completions"

    try:
        response = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Content-Type": "application/json"
            },
            json={
                "model": llm_config['model_name'],
                "messages": [
                    {"role": "system", "content": "你是一个专业的新闻翻译助手，擅长将英文新闻翻译为简洁准确的中文。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3
            },
            timeout=60
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"翻译失败: {str(e)}"


def format_output(entries, translations):
    """格式化输出"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    output = [f"# 📰 RSS 新闻日报 - {datetime.now().strftime('%Y-%m-%d')}"]
    output.append("")
    output.append("## 📅 新闻概览")
    output.append(f"- 总计: {len(entries)} 篇")
    output.append(f"- 时间范围: 最近 24 小时")
    output.append(f"- 生成时间: {now}")
    output.append("")
    output.append("---")
    output.append("")

    # 按订阅源分组
    feeds = {}
    for entry, translation in zip(entries, translations):
        feed_title = entry.get('feed', {}).get('title', 'Unknown')
        if feed_title not in feeds:
            feeds[feed_title] = []
        feeds[feed_title].append((entry, translation))

    output.append("## 📰 新闻列表")
    output.append("")

    for feed_title, items in feeds.items():
        output.append(f"### 📡 {feed_title}")
        output.append("")

        for entry, translation in items:
            published_at = entry.get('published_at', '')
            if published_at:
                try:
                    dt = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
                    published_time = dt.strftime('%Y-%m-%d %H:%M')
                except:
                    published_time = published_at
            else:
                published_time = 'Unknown'

            output.append(f"#### {entry.get('title', 'No Title')}")
            output.append("")
            output.append(translation)
            output.append("")
            output.append(f"**发布时间**: {published_time}")
            output.append("")
            output.append("---")
            output.append("")

    return '\n'.join(output)


def main():
    parser = argparse.ArgumentParser(description='RSS 新闻翻译器')
    parser.add_argument('--hours', type=int, default=24, help='获取最近 N 小时的新闻 (默认: 24)')
    parser.add_argument('--limit', type=int, default=10, help='最多处理 N 篇文章 (默认: 10)')
    parser.add_argument('--category', type=int, help='只获取特定分类的新闻')
    parser.add_argument('--feed', type=int, help='只获取特定订阅源的新闻')
    parser.add_argument('--output', type=str, help='输出到文件 (默认: 根据日期自动生成)')
    parser.add_argument('--print', action='store_true', help='同时打印到终端')

    args = parser.parse_args()

    # 默认输出文件名：rss-news-YYYY-MM-DD.md
    if args.output is None:
        args.output = f"rss-news-{datetime.now().strftime('%Y-%m-%d')}.md"

    try:
        # 获取配置
        miniflux_config = get_miniflux_config()
        llm_config = get_llm_config()

        print(f"🔍 正在从 Miniflux 获取最近 {args.hours} 小时的新闻...")
        entries = fetch_news(miniflux_config, hours=args.hours, limit=args.limit,
                            category_id=args.category, feed_id=args.feed)

        if not entries:
            print("没有找到新闻。")
            return

        print(f"找到 {len(entries)} 篇新闻，开始翻译...")

        translations = []
        for i, entry in enumerate(entries, 1):
            title = entry.get('title', 'No Title')
            content = entry.get('content', '')
            url = entry.get('url', '')

            print(f"[{i}/{len(entries)}] 正在翻译: {title[:50]}...")
            translation = translate_news(title, content, url, llm_config)
            translations.append(translation)

        # 格式化输出
        output_content = format_output(entries, translations)

        # 保存到文件
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output_content)
        print(f"\n翻译完成！结果已保存到: {args.output}")

        # 如果指定了 --print，同时输出到终端
        if args.print:
            print("\n" + "="*60)
            print(output_content)

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

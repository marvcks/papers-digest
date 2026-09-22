#!/usr/bin/env python3
"""添加 JAV RSS 源到 Miniflux"""

import requests
from requests.auth import HTTPBasicAuth

# Miniflux 配置
MINIFLUX_URL = "http://localhost:8080/v1"
USERNAME = "admin"
PASSWORD = "test123"

# 要添加的 JAV RSS 源
JAV_FEEDS = [
    {
        "url": "https://rsshub.app/javlibrary/newrelease/en",
        "title": "JavLibrary 新发布",
    },
    {
        "url": "https://rsshub.app/javlibrary/update/en",
        "title": "JavLibrary 最近讨论",
    },
    {
        "url": "https://rsshub.app/javdb/rankings/daily",
        "title": "JavDB 日排行",
    },
    {
        "url": "https://sukebei.nyaa.si/?page=rss&q=JAV",
        "title": "Sukebei JAV 搜索",
    },
]


def get_or_create_category(category_name: str) -> int:
    """获取或创建分类，返回分类 ID"""
    auth = HTTPBasicAuth(USERNAME, PASSWORD)

    # 获取所有分类
    response = requests.get(f"{MINIFLUX_URL}/categories", auth=auth)
    response.raise_for_status()
    categories = response.json()

    # 查找目标分类
    for cat in categories:
        if cat["title"] == category_name:
            print(f"✓ 找到分类: {category_name} (ID: {cat['id']})")
            return cat["id"]

    # 创建新分类
    print(f"创建新分类: {category_name}")
    response = requests.post(
        f"{MINIFLUX_URL}/categories",
        auth=auth,
        json={"title": category_name},
    )
    response.raise_for_status()
    category = response.json()
    print(f"✓ 分类创建成功 (ID: {category['id']})")
    return category["id"]


def add_feed(url: str, title: str, category_id: int) -> bool:
    """添加订阅源"""
    auth = HTTPBasicAuth(USERNAME, PASSWORD)

    # 检查是否已存在
    response = requests.get(f"{MINIFLUX_URL}/feeds", auth=auth)
    response.raise_for_status()
    feeds = response.json()

    for feed in feeds:
        if feed["feed_url"] == url:
            print(f"  ⊗ 已存在: {title}")
            return False

    # 添加新源
    response = requests.post(
        f"{MINIFLUX_URL}/feeds",
        auth=auth,
        json={"feed_url": url, "category_id": category_id},
    )
    if response.status_code == 200:
        print(f"  ✓ 添加成功: {title}")
        return True
    else:
        print(f"  ✗ 添加失败: {title} - {response.text}")
        return False


def main():
    print("=" * 50)
    print("添加 JAV RSS 源到 Miniflux")
    print("=" * 50)

    # 获取或创建 jav 分类
    category_id = get_or_create_category("jav")
    print()

    # 添加所有源
    success_count = 0
    for feed in JAV_FEEDS:
        if add_feed(feed["url"], feed["title"], category_id):
            success_count += 1

    print()
    print(f"完成! 成功添加 {success_count}/{len(JAV_FEEDS)} 个源")
    print(f"访问 http://localhost:8080 查看结果")


if __name__ == "__main__":
    main()

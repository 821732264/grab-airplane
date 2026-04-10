#!/usr/bin/env python3
"""
海南航空"海航公告"监控 - Vercel Serverless Function
- 覆盖4个分类：出行提示、服务调整、信息通知、招标信息
- 仅处理最近 10 天内发布的公告
- 关键词匹配后通过钉钉机器人发送通知
- 使用 Vercel KV 存储去重
"""

import hashlib
import hmac
import base64
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

import requests
from bs4 import BeautifulSoup

# ==================== 配置区 ====================

BASE_URL = "https://www.hnair.com/guanyuhaihang/hhdt/hhgg/"
TARGET_URLS = {
    "出行提示": BASE_URL + "cxts/cxts2026/",
    "服务调整": BASE_URL + "fwtz/fwtz2026/",
    "信息通知": BASE_URL + "xxtz/xxtz2026/",
    "招标信息": BASE_URL + "zbxx/zbxx2026/",
}

RECENT_DAYS = 10

KEYWORDS = [
    "海航PLUS会员开放预定",
    "PLUS会员 预订航班",
    "会员 放票",
    "PLUS会员",
    "会员预定",
    "会员预订",
    "开放预定",
    "开放预订",
]

DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", "")

REQUEST_TIMEOUT = 30

# 使用内存存储（Vercel 每次调用都是新的实例）
# 实际生产环境建议使用 Vercel KV 或外部数据库
_notified_cache = set()

# ==================== 工具函数 ====================

def build_dingtalk_sign(secret: str) -> tuple:
    """生成钉钉加签参数"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def send_dingtalk_message(title: str, text: str):
    """通过钉钉机器人发送消息"""
    if not DINGTALK_WEBHOOK:
        print("钉钉 Webhook 未配置，跳过发送")
        return False

    webhook_url = DINGTALK_WEBHOOK

    if DINGTALK_SECRET:
        timestamp, sign = build_dingtalk_sign(DINGTALK_SECRET)
        sep = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        result = resp.json()
        if result.get("errcode") == 0:
            print("钉钉通知发送成功")
            return True
        else:
            print(f"钉钉通知发送失败: {result}")
            return False
    except Exception as e:
        print(f"钉钉通知发送异常: {e}")
        return False


def fetch_announcements() -> list:
    """抓取公告列表"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    cutoff_date = (datetime.now() - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    all_announcements = []

    for category, url in TARGET_URLS.items():
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception as e:
            print(f"[{category}] 抓取失败: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        news_boxes = soup.select("div.d-newsbox")

        for box in news_boxes:
            title_el = box.select_one("h5.d-mintit a")
            summary_el = box.select_one("p.d-text")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            summary = summary_el.get_text(strip=True) if summary_el else ""

            article_id = ""
            date_str = ""
            match = re.search(r"t(\d{8})_(\d+)\.html", href)
            if match:
                raw_date = match.group(1)
                article_id = match.group(2)
                date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

            if date_str and date_str < cutoff_date:
                continue

            if href.startswith("./"):
                full_url = url.rstrip("/") + "/" + href[2:]
            elif href.startswith("http"):
                full_url = href
            else:
                full_url = url.rstrip("/") + "/" + href

            all_announcements.append({
                "id": article_id or title,
                "title": title,
                "url": full_url,
                "summary": summary,
                "date": date_str,
                "category": category,
            })

    return all_announcements


def match_keywords(announcement: dict) -> list:
    """检查公告是否包含关键词"""
    text = f"{announcement['title']} {announcement['summary']}"
    matched = []
    for kw in KEYWORDS:
        parts = kw.split()
        if len(parts) > 1:
            if all(p in text for p in parts):
                matched.append(kw)
        else:
            if kw in text:
                matched.append(kw)
    return matched


def notify_announcement(announcement: dict):
    """发送钉钉通知"""
    keywords_str = "、".join(announcement["matched_keywords"])
    title = "海航公告关键词命中通知"
    text = (
        f"### {title}\n\n"
        f"**公告分类**: {announcement.get('category', '未知')}\n\n"
        f"**公告标题**: {announcement['title']}\n\n"
        f"**发布日期**: {announcement.get('date', '未知')}\n\n"
        f"**匹配关键词**: {keywords_str}\n\n"
        f"**公告摘要**: {announcement['summary'][:200]}\n\n"
        f"**公告链接**: [点击查看]({announcement['url']})\n\n"
        f"**检测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return send_dingtalk_message(title, text)


def run_monitor():
    """执行监控"""
    global _notified_cache

    print(f"开始抓取 {len(TARGET_URLS)} 个分类")

    try:
        announcements = fetch_announcements()
    except Exception as e:
        return {"error": f"抓取失败: {str(e)}"}

    if not announcements:
        return {"message": "未抓取到任何公告", "count": 0}

    matched_announcements = []
    new_notifications = []

    for ann in announcements:
        keywords_hit = match_keywords(ann)
        if keywords_hit:
            ann["matched_keywords"] = keywords_hit
            matched_announcements.append(ann)

            if ann["id"] not in _notified_cache:
                notify_announcement(ann)
                _notified_cache.add(ann["id"])
                new_notifications.append(ann["title"])

    return {
        "message": "监控完成",
        "total": len(announcements),
        "matched": len(matched_announcements),
        "new_notifications": new_notifications,
    }


# ==================== Vercel Handler ====================

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """处理 GET 请求（用于 Cron 触发）"""
        result = run_monitor()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

    def do_POST(self):
        """处理 POST 请求"""
        result = run_monitor()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

#!/usr/bin/env python3
"""
海南航空"海航公告"监控脚本
- 覆盖4个分类：出行提示、服务调整、信息通知、招标信息
- 每隔 5 分钟轮询所有分类页面
- 仅处理最近 10 天内发布的公告
- 关键词匹配后通过钉钉机器人发送通知
- 使用本地 JSON 文件去重 + 记录抓取日志
"""

import hashlib
import hmac
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ==================== 配置区 ====================

# 目标网址（4个分类的2026年页面）
BASE_URL = "https://www.hnair.com/guanyuhaihang/hhdt/hhgg/"
TARGET_URLS = {
    "出行提示": BASE_URL + "cxts/cxts2026/",
    "服务调整": BASE_URL + "fwtz/fwtz2026/",
    "信息通知": BASE_URL + "xxtz/xxtz2026/",
    "招标信息": BASE_URL + "zbxx/zbxx2026/",
}

# 只处理最近 N 天内发布的公告
RECENT_DAYS = 10

# 轮询间隔（秒），默认 5 分钟
POLL_INTERVAL = 5 * 60

# 关键词列表（只要标题或摘要中包含任意一个关键词，即视为命中）
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

# 钉钉机器人配置
# Webhook 地址（优先从环境变量读取，否则使用默认值）
DINGTALK_WEBHOOK = os.environ.get(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send?access_token=7f03e38fa8c9766ea22cd973077ef920d47f0d29e9b6540b9a40a3d3e874852b",
)
# 加签密钥
DINGTALK_SECRET = os.environ.get(
    "DINGTALK_SECRET",
    "SECb860539cfdd401eaaf6bd667d7c01e206f614dedc2ee532ce5b226e545472ba3",
)

# 数据存储目录（与脚本同目录）
DATA_DIR = Path(__file__).parent / "data"
# 已通知记录文件
NOTIFIED_FILE = DATA_DIR / "notified.json"
# 抓取日志文件
FETCH_LOG_FILE = DATA_DIR / "fetch_log.json"

# 请求超时（秒）
REQUEST_TIMEOUT = 30

# ==================== 日志配置 ====================

# 日志文件路径
LOG_FILE = Path(__file__).parent / "monitor.log"

# 清理超过7天的旧日志
def clean_old_logs():
    """删除超过7天的旧日志文件"""
    if LOG_FILE.exists():
        stat = LOG_FILE.stat()
        file_age_days = (datetime.now().timestamp() - stat.st_mtime) / (24 * 3600)
        if file_age_days > 7:
            try:
                LOG_FILE.unlink()
                print(f"已清理超过7天的旧日志文件: {LOG_FILE}")
            except OSError as e:
                print(f"清理旧日志失败: {e}")

# 启动时清理旧日志
clean_old_logs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ==================== 工具函数 ====================


def ensure_data_dir():
    """确保数据目录存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(filepath: Path) -> list:
    """加载 JSON 文件，不存在则返回空列表"""
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"读取 {filepath} 失败，将重置为空列表")
    return []


def save_json(filepath: Path, data: list):
    """保存数据到 JSON 文件"""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_notified_ids() -> set:
    """获取已通知的公告 ID 集合"""
    records = load_json(NOTIFIED_FILE)
    return {r["id"] for r in records if "id" in r}


def add_notified_record(announcement: dict):
    """添加已通知记录"""
    records = load_json(NOTIFIED_FILE)
    records.append(
        {
            "id": announcement["id"],
            "title": announcement["title"],
            "url": announcement["url"],
            "matched_keywords": announcement["matched_keywords"],
            "notified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_json(NOTIFIED_FILE, records)


def add_fetch_log(announcements: list, matched: list):
    """记录每次抓取日志"""
    logs = load_json(FETCH_LOG_FILE)
    # 只保留最近 500 条日志，避免文件无限增长
    if len(logs) > 500:
        logs = logs[-500:]
    logs.append(
        {
            "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_count": len(announcements),
            "titles": [a["title"] for a in announcements],
            "matched_count": len(matched),
            "matched_titles": [m["title"] for m in matched],
        }
    )
    save_json(FETCH_LOG_FILE, logs)


# ==================== 页面抓取 ====================


def fetch_announcements() -> list:
    """
    抓取所有分类页面，解析公告列表，仅返回最近 RECENT_DAYS 天内的公告。
    返回格式: [{"id": str, "title": str, "url": str, "summary": str, "date": str, "category": str}, ...]
    """
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
            logger.warning(f"[{category}] 抓取失败: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        news_boxes = soup.select("div.d-newsbox")

        count = 0
        for box in news_boxes:
            title_el = box.select_one("h5.d-mintit a")
            summary_el = box.select_one("p.d-text")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            summary = summary_el.get_text(strip=True) if summary_el else ""

            # 从 href 提取公告 ID 和日期
            # 格式: ./202603/t20260306_83231.html
            article_id = ""
            date_str = ""
            match = re.search(r"t(\d{8})_(\d+)\.html", href)
            if match:
                raw_date = match.group(1)  # 20260306
                article_id = match.group(2)  # 83231
                date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

            # 按日期过滤：仅保留最近 RECENT_DAYS 天的公告
            if date_str and date_str < cutoff_date:
                continue

            # 拼接完整 URL
            if href.startswith("./"):
                full_url = url.rstrip("/") + "/" + href[2:]
            elif href.startswith("http"):
                full_url = href
            else:
                full_url = url.rstrip("/") + "/" + href

            all_announcements.append(
                {
                    "id": article_id or title,
                    "title": title,
                    "url": full_url,
                    "summary": summary,
                    "date": date_str,
                    "category": category,
                }
            )
            count += 1

        logger.info(f"[{category}] 页面共 {len(news_boxes)} 条，最近{RECENT_DAYS}天内 {count} 条")

    return all_announcements


# ==================== 关键词匹配 ====================


def match_keywords(announcement: dict) -> list:
    """
    检查公告标题和摘要是否包含任意关键词。
    返回匹配到的关键词列表。
    """
    text = f"{announcement['title']} {announcement['summary']}"
    matched = []
    for kw in KEYWORDS:
        # 支持空格分隔的多关键词（AND 逻辑）
        parts = kw.split()
        if len(parts) > 1:
            if all(p in text for p in parts):
                matched.append(kw)
        else:
            if kw in text:
                matched.append(kw)
    return matched


# ==================== 钉钉通知 ====================


def build_dingtalk_sign(secret: str) -> tuple:
    """
    生成钉钉加签参数。
    返回 (timestamp, sign)
    """
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
    """
    通过钉钉机器人 Webhook 发送 Markdown 消息。
    """
    if not DINGTALK_WEBHOOK:
        logger.warning("钉钉 Webhook 未配置，跳过发送通知")
        return False

    webhook_url = DINGTALK_WEBHOOK

    # 如果配置了加签
    if DINGTALK_SECRET:
        timestamp, sign = build_dingtalk_sign(DINGTALK_SECRET)
        sep = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text + "\n\n@18381666195",
        },
        "at": {"atMobiles": ["18381666195"], "isAtAll": True},
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
            logger.info("钉钉通知发送成功")
            return True
        else:
            logger.error(f"钉钉通知发送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"钉钉通知发送异常: {e}")
        return False


def notify_announcement(announcement: dict):
    """构造并发送单条公告的钉钉通知"""
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
    send_dingtalk_message(title, text)


# ==================== 主循环 ====================


def run_once():
    """执行一次检测"""
    categories_str = "、".join(TARGET_URLS.keys())
    logger.info(f"开始抓取 [{categories_str}] 共 {len(TARGET_URLS)} 个分类")

    try:
        announcements = fetch_announcements()
    except Exception as e:
        logger.error(f"抓取失败: {e}")
        return

    if not announcements:
        logger.warning("未抓取到任何公告（最近%d天内）", RECENT_DAYS)
        return

    logger.info(f"最近{RECENT_DAYS}天内共 {len(announcements)} 条公告")

    notified_ids = get_notified_ids()
    matched_announcements = []

    for ann in announcements:
        keywords_hit = match_keywords(ann)
        if keywords_hit:
            ann["matched_keywords"] = keywords_hit
            matched_announcements.append(ann)

            if ann["id"] in notified_ids:
                logger.info(
                    f"[已通知-跳过] [{ann['category']}] {ann['title']} "
                    f"(ID: {ann['id']}, 关键词: {keywords_hit})"
                )
            else:
                logger.info(
                    f"[新命中-通知] [{ann['category']}] {ann['title']} "
                    f"(ID: {ann['id']}, 关键词: {keywords_hit})"
                )
                notify_announcement(ann)
                add_notified_record(ann)
        else:
            logger.info(f"  - [{ann['category']}] {ann['title']} (无匹配)")

    # 记录抓取日志
    add_fetch_log(announcements, matched_announcements)

    if not matched_announcements:
        logger.info("本轮无关键词命中")


def main():
    """主入口：循环轮询"""
    logger.info("=" * 60)
    logger.info("海航公告监控脚本启动")
    logger.info(f"监控分类: {list(TARGET_URLS.keys())}")
    logger.info(f"时间范围: 最近 {RECENT_DAYS} 天")
    logger.info(f"轮询间隔: {POLL_INTERVAL} 秒")
    logger.info(f"监控关键词: {KEYWORDS}")
    logger.info(f"钉钉 Webhook: {'已配置' if DINGTALK_WEBHOOK else '未配置'}")
    logger.info(f"钉钉加签: {'已配置' if DINGTALK_SECRET else '未配置'}")
    logger.info("=" * 60)

    if not DINGTALK_WEBHOOK:
        logger.warning(
            "警告: 钉钉 Webhook 未配置！请设置环境变量 DINGTALK_WEBHOOK"
        )
        logger.warning(
            "示例: export DINGTALK_WEBHOOK='https://oapi.dingtalk.com/robot/send?access_token=xxx'"
        )

    ensure_data_dir()

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("收到中断信号，脚本退出")
            sys.exit(0)
        except Exception as e:
            logger.error(f"运行异常: {e}", exc_info=True)

        logger.info(f"等待 {POLL_INTERVAL} 秒后进行下一轮检测...\n")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("收到中断信号，脚本退出")
            sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
测试钉钉通知链路
"""

import json
import os
from http.server import BaseHTTPRequestHandler

DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", "")


def send_test_message():
    """发送测试消息到钉钉"""
    import hashlib
    import hmac
    import base64
    import time
    import urllib.parse
    import requests

    if not DINGTALK_WEBHOOK:
        return {"error": "DINGTALK_WEBHOOK not configured"}

    webhook_url = DINGTALK_WEBHOOK

    # 加签
    if DINGTALK_SECRET:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
        hmac_code = hmac.new(
            DINGTALK_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "Vercel 部署测试",
            "text": "### 海航公告监控测试\n\n✅ Vercel 部署成功！\n\n- 部署时间：自动\n- 监控频率：每5分钟\n- 监控分类：出行提示、服务调整、信息通知、招标信息\n\n如有匹配关键词的公告，将立即通知。"
        }
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
            return {"success": True, "message": "测试消息已发送到钉钉"}
        else:
            return {"error": f"钉钉返回错误: {result}"}
    except Exception as e:
        return {"error": str(e)}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        result = send_test_message()

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

    def do_POST(self):
        self.do_GET()

#!/usr/bin/env python3
"""
海航机票价格监控脚本
- 通过海航 APP API (airLowFareSearch) 查询航班价格
- 同时监控普通最低价和 PLUS 会员专享价
- 当出现目标价格（如 199 元）机票时，通过钉钉机器人通知
- 支持多航线、多日期可配置
- 每 60 秒轮询一次
"""

import hashlib
import hmac
import base64
import json
import logging
import os
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

# ==================== 配置区 ====================

# 监控航线列表（可配置多条）
WATCH_ROUTES = [
    {
        "origin": "SZX",
        "destination": "HGH",
        "date": "2026-04-29",
        "origin_name": "深圳",
        "dest_name": "杭州",
    },
    {
        "origin": "CAN",
        "destination": "HGH",
        "date": "2026-04-29",
        "origin_name": "广州",
        "dest_name": "杭州",
    },
    {
        "origin": "HGH",
        "destination": "CAN",
        "date": "2026-05-07",
        "origin_name": "杭州",
        "dest_name": "广州",
    },
]

# 目标价格：任何航班出现该价格即通知（包括普通价和会员价）
TARGET_PRICE = 199

# 轮询间隔（秒），每 3 分钟查询一次
POLL_INTERVAL = 180

# 乘客信息
PASSENGER = "ADT:1,CNN:1,INF:1"

# ==================== APP API 配置 ====================

# 使用 airLowFareSearch 接口，返回详细票价产品（含 PLUS 会员专享价）
API_URL = "https://app.hnair.com/ticket/lfs/airLowFareSearch"
API_PARAMS = {
    "token": "4c5b7edca9e112636be421220f763d89_c5af640af880d5319235e616a254a857",
    "hnairSign": "A0A27700D664AB7678580BF253BEA5F5E305C310",
}

API_HEADERS = {
    "Host": "app.hnair.com",
    "Cookie": (
        "ekingCode=OGJnZDVUR1hvZ0xEQmVUORx0ayhqMbzjKtKCOhhShCWR/GXLuAYSwiu6ZzOhtoi2"
        "8QhCXkjfGeT8KerFyA7tYi/CiyvFwIG93al3QwxTPjC9B+UBBzI6PEIW6RhomCkN; "
        "8fe1e3514bafd525_gdp_cs1=gioenc-IT%5E113134140511543954111014; "
        "8fe1e3514bafd525_gdp_gio_id=gioenc-IT%5E113134140511543954111014; "
        "_ga=GA1.2.1074588932.1775648041; "
        "gdp_user_id=gioenc-aad2g076%2C137a%2C55b0%2C9b8a%2C3021e4824e60; "
        "abymg_id=1_0380E01F6FCAC53D15BF14BCCC3BC5FCF89926564317096CC9EC260AC783EA08"
    ),
    "content-type": "application/json",
    "accept": "*/*",
    "appver": "10.13.2",
    "ekingcode": (
        "b2NsNDhWODE0UFBxQXlIQlHfI9S7j/P7Mk8XEoz1efomGADtWayGDHgmN/bJvbC5"
        "EkB1bwp+MFQuwDq0Sk0+99ynF/grhSvHLbKZNJGX6PLnqZZeu7KTKZEKGFKb0Bzr"
    ),
    "accept-language": "zh-Hans-CN;q=1.0",
    "hna-app": "APP",
    "appstamp": (
        "F7gu8dhqNy/79l++DtOR6zGEZVDalVB+nzyGxd7haPgbgHVblSkcXPBoncxbJ9Ur"
        "N4ABoLD6FjJAMNCns09LoyAh6vcoVYIgmc2+ZBQqRFwVPSESNvoWTiO+y0caDGSF"
        "B9SSeb0Couh0c0FsLPmsrviqL3BlsP5mBCWICNSOSqU="
    ),
    "user-agent": "HNAApp/10.13.2 (com.hnair.mobile; build:25920; iOS 18.7.7) Alamofire/5.6.3",
    "hna-channel": "IP",
}

API_COMMON = {
    "sver": "18.7.7",
    "stime": "1775814967775",
    "gtcid": "edcca14e1e36d3b722c9907bb57fb284",
    "dname": "iPhone17,1",
    "abuild": "25920",
    "szone": "+0800",
    "riskToken": "69d8c01dyY0djDJ1hlYlTttfkvyiJcBpOreTP2x2",
    "slang": "zh_Hans_CN",
    "slng": "113.34892218412041",
    "did": "072EE7006388496DB6F6979212BCC6E7",
    "blackBox": "1775814713784IPHdNjI5cXw9e",
    "atarget": "standard",
    "akey": "F57531F4F0C84D6196DA1C79DC94D1D9",
    "aname": "com.hnair.mobile",
    "validateToken": "",
    "sname": "iOS",
    "aver": "10.13.2",
    "hver": "build-10.13.0.48186.aba23763e1.standard",
    "slat": "23.103260914400977",
    "schannel": "IP",
    "captchaToken": "",
    "mchannel": "appStore",
}

# ==================== 钉钉配置 ====================

DINGTALK_WEBHOOK = os.environ.get(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send?access_token=7f03e38fa8c9766ea22cd973077ef920d47f0d29e9b6540b9a40a3d3e874852b",
)
DINGTALK_SECRET = os.environ.get(
    "DINGTALK_SECRET",
    "SECb860539cfdd401eaaf6bd667d7c01e206f614dedc2ee532ce5b226e545472ba3",
)

# ==================== 数据存储 ====================

DATA_DIR = Path(__file__).parent / "data"
NOTIFIED_FILE = DATA_DIR / "ticket_notified.json"
FETCH_LOG_FILE = DATA_DIR / "ticket_fetch_log.json"

REQUEST_TIMEOUT = 30

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(__file__).parent / "ticket_monitor.log", encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# ==================== 工具函数 ====================


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(filepath: Path) -> list:
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning(f"读取 {filepath} 失败，将重置为空列表")
    return []


def save_json(filepath: Path, data: list):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_notified_ids() -> set:
    records = load_json(NOTIFIED_FILE)
    return {r["id"] for r in records if "id" in r}


def add_notified_record(record: dict):
    records = load_json(NOTIFIED_FILE)
    records.append(record)
    save_json(NOTIFIED_FILE, records)


def add_fetch_log(route: dict, flights: list, matched: list):
    logs = load_json(FETCH_LOG_FILE)
    if len(logs) > 1000:
        logs = logs[-1000:]
    logs.append(
        {
            "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "route": f"{route['origin_name']}->{route['dest_name']}",
            "date": route["date"],
            "total_flights": len(flights),
            "prices": [
                {
                    "flight": f.get("flight_no", ""),
                    "min_price": f.get("min_price", ""),
                    "member_price": f.get("member_price"),
                    "plus_price": f.get("plus_price"),
                }
                for f in flights
            ],
            "matched_count": len(matched),
        }
    )
    save_json(FETCH_LOG_FILE, logs)


def _safe_int(val, default=0):
    """安全转换为 int，处理空字符串和 None"""
    if val is None or val == "" or val is False:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ==================== API 调用 ====================


def query_flights(origin: str, destination: str, date: str) -> list:
    """
    调用海航 APP airLowFareSearch API 查询航班价格。
    返回每个航班的:
      - min_price: 最低普通价
      - member_price: 接口返回的 memberPrice（会员价，可能为空）
      - plus_price: 从 "PLUS会员专享" 票价产品中提取的成人价格
      - member: 是否有会员价标识
    """
    payload = {
        "data": {
            "originDestinations": [
                {
                    "destination": destination,
                    "origin": origin,
                    "destinationType": "1",
                    "originType": "1",
                    "departureDate": date,
                }
            ],
            "passenger": PASSENGER,
        },
        "common": API_COMMON,
    }

    url = API_URL + "?" + urllib.parse.urlencode(API_PARAMS)
    resp = requests.post(
        url,
        json=payload,
        headers=API_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    # 检查 API 是否返回成功
    if not data.get("data", {}).get("success"):
        error_msg = data.get("data", {}).get("message", "未知错误")
        raise RuntimeError(f"API 返回失败: {error_msg}")

    origin_dests = data.get("data", {}).get("originDestinations", [])
    if not origin_dests:
        return []

    itineraries = origin_dests[0].get("airItineraries", [])
    flights = []

    for it in itineraries:
        seg = it.get("flightSegments", [{}])[0]
        airline_code = seg.get("marketingAirlineCode", "")
        flight_number = seg.get("flightNumber", "")
        flight_no = f"{airline_code}{flight_number}"

        # 从 airItineraryPrices 中找 PLUS 会员专享价
        plus_price = None
        plus_fare_name = None
        for price_item in it.get("airItineraryPrices", []):
            fare_name = price_item.get("fareFamilyName", "")
            if "PLUS" in fare_name or "会员专享" in fare_name:
                # 提取成人基础票价
                for tp in price_item.get("travelerPrices", []):
                    if tp.get("travelerType") == "ADT":
                        p = _safe_int(tp.get("baseFare"))
                        if p > 0:
                            plus_price = p
                            plus_fare_name = fare_name
                        break
                if plus_price:
                    break

        # itinerary 级别的 memberPrice
        member_price_raw = it.get("memberPrice", "")
        member_price = _safe_int(member_price_raw) if member_price_raw else None

        flights.append(
            {
                "flight_no": flight_no,
                "origin": seg.get("originShortName", origin),
                "dest": seg.get("destinationShortName", destination),
                "date": seg.get("departureDate", date),
                "dep_time": seg.get("departureTime", ""),
                "arr_time": seg.get("arrivalTime", ""),
                "aircraft": seg.get("displayAircraftName", ""),
                "airline": seg.get("operatingAirlineName", ""),
                "min_price": _safe_int(it.get("minLowPrice", 0)),
                "economy_price": _safe_int(it.get("lowPriceY", 0)),
                "business_price": _safe_int(it.get("lowPriceC", 0)),
                "seats": it.get("inventoryQuantity", ""),
                "member": it.get("member", False),
                "member_price": member_price,
                "plus_price": plus_price,
                "plus_fare_name": plus_fare_name,
            }
        )

    return flights


# ==================== 钉钉通知 ====================


def build_dingtalk_sign(secret: str) -> tuple:
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
    if not DINGTALK_WEBHOOK:
        logger.warning("钉钉 Webhook 未配置，跳过发送通知")
        return False

    webhook_url = DINGTALK_WEBHOOK
    if DINGTALK_SECRET:
        timestamp, sign = build_dingtalk_sign(DINGTALK_SECRET)
        sep = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text + "\n\n@18381666195"},
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


def notify_ticket(flight: dict, route: dict, price_type: str, hit_price: int):
    """发送命中通知，price_type 为 'regular' / 'member' / 'plus'"""
    title = "海航特价机票通知"
    type_label = {
        "regular": "普通最低价",
        "member": "会员价(memberPrice)",
        "plus": f"PLUS会员专享({flight.get('plus_fare_name', '')})",
    }.get(price_type, price_type)

    text = (
        f"### {title}\n\n"
        f"**航线**: {route['origin_name']} → {route['dest_name']}\n\n"
        f"**航班**: {flight['flight_no']}\n\n"
        f"**日期**: {flight['date']}\n\n"
        f"**时间**: {flight['dep_time']} - {flight['arr_time']}\n\n"
        f"**机型**: {flight['aircraft']}\n\n"
        f"**命中价格**: ¥{hit_price}（{type_label}）\n\n"
        f"**普通最低价**: ¥{flight['min_price']}\n\n"
        f"**余票**: {flight['seats']}\n\n"
        f"**检测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_dingtalk_message(title, text)


# ==================== 主循环 ====================

# Token 过期提醒：记录上次提醒时间，避免每分钟都发
_last_token_alert_time = 0


def _notify_token_expired(route_name: str, error_msg: str):
    """Token 过期时通过钉钉提醒，每小时最多一次"""
    global _last_token_alert_time
    now = time.time()
    if now - _last_token_alert_time < 3600:
        return
    _last_token_alert_time = now

    title = "机票监控异常 - Token已过期"
    text = (
        f"### Token/Cookie 已过期\n\n"
        f"**航线**: {route_name}\n\n"
        f"**错误**: {error_msg[:200]}\n\n"
        f"**影响**: 机票价格监控已暂停，无法查询航班数据\n\n"
        f"**处理**: 请重新打开海航APP抓包，更新 ticket_monitor.py 中的 API_HEADERS 和 API_PARAMS\n\n"
        f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_dingtalk_message(title, text)


def check_price_match(flight: dict) -> list:
    """
    检查航班是否命中目标价格，返回命中列表:
    [{"type": "regular"/"member"/"plus", "price": int}, ...]
    """
    hits = []

    # 1. 检查普通最低价
    if flight["min_price"] == TARGET_PRICE:
        hits.append({"type": "regular", "price": flight["min_price"]})

    # 2. 检查 memberPrice（接口级会员价字段）
    if flight.get("member_price") and flight["member_price"] == TARGET_PRICE:
        hits.append({"type": "member", "price": flight["member_price"]})

    # 3. 检查 PLUS 会员专享产品的价格
    if flight.get("plus_price") and flight["plus_price"] == TARGET_PRICE:
        hits.append({"type": "plus", "price": flight["plus_price"]})

    return hits


def run_once():
    """执行一次检测"""
    notified_ids = get_notified_ids()

    for route in WATCH_ROUTES:
        route_name = f"{route['origin_name']}->{route['dest_name']} {route['date']}"
        logger.info(f"查询航线: {route_name}")

        try:
            flights = query_flights(route["origin"], route["destination"], route["date"])
        except Exception as e:
            error_str = str(e)
            logger.error(f"[{route_name}] 查询失败: {error_str}")
            if "401" in error_str or "403" in error_str or "token" in error_str.lower():
                logger.error(
                    "可能是 Token/Cookie 已过期，请重新抓包更新 API_HEADERS 和 API_PARAMS"
                )
                _notify_token_expired(route_name, error_str)
            continue

        if not flights:
            logger.warning(f"[{route_name}] 未查到航班")
            continue

        matched = []
        for f in flights:
            # 日志输出：显示普通价、会员价、PLUS价
            member_info = ""
            if f.get("member_price"):
                member_info += f" | 会员价:¥{f['member_price']}"
            if f.get("plus_price"):
                member_info += f" | PLUS价:¥{f['plus_price']}({f.get('plus_fare_name','')})"
            if f.get("member"):
                member_info += " | [会员标识]"

            logger.info(
                f"  {f['flight_no']} | {f['dep_time']}-{f['arr_time']} | "
                f"¥{f['min_price']}{member_info} | 余票:{f['seats']}"
            )

            # 检查是否命中目标价格
            hits = check_price_match(f)
            if hits:
                matched.append((f, hits))

        # 处理命中的航班
        for f, hits in matched:
            for hit in hits:
                notify_id = f"{f['flight_no']}_{f['date']}_{hit['type']}_{hit['price']}"
                if notify_id in notified_ids:
                    logger.info(
                        f"  [已通知-跳过] {f['flight_no']} {hit['type']}:¥{hit['price']}"
                    )
                else:
                    logger.info(
                        f"  [命中-通知] {f['flight_no']} {hit['type']}:¥{hit['price']}"
                    )
                    notify_ticket(f, route, hit["type"], hit["price"])
                    add_notified_record(
                        {
                            "id": notify_id,
                            "flight_no": f["flight_no"],
                            "date": f["date"],
                            "price": hit["price"],
                            "price_type": hit["type"],
                            "route": route_name,
                            "notified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    notified_ids.add(notify_id)

        # 记录抓取日志
        add_fetch_log(route, flights, [f for f, _ in matched])

        if not matched:
            prices = [f["min_price"] for f in flights]
            plus_prices = [f["plus_price"] for f in flights if f.get("plus_price")]
            info = f"共 {len(flights)} 个航班，最低 ¥{min(prices)}"
            if plus_prices:
                info += f"，PLUS最低 ¥{min(plus_prices)}"
            info += f"，未出现 ¥{TARGET_PRICE}"
            logger.info(f"[{route_name}] {info}")


def main():
    logger.info("=" * 60)
    logger.info("海航机票价格监控脚本启动")
    logger.info(f"接口: airLowFareSearch（支持会员价检测）")
    logger.info(f"目标价格: ¥{TARGET_PRICE}（同时监控普通价 + PLUS会员价）")
    logger.info(f"轮询间隔: {POLL_INTERVAL} 秒")
    logger.info(f"监控航线:")
    for r in WATCH_ROUTES:
        logger.info(f"  {r['origin_name']}({r['origin']}) -> {r['dest_name']}({r['destination']}) {r['date']}")
    logger.info(f"钉钉 Webhook: {'已配置' if DINGTALK_WEBHOOK else '未配置'}")
    logger.info("=" * 60)

    ensure_data_dir()

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("收到中断信号，脚本退出")
            sys.exit(0)
        except Exception as e:
            logger.error(f"运行异常: {e}", exc_info=True)

        logger.info(f"等待 {POLL_INTERVAL} 秒...\n")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("收到中断信号，脚本退出")
            sys.exit(0)


if __name__ == "__main__":
    main()

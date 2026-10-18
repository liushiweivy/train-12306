# -*- coding: utf-8 -*-
"""12306 余票查询与解析（纯逻辑 + 网络层分离，解析函数可脱离网络单测）。

关键实测（2026-10-17，北京->上海 queryZ 实捕）：
- 每条结果 58 个 `|` 分隔字段；字段 39 = yp_info，内含全部席别价格，
  无需再调任何价格接口（queryAllPublicPrice 已对匿名请求恒返「系统忙」）。
- yp_info 编码：每 10 字符一组 = 席别码(1) + 价格x10 的 5 位整数 + 4 位控制码；
  组序与字段 35(seat_types) 对应；同一席别可出现两组（第二组多为无座同价/折扣副本）。
- 席别可用票字段：21=高级软卧 23=软卧 24=软座 26=无座 28=硬卧 29=硬座
  30=二等座 31=一等座 32/25=商务/特等座 33=动卧；值为 数字/有/无/空串(无此席别)。
"""
from __future__ import annotations

import json
import time
import urllib.parse

import requests

# ---------------- 常量映射 ----------------

SEAT_NAMES = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
    "D": "优选一等座",
    "6": "高级软卧", "F": "动卧", "4": "软卧", "3": "硬卧",
    "2": "软座", "1": "硬座", "W": "无座",
}

# 席别码 -> 可用票字段下标（多下标表示官方不同年份字段漂移，取第一个非空）
# 字段 20=优选一等座：G3 实捕 seat_types=9MODO、field20=无 与官方售罄态一致
AVAIL_FIELDS = {
    "9": (32, 25), "P": (32, 25), "M": (31,), "O": (30,), "D": (20,),
    "6": (21,), "4": (23,), "3": (28,), "2": (24,),
    "1": (29,), "W": (26,), "F": (33,),
}

# 无座价回落链：无座通常与二等座/硬座同价
WUZUO_FALLBACK = ("O", "1")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

OTN = "https://kyfw.12306.cn/otn"
QUERY_ENDPOINTS = ("queryZ", "queryG", "queryA", "query")

MIN_FIELDS = 40  # 实测 58 字段；低于此视为坏行


# ---------------- 纯解析（可单测） ----------------

def seat_name(code: str) -> str:
    return SEAT_NAMES.get(code, "席别" + code)


def train_category(code: str) -> str:
    if not code:
        return "未知"
    c = code[0]
    if c in "GDC":
        return c
    if c in "KTZ":
        return c
    if c == "S":
        return "S"
    if c.isdigit():
        return "普快"
    return c if c.isalpha() else "未知"


def duration_minutes(dur: str) -> int:
    """'05:53' -> 353；非法输入返回 0。"""
    try:
        h, m = dur.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def arrival_day_offset(from_time: str, dur_min: int) -> int:
    """发车时刻 + 历时跨过的天数（次日到达返回 1）。"""
    try:
        h, m = from_time.split(":")
        return (int(h) * 60 + int(m) + dur_min) // (24 * 60)
    except (ValueError, AttributeError):
        return 0


def parse_yp_info(yp: str) -> list:
    """解码 yp_info -> [(席别码, 价格)]，保序。非法输入返回 []。"""
    if not yp or len(yp) < 10 or len(yp) % 10 != 0:
        return []
    out = []
    for i in range(0, len(yp), 10):
        group = yp[i:i + 10]
        seat, price_digits = group[0], group[1:6]
        if not price_digits.isdigit():
            return []
        out.append((seat, int(price_digits) / 10))
    return out


def _avail_of(fields: list, seat_code: str) -> str:
    for idx in AVAIL_FIELDS.get(seat_code, ()):  # 多候选下标取首个非空
        if idx < len(fields) and fields[idx] != "":
            return fields[idx]
    return ""


def parse_train_row(row: str, station_map: dict) -> dict:
    """解析一条 `|` 分隔的余票结果行为结构化字典。"""
    f = row.split("|")
    if len(f) < MIN_FIELDS:
        raise ValueError(f"字段数不足：{len(f)} < {MIN_FIELDS}")

    prices = parse_yp_info(f[39])
    price_by_seat = {}
    for seat, price in prices:  # 同席别多组取首个
        price_by_seat.setdefault(seat, price)

    seats = []
    seen = set()
    for seat in f[35]:  # seat_types 顺序即展示顺序
        if seat in seen or seat == "0":
            continue
        seen.add(seat)
        seats.append({
            "code": seat,
            "name": seat_name(seat),
            "price": price_by_seat.get(seat),
            "avail": _avail_of(f, seat),
        })

    # 无座：官方从不把它放进 seat_types，但字段 26 非空即有此席别可售/售罄态
    if f[26] != "" and "W" not in seen:
        wz_price = None
        for fb in WUZUO_FALLBACK:
            if fb in price_by_seat:
                wz_price = price_by_seat[fb]
                break
        seats.append({"code": "W", "name": "无座",
                      "price": wz_price, "avail": f[26]})

    dur = duration_minutes(f[10])
    return {
        "code": f[3],
        "trainNo": f[2],
        "category": train_category(f[3]),
        "fromStation": station_map.get(f[6], f[6]),
        "toStation": station_map.get(f[7], f[7]),
        "fromTelecode": f[6],
        "toTelecode": f[7],
        "fromTime": f[8],
        "toTime": f[9],
        "durationMin": dur,
        "dayOffset": arrival_day_offset(f[8], dur),
        "canBook": f[11] == "Y" and f[1] == "预订",
        "seats": seats,
    }


def parse_response(payload: dict) -> dict:
    """解析 queryZ 整个 JSON -> {trains, map}；官方 status=False 时抛 QueryError。"""
    if not payload.get("status"):
        msgs = payload.get("messages") or ["12306 返回失败"]
        raise QueryError(";".join(str(m) for m in msgs))
    data = payload["data"]
    station_map = data.get("map", {})
    trains = []
    for row in data.get("result", []):
        try:
            trains.append(parse_train_row(row, station_map))
        except ValueError:
            continue  # 坏行跳过，不拖垮整页
    return {"trains": trains, "count": len(trains)}


class QueryError(Exception):
    """12306 侧业务失败（限流/系统忙/参数错误）。"""


# ---------------- 网络层 ----------------

class RailClient:
    """带会话的 12306 查询客户端。一个实例复用 Cookie，失败自动重建会话。"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._session = None

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        # 先访问余票页拿 JSESSIONID 等 Cookie，否则查询被拒
        s.get(f"{OTN}/leftTicket/init", timeout=self.timeout)
        return s

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def query_trains(self, date: str, from_code: str, to_code: str) -> dict:
        """date=YYYY-MM-DD，from/to=车站电报码。端点轮换 + 会话失效重建重试一次。"""
        qs = urllib.parse.urlencode({
            "leftTicketDTO.train_date": date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": "ADULT",
        })
        hdr = {"Referer": f"{OTN}/leftTicket/init",
               "X-Requested-With": "XMLHttpRequest"}
        last_err = None
        for attempt in range(2):
            for ep in QUERY_ENDPOINTS:
                try:
                    r = self.session.get(f"{OTN}/leftTicket/{ep}?{qs}",
                                         timeout=self.timeout, headers=hdr)
                    if "json" not in r.headers.get("Content-Type", ""):
                        last_err = QueryError(f"{ep} 非 JSON 响应 HTTP {r.status_code}")
                        continue
                    payload = json.loads(r.text.lstrip("﻿"))  # 官方偶发 BOM
                    if payload.get("status"):
                        return parse_response(payload)
                    last_err = QueryError(";".join(payload.get("messages") or ["系统忙"]))
                    time.sleep(0.5)  # 系统忙时限流友好
                except (requests.RequestException, ValueError) as e:
                    last_err = QueryError(f"{ep} 请求异常: {e}")
            self._session = None  # 重建会话再试一轮
        raise last_err or QueryError("所有查询端点均失败")


# ---------------- 车站表 ----------------

def parse_station_name_js(text: str) -> list:
    """解析官方 station_name.js -> [{name, code, pinyin, abbr}]。
    格式：@bjb|北京北|VAP|beijingbei|bjb|0@bji|北京|BJP|beijing|bj|1@..."""
    text = text[text.find("@") + 1:]
    out = []
    for rec in text.split("@"):
        parts = rec.split("|")
        if len(parts) >= 6 and parts[2]:
            out.append({"name": parts[1], "code": parts[2],
                        "pinyin": parts[3], "abbr": parts[4]})
    return out


def fetch_stations(client_timeout: int = 20) -> list:
    r = requests.get(f"{OTN}/resources/js/framework/station_name.js",
                     headers={"User-Agent": UA}, timeout=client_timeout)
    r.encoding = "utf-8"
    stations = parse_station_name_js(r.text)
    if len(stations) < 1000:
        raise QueryError(f"车站表异常：仅 {len(stations)} 条")
    return stations

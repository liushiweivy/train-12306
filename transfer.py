# -*- coding: utf-8 -*-
"""中转方案引擎：12306 网页端「接续换乘」接口已失效（2026-10-17 探针验证，
lcquery 无论是否带 init cookie 都返回 HTML 验证页），改为自建枢纽换乘——
对 N 个核心铁路枢纽并行查 A→H、H→B 两段余票，按换乘窗口拼接排序。

拼接规则（同站换乘）：
  45 分钟 ≤ 第二段发车 - 第一段到达 ≤ 5 小时，当天到达当天走（不跨夜）。
排序：总耗时（A 发车 → B 到达）升序。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

# 全国高铁/普铁核心十字枢纽（电报码），覆盖主要流向的换乘点
HUBS = [
    ("郑州东", "ZAF"), ("武汉", "WHN"), ("徐州东", "UUH"), ("南京南", "NKH"),
    ("石家庄", "SJP"), ("济南西", "JGK"), ("西安北", "EAY"), ("合肥南", "ENH"),
    ("天津", "TJP"), ("长沙南", "CWQ"), ("南昌西", "NCG"), ("杭州东", "HGH"),
]

MIN_WAIT_MIN = 45    # 同站换乘最小缓冲（12306 官方建议 ≥40）
MAX_WAIT_MIN = 300   # 超过 5 小时的等待不作为推荐方案
MAX_PLANS = 10
LEG_CACHE_TTL = 1800  # 中转段缓存 30 分钟（班次稳定，余票略滞后可接受）


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def cheapest_price(train: dict) -> float | None:
    """该车次最便宜席别价（汇总总价用）。"""
    prices = [s["price"] for s in train.get("seats", []) if s.get("price")]
    return min(prices) if prices else None


def combine_transfers(legs_a: list, legs_b: list, hub_name: str,
                      min_wait: int = MIN_WAIT_MIN,
                      max_wait: int = MAX_WAIT_MIN) -> list:
    """A→H 车次表 × H→B 车次表 → 当天可衔接方案列表（未排序）。"""
    plans = []
    for t1 in legs_a:
        arr1 = _to_min(t1["toTime"]) + t1.get("dayOffset", 0) * 1440
        for t2 in legs_b:
            dep2 = _to_min(t2["fromTime"])
            wait = dep2 - arr1
            if not (min_wait <= wait <= max_wait):
                continue
            total = dep2 + t2["durationMin"] - _to_min(t1["fromTime"])
            p1, p2 = cheapest_price(t1), cheapest_price(t2)
            plans.append({
                "hub": hub_name,
                "leg1": t1, "leg2": t2,
                "waitMin": wait,
                "totalMin": total,
                "totalDayOffset": t1.get("dayOffset", 0) + t2.get("dayOffset", 0),
                "minTotalPrice": round(p1 + p2, 1) if p1 is not None and p2 is not None else None,
            })
    return plans


def find_transfers(query_leg, from_code: str, to_code: str, hubs: list,
                   max_workers: int = 6) -> dict:
    """并行查所有枢纽两段并拼接。

    query_leg: fn(from_code, to_code) -> list[train dict]，抛异常视为该段无车。
    hubs: [(站名, 电报码)]；与起终点相同的枢纽自动跳过。
    返回 {plans, searchedHubs, noTrainHubs, totalPlans}。
    """
    legs: dict = {}

    def fetch(code_pair):
        frm, to = code_pair
        try:
            return code_pair, query_leg(frm, to)
        except Exception:
            return code_pair, []

    active = [(n, c) for n, c in hubs if c not in (from_code, to_code)]
    tasks = set()
    for _, code in active:
        tasks.add((from_code, code))
        tasks.add((code, to_code))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(fetch, t) for t in tasks]
        for f in as_completed(futs):
            pair, trains = f.result()
            legs[pair] = trains

    plans, searched, failed = [], 0, []
    for name, code in active:
        a = legs.get((from_code, code), [])
        b = legs.get((code, to_code), [])
        if not a or not b:
            failed.append(name)
            continue
        searched += 1
        plans.extend(combine_transfers(a, b, name))

    plans.sort(key=lambda p: p["totalMin"])
    return {"plans": plans[:MAX_PLANS], "searchedHubs": searched,
            "noTrainHubs": failed, "totalPlans": len(plans)}

# -*- coding: utf-8 -*-
"""机票比价入口：主流 OTA 查询接口全部有反爬签名（2026-10-17 实测携程
/itinerary/api/12808/products 返回 432 风控、m.ctrip soa2 404），不硬刚——
生成预填好城市/日期的官方搜索深链，一键跳过去看实时票价。

城市码用携程三字母城市码（与 IATA 城市码基本一致）。
"""
from __future__ import annotations

import urllib.parse

CITY_CODES = {
    "北京": "BJS", "上海": "SHA", "广州": "CAN", "深圳": "SZX", "成都": "CTU",
    "重庆": "CKG", "杭州": "HGH", "西安": "SIA", "武汉": "WUH", "南京": "NKG",
    "天津": "TSN", "长沙": "CSX", "青岛": "TAO", "大连": "DLC", "厦门": "XMN",
    "昆明": "KMG", "哈尔滨": "HRB", "沈阳": "SHE", "郑州": "CGO", "济南": "TNA",
    "合肥": "HFE", "福州": "FOC", "南昌": "KHN", "贵阳": "KWE", "太原": "TYN",
    "石家庄": "SJW", "长春": "CGQ", "兰州": "LHW", "乌鲁木齐": "URC",
    "海口": "HAK", "三亚": "SYX", "宁波": "NGB", "无锡": "WUX", "苏州": "SZV",
    "佛山": "FUO", "东莞": "DGM", "温州": "WNZ", "泉州": "JJN", "烟台": "YNT",
    "珠海": "ZUH", "南宁": "NNG", "呼和浩特": "HET", "银川": "INC",
    "西宁": "XNN", "拉萨": "LXA", "洛阳": "LYA", "桂林": "KWL", "丽江": "LJG",
    "张家界": "DYG", "九寨沟": "JZH", "黄山": "TXN",
}


# 国际/港澳台热门城市（中文名 -> IATA 城市码）
INTL_CITY_CODES = {
    "香港": "HKG", "澳门": "MFM", "台北": "TPE", "高雄": "KHH",
    "东京": "TYO", "大阪": "OSA", "首尔": "SEL", "釜山": "PUS",
    "曼谷": "BKK", "新加坡": "SIN", "吉隆坡": "KUL", "普吉": "HKT",
    "巴厘岛": "DPS", "马尼拉": "MNL", "河内": "HAN", "暹粒": "SAI",
    "迪拜": "DXB", "多哈": "DOH", "伦敦": "LON", "巴黎": "PAR",
    "法兰克福": "FRA", "阿姆斯特丹": "AMS", "罗马": "ROM", "莫斯科": "MOW",
    "纽约": "NYC", "洛杉矶": "LAX", "旧金山": "SFO", "温哥华": "YVR",
    "多伦多": "YTO", "悉尼": "SYD", "墨尔本": "MEL", "奥克兰": "AKL",
}

_DOMESTIC_SUFFIXES = ("南", "北", "东", "西", "虹桥", "站")


def is_international(from_name: str, to_name: str) -> bool:
    """任一端在境外城市表即按国际线出链接。"""
    return (from_name in INTL_CITY_CODES) or (to_name in INTL_CITY_CODES)


def city_code(name: str) -> str | None:
    """车站名/城市名 -> 城市码。自动剥「北京南」这类站名后缀。"""
    name = (name or "").strip()
    if name in CITY_CODES:
        return CITY_CODES[name]
    if name in INTL_CITY_CODES:
        return INTL_CITY_CODES[name]
    for suffix in _DOMESTIC_SUFFIXES:
        if name.endswith(suffix) and name[:-len(suffix)] in CITY_CODES:
            return CITY_CODES[name[:-len(suffix)]]
    return None


def _skyscanner_month(fc: str, tc: str) -> dict:
    """天巡整月低价日历（国内/国际通用）：一页看全月每天最低价，高低价走势直观。"""
    return {"site": "天巡整月低价",
            "url": f"https://www.tianxun.com/transport/flights/{fc.lower()}/{tc.lower()}"
                   "/cheapest-month/?adultsv2=1&cabinclass=economy&ref=home"}


def flight_links(from_name: str, to_name: str, date: str) -> list:
    """生成各 OTA 预填深链。城市不支持时返回 []；含境外端点自动切国际线链接组。"""
    fc, tc = city_code(from_name), city_code(to_name)
    if not fc or not tc:
        return []
    q = urllib.parse.quote
    fl, tl = fc.lower(), tc.lower()

    if is_international(from_name, to_name):
        # 天巡日期段格式 YYMMDD
        skd = date[2:].replace("-", "")
        return [
            {"site": "携程国际", "url": f"https://flights.ctrip.com/online/list/oneway-{fl}-{tl}"
                                        f"?depdate={date}&cabin=y_s_c_f"},
            {"site": "去哪儿国际", "url": f"https://flight.qunar.com/site/oneway_list_inter.htm"
                                        f"?searchDepartureAirport={q(from_name)}"
                                        f"&searchArrivalAirport={q(to_name)}"
                                        f"&searchDepartureTime={date}&nextNDays=0"
                                        f"&startSearch=true&fromCode={fc}&toCode={tc}"
                                        f"&from=qunarindex"},
            {"site": "天巡比价", "url": f"https://www.tianxun.com/transport/flights/{fl}/{tl}/{skd}/"
                                        f"?adultsv2=1&cabinclass=economy&childrenv2="
                                        f"&inboundaltsenabled=false&outboundaltsenabled=false"
                                        f"&preferdirects=false&ref=home&rtn=0"},
            _skyscanner_month(fc, tc),
        ]

    return [
        {"site": "携程", "url": f"https://flights.ctrip.com/online/list/oneway-{fl}-{tl}"
                                f"?depdate={date}&cabin=y_s_c_f"},
        {"site": "去哪儿", "url": f"https://flight.qunar.com/site/oneway_list.htm"
                                  f"?searchDepartureAirport={q(from_name)}"
                                  f"&searchArrivalAirport={q(to_name)}"
                                  f"&searchDepartureTime={date}"
                                  f"&searchArrivalTime={date}&nextNDays=0&startSearch=true"
                                  f"&fromCode={fc}&toCode={tc}&from=qunarindex&lowestPrice=null"
                                  f"&favoriteCity=&showTotalPr=null"},
        {"site": "同程", "url": f"https://www.ly.com/flights/itinerary/oneway/{fl.lower()}-{tl.lower()}"
                                f"?date={date}"},
        _skyscanner_month(fc, tc),
        {"site": "航旅纵横", "url": "https://www.umetrip.com/gw/dist/index.html#/flight"},
    ]


def supported_cities() -> list:
    return sorted(list(CITY_CODES.keys()) + list(INTL_CITY_CODES.keys()))

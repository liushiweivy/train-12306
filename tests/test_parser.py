# -*- coding: utf-8 -*-
"""query12306 解析层单测：yp_info 解码 + 车次行解析 + 车型分类 + 历时计算。
样本是 2026-10-17 从 12306 queryZ 实捕的三条原始行（G547/1461/T109，北京->上海）。"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from query12306 import (  # noqa: E402
    parse_yp_info, parse_train_row, train_category, seat_name,
    duration_minutes, arrival_day_offset,
)

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "leftticket_bjp_shh.json")
                     .read_text(encoding="utf-8"))


class TestYpInfo(unittest.TestCase):
    """yp_info：每 10 字符一组 = 席别码(1) + 价格x10(5位) + 4 位控制码。"""

    def test_g547_highspeed(self):
        prices = parse_yp_info("9199500000M092900021O055300021O055303000")
        self.assertEqual(prices, [("9", 1995.0), ("M", 929.0),
                                  ("O", 553.0), ("O", 553.0)])

    def test_1461_slow_train_half_yuan(self):
        # 普快硬卧 283.5 / 软卧 455.5 / 硬座 156.5 —— 验证 .5 角位
        prices = parse_yp_info("3028350000404555000010156500211015653312")
        self.assertEqual(prices, [("3", 283.5), ("4", 455.5),
                                  ("1", 156.5), ("1", 156.5)])

    def test_t109(self):
        prices = parse_yp_info("101775001740476500013030450000")
        self.assertEqual(prices, [("1", 177.5), ("4", 476.5), ("3", 304.5)])

    def test_empty_and_garbage(self):
        self.assertEqual(parse_yp_info(""), [])
        self.assertEqual(parse_yp_info("0"), [])
        self.assertEqual(parse_yp_info("XYZ123456"), [])


class TestTrainRow(unittest.TestCase):
    def _row(self, code):
        return parse_train_row(FIXTURE[code],
                               {"BJP": "北京", "SHH": "上海",
                                "VNP": "北京南", "AOH": "上海虹桥",
                                "FTP": "北京丰台", "IMH": "上海松江"})

    def test_g547_basics(self):
        t = self._row("G547")
        self.assertEqual(t["code"], "G547")
        self.assertEqual(t["trainNo"], "240000G54700")
        self.assertEqual(t["fromTime"], "06:18")
        self.assertEqual(t["toTime"], "12:11")
        self.assertEqual(t["durationMin"], 353)
        self.assertTrue(t["canBook"])
        self.assertEqual(t["category"], "G")
        # 站名从 map 解析为中文
        self.assertTrue(t["fromStation"].endswith(("南", "京", "台")))

    def test_g547_seats_prices_and_availability(self):
        t = self._row("G547")
        seats = {s["name"]: s for s in t["seats"]}
        self.assertEqual(seats["商务座"]["price"], 1995.0)
        self.assertEqual(seats["一等座"]["price"], 929.0)
        self.assertEqual(seats["二等座"]["price"], 553.0)
        # 字段 30=二等=有, 31=一等=有, 32=商务=无
        self.assertEqual(seats["二等座"]["avail"], "有")
        self.assertEqual(seats["一等座"]["avail"], "有")
        self.assertEqual(seats["商务座"]["avail"], "无")
        # 字段 26 非空 → 生成无座行（G 车售罄态），价回落二等座
        self.assertEqual(seats["无座"]["avail"], "无")
        self.assertEqual(seats["无座"]["price"], 553.0)

    def test_1461_wuzuo_price_fallback(self):
        t = self._row("1461")
        seats = {s["name"]: s for s in t["seats"]}
        self.assertEqual(seats["硬座"]["price"], 156.5)
        self.assertEqual(seats["硬卧"]["price"], 283.5)
        self.assertEqual(seats["软卧"]["price"], 455.5)
        # seat_types "3411" 第二个 1 是重复席别（去重保留首个价）
        names = [s["name"] for s in t["seats"]]
        self.assertEqual(names.count("硬座"), 1)
        # 字段 26=无座=有 → 无座行，价 = 硬座价
        self.assertEqual(seats["无座"]["avail"], "有")
        self.assertEqual(seats["无座"]["price"], 156.5)

    def test_1461_availability_fields(self):
        t = self._row("1461")
        seats = {s["name"]: s for s in t["seats"]}
        self.assertEqual(seats["软卧"]["avail"], "无")   # 字段 23
        self.assertEqual(seats["硬卧"]["avail"], "无")   # 字段 28
        self.assertEqual(seats["硬座"]["avail"], "有")   # 字段 29

    def test_t109_numeric_availability(self):
        t = self._row("T109")
        seats = {s["name"]: s for s in t["seats"]}
        self.assertEqual(seats["软卧"]["avail"], "1")
        self.assertEqual(seats["硬座"]["avail"], "17")
        self.assertEqual(seats["硬卧"]["avail"], "无")

    def test_g3_premium_first_class_seat_d(self):
        # G3 seat_types=9MODO：D=优选一等座（field20 可用票），二等座出现两组去重
        t = self._row("G3")
        seats = {s["name"]: s for s in t["seats"]}
        self.assertEqual(seats["优选一等座"]["price"], 1468.0)
        self.assertEqual(seats["优选一等座"]["avail"], "无")
        self.assertEqual([s["name"] for s in t["seats"]].count("二等座"), 1)

    def test_row_minimal_required_fields(self):
        # 不足字段数的行应抛 ValueError 而非静默出错
        with self.assertRaises(ValueError):
            parse_train_row("a|b|c", {})


class TestHelpers(unittest.TestCase):
    def test_train_category(self):
        self.assertEqual(train_category("G547"), "G")
        self.assertEqual(train_category("D310"), "D")
        self.assertEqual(train_category("C2001"), "C")
        self.assertEqual(train_category("K101"), "K")
        self.assertEqual(train_category("T109"), "T")
        self.assertEqual(train_category("Z281"), "Z")
        self.assertEqual(train_category("1461"), "普快")
        self.assertEqual(train_category("S501"), "S")

    def test_seat_name(self):
        self.assertEqual(seat_name("9"), "商务座")
        self.assertEqual(seat_name("M"), "一等座")
        self.assertEqual(seat_name("O"), "二等座")
        self.assertEqual(seat_name("4"), "软卧")
        self.assertEqual(seat_name("3"), "硬卧")
        self.assertEqual(seat_name("1"), "硬座")
        self.assertEqual(seat_name("W"), "无座")
        self.assertEqual(seat_name("6"), "高级软卧")
        self.assertEqual(seat_name("F"), "动卧")
        self.assertEqual(seat_name("2"), "软座")
        self.assertEqual(seat_name("P"), "特等座")
        self.assertEqual(seat_name("?"), "席别?")

    def test_duration(self):
        self.assertEqual(duration_minutes("05:53"), 353)
        self.assertEqual(duration_minutes("00:41"), 41)
        self.assertEqual(duration_minutes("24:00"), 1440)

    def test_arrival_day_offset(self):
        self.assertEqual(arrival_day_offset("06:18", 353), 0)
        self.assertEqual(arrival_day_offset("19:33", 927), 1)   # 次日 11:00
        self.assertEqual(arrival_day_offset("22:00", 1800), 2)  # 第三日


if __name__ == "__main__":
    unittest.main()

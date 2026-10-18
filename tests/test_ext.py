# -*- coding: utf-8 -*-
"""db 缓存 / transfer 中转 / flights 深链 三块扩展的测试。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import Cache  # noqa: E402
from transfer import combine_transfers, find_transfers  # noqa: E402
from flights import city_code, flight_links, is_international, supported_cities  # noqa: E402


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.cache = Cache("sqlite", sqlite_path=Path(self.tmp.name))

    def test_put_get_roundtrip(self):
        self.cache.put("k1", {"a": 1, "中文": "值"}, ttl_sec=60)
        self.assertEqual(self.cache.get("k1"), {"a": 1, "中文": "值"})

    def test_miss_and_expiry(self):
        self.assertIsNone(self.cache.get("nope"))
        self.cache.put("k2", {"x": 1}, ttl_sec=-1)  # 已过期
        self.assertIsNone(self.cache.get("k2"))

    def test_purge_and_stats(self):
        self.cache.put("live", {"x": 1}, ttl_sec=60)
        self.cache.put("dead", {"x": 1}, ttl_sec=-5)
        self.assertEqual(self.cache.purge_expired(), 1)
        st = self.cache.stats()
        self.assertEqual(st["backend"], "sqlite")
        self.assertEqual(st["entries"], 1)


def _train(code, frm, to, dur, price, day=0):
    return {"code": code, "fromTime": frm, "toTime": to, "durationMin": dur,
            "dayOffset": day, "seats": [{"name": "二等座", "price": price, "avail": "有"}]}


class TestTransfer(unittest.TestCase):
    def test_combine_window_filter(self):
        legs_a = [_train("G1", "08:00", "10:00", 120, 100.0)]
        legs_b = [
            _train("G2", "10:30", "12:00", 90, 200.0),   # 等 30min < 45 拒
            _train("G3", "11:00", "13:00", 120, 200.0),  # 等 60min 收
            _train("G4", "16:30", "18:00", 90, 200.0),   # 等 6.5h > 5h 拒
        ]
        plans = combine_transfers(legs_a, legs_b, "郑州东")
        self.assertEqual(len(plans), 1)
        p = plans[0]
        self.assertEqual(p["leg2"]["code"], "G3")
        self.assertEqual(p["waitMin"], 60)
        self.assertEqual(p["totalMin"], 300)  # 08:00 -> 13:00
        self.assertEqual(p["minTotalPrice"], 300.0)

    def test_combine_leg1_arrives_next_day(self):
        legs_a = [_train("K1", "20:00", "02:00", 360, 80.0, day=1)]
        legs_b = [_train("G9", "06:00", "08:00", 120, 150.0)]
        # K1 次日 02:00 到 = 1560min；G9 当天 06:00 发 → 衔接不上（负等待）
        self.assertEqual(combine_transfers(legs_a, legs_b, "武汉"), [])

    def test_find_transfers_parallel(self):
        table = {
            ("AAA", "HUB1"): [_train("G1", "08:00", "10:00", 120, 100.0)],
            ("HUB1", "BBB"): [_train("G2", "11:00", "13:00", 120, 200.0)],
            ("AAA", "HUB2"): [_train("G3", "09:00", "10:30", 90, 100.0)],
            ("HUB2", "BBB"): [_train("G4", "11:30", "12:30", 60, 200.0)],
        }

        def fake_leg(frm, to):
            if frm == "AAA" and to == "HUB3":
                raise RuntimeError("12306 系统忙")  # 异常按无车处理
            return table.get((frm, to), [])

        res = find_transfers(fake_leg, "AAA", "BBB",
                             [("枢纽一", "HUB1"), ("枢纽二", "HUB2"),
                              ("枢纽三", "HUB3"), ("起点", "AAA")])
        self.assertEqual(res["searchedHubs"], 2)
        self.assertIn("枢纽三", res["noTrainHubs"])
        self.assertEqual(res["totalPlans"], 2)
        # 按总耗时升序：G3->G4 共 210min 排前
        self.assertEqual(res["plans"][0]["leg1"]["code"], "G3")
        self.assertEqual(res["plans"][0]["totalMin"], 210)


class TestFlights(unittest.TestCase):
    def test_city_code(self):
        self.assertEqual(city_code("北京"), "BJS")
        self.assertEqual(city_code("北京南"), "BJS")
        self.assertEqual(city_code("上海虹桥"), "SHA")
        self.assertIsNone(city_code("铁岭"))

    def test_links(self):
        links = flight_links("北京", "上海", "2026-10-21")
        self.assertGreaterEqual(len(links), 4)
        ctrip = links[0]["url"]
        self.assertIn("oneway-bjs-sha", ctrip)
        self.assertIn("depdate=2026-10-21", ctrip)
        # 低价日历链接（高低价走势入口）
        month = [l for l in links if l["site"] == "天巡整月低价"]
        self.assertEqual(len(month), 1)
        self.assertIn("cheapest-month", month[0]["url"])
        self.assertEqual(flight_links("铁岭", "上海", "2026-10-21"), [])

    def test_international(self):
        self.assertEqual(city_code("东京"), "TYO")
        self.assertEqual(city_code("香港"), "HKG")
        self.assertTrue(is_international("北京", "东京"))
        self.assertFalse(is_international("北京", "上海"))
        links = flight_links("北京", "东京", "2026-11-05")
        self.assertEqual(len(links), 4)
        sites = [l["site"] for l in links]
        self.assertIn("天巡整月低价", sites)
        self.assertIn("去哪儿国际", sites)
        self.assertIn("天巡比价", sites)
        urls = " ".join(l["url"] for l in links)
        self.assertIn("oneway_list_inter", urls)     # 去哪儿国际线专用页
        self.assertIn("261105", urls)                # 天巡 YYMMDD 日期段
        self.assertNotIn("oneway_list.htm?", urls)   # 不得混入国内线链接
        self.assertIn("东京", supported_cities())
        self.assertIn("北京", supported_cities())


if __name__ == "__main__":
    unittest.main()

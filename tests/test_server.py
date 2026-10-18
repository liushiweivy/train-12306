# -*- coding: utf-8 -*-
"""server.py 集成测试：假 RailClient + 随机端口起真 HTTP 服务，验证路由/校验/缓存。"""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import create_server, resolve_station, validate_date  # noqa: E402

STATIONS = [
    {"name": "北京", "code": "BJP", "pinyin": "beijing", "abbr": "bj"},
    {"name": "北京南", "code": "VNP", "pinyin": "beijingnan", "abbr": "bjn"},
    {"name": "上海", "code": "SHH", "pinyin": "shanghai", "abbr": "sh"},
    {"name": "上海虹桥", "code": "AOH", "pinyin": "shanghaihongqiao", "abbr": "shhq"},
]


class FakeClient:
    def __init__(self):
        self.calls = []

    def query_trains(self, date, frm, to):
        self.calls.append((date, frm, to))
        # 方向感知的假车次：去程 08:00->10:00，回程 11:00->13:00，保证中转可拼接
        if frm == "BJP":
            t = {"code": "G1", "fromTime": "08:00", "toTime": "10:00",
                 "durationMin": 120, "dayOffset": 0, "canBook": True,
                 "seats": [{"name": "二等座", "price": 100.0, "avail": "有"}]}
        else:
            t = {"code": "G2", "fromTime": "11:00", "toTime": "13:00",
                 "durationMin": 120, "dayOffset": 0, "canBook": True,
                 "seats": [{"name": "二等座", "price": 200.0, "avail": "有"}]}
        return {"trains": [t], "count": 1}


class TestValidation(unittest.TestCase):
    TODAY = date(2026, 10, 17)

    def test_valid_date(self):
        self.assertEqual(validate_date("2026-10-20", self.TODAY), "2026-10-20")

    def test_bad_format(self):
        for bad in ("", "2026/10/20", "2026-1-1", "abc", "2026-13-01", "2026-10-32"):
            with self.assertRaises(ValueError):
                validate_date(bad, self.TODAY)

    def test_past_and_beyond_presale(self):
        with self.assertRaises(ValueError):
            validate_date("2026-10-16", self.TODAY)  # 昨天
        with self.assertRaises(ValueError):
            validate_date("2026-11-01", self.TODAY)  # 超 15 天预售期
        self.assertEqual(validate_date("2026-10-31", self.TODAY), "2026-10-31")

    def test_resolve_station(self):
        self.assertEqual(resolve_station("北京", STATIONS)["code"], "BJP")
        self.assertEqual(resolve_station(" 上海 ", STATIONS)["code"], "SHH")
        self.assertEqual(resolve_station("VNP", STATIONS)["name"], "北京南")
        with self.assertRaises(ValueError):
            resolve_station("", STATIONS)
        with self.assertRaises(ValueError):
            resolve_station("XXX", STATIONS)
        try:
            resolve_station("北", STATIONS)  # 非精确名但可模糊命中
            self.fail("应抛 ValueError")
        except ValueError as e:
            self.assertIn("你是不是想找", str(e))  # 给出模糊候选
            self.assertIn("北京", str(e))


class ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        from db import Cache
        cls.client = FakeClient()
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        cache = Cache("sqlite", sqlite_path=Path(cls._tmp.name))
        cls.httpd = create_server(port=0, client=cls.client, stations=STATIONS,
                                  cache=cache, client_factory=lambda: cls.client)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        # URL 中的中文 query 需百分号编码，urllib 不接受裸非 ASCII
        url = f"http://127.0.0.1:{self.port}" + urllib.parse.quote(path, safe="/?=&")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class TestHttpRoutes(ServerTestBase):
    def test_health(self):
        code, body = self.get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["stations"], 4)

    def test_stations(self):
        code, body = self.get("/api/stations")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["data"]), 4)
        self.assertEqual(body["data"][0]["name"], "北京")

    def test_query_validation_errors(self):
        code, body = self.get("/api/query?from=北京&to=上海&date=2020-01-01")
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        code, body = self.get("/api/query?from=不存在站&to=上海&date=2099-01-01")
        self.assertIn(code, (400,))  # 日期先挂或站名先挂都是 400

    def test_query_ok_and_cache(self):
        from datetime import datetime, timedelta
        d = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        before = len(self.client.calls)
        code, body = self.get(f"/api/query?from=北京&to=上海&date={d}")
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["count"], 1)
        self.assertFalse(body["data"]["cached"])
        self.assertEqual(body["data"]["from"], "北京")
        # 第二次命中缓存，不再打 12306
        code, body2 = self.get(f"/api/query?from=BJP&to=SHH&date={d}")
        self.assertTrue(body2["data"]["cached"])
        self.assertEqual(len(self.client.calls), before + 1)

    def test_transfer_ok_and_cache(self):
        from datetime import datetime, timedelta
        d = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        code, body = self.get(f"/api/transfer?from=北京&to=上海&date={d}")
        self.assertEqual(code, 200)
        data = body["data"]
        self.assertFalse(data["cached"])
        self.assertEqual(data["searchedHubs"], 12)  # HUBS 全命中（假 client 任意两段都有车）
        self.assertGreater(len(data["plans"]), 0)
        p = data["plans"][0]
        self.assertEqual(p["leg1"]["code"], "G1")
        self.assertEqual(p["leg2"]["code"], "G2")
        self.assertEqual(p["waitMin"], 60)
        self.assertEqual(p["minTotalPrice"], 300.0)
        # 第二次命中整体缓存
        _, body2 = self.get(f"/api/transfer?from=BJP&to=SHH&date={d}")
        self.assertTrue(body2["data"]["cached"])

    def test_flights_links(self):
        code, body = self.get("/api/flights?from=北京&to=上海&date=2026-12-01")
        self.assertEqual(code, 200)
        links = body["data"]["links"]
        self.assertGreaterEqual(len(links), 3)
        self.assertIn("depdate=2026-12-01", links[0]["url"])

    def test_flights_validation(self):
        code, _ = self.get("/api/flights?from=铁岭&to=上海&date=2026-12-01")
        self.assertEqual(code, 400)  # 不支持的机场城市
        code, _ = self.get("/api/flights?from=北京&to=&date=2026-12-01")
        self.assertEqual(code, 400)
        code, _ = self.get("/api/flights?from=北京&to=上海&date=2020-01-01")
        self.assertEqual(code, 400)  # 过去日期

    def test_index_and_404(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=10) as r:
            html = r.read().decode("utf-8")
            self.assertEqual(r.status, 200)
            self.assertIn("<html", html.lower())
        code, _ = self.get("/api/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()

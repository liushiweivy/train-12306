# -*- coding: utf-8 -*-
"""train-12306 后端：stdlib http.server，绑定 127.0.0.1:8831。

路由：
  GET /               -> static/index.html（单文件前端）
  GET /api/health     -> {ok, stations, cacheBackend, uptime}
  GET /api/stations   -> 车站表（启动时拉取并落盘缓存）
  GET /api/query      -> 直达车次+席别价格余票（DB 缓存 10 分钟，重启不失效）
  GET /api/transfer   -> 中转方案（枢纽并行两段查询+拼接，整体缓存 30 分钟）
  GET /api/flights    -> 机票比价深链（OTA 接口全有反爬签名，深链最稳）

车站/日期/查询层全部在 query12306.py；中转拼接在 transfer.py；机票在 flights.py。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from datetime import date as date_cls
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from db import Cache, create_cache
from flights import flight_links
from query12306 import QueryError, RailClient, fetch_stations
from transfer import HUBS, find_transfers

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
STATION_CACHE_FILE = ROOT / "stations.json"

HOST = "127.0.0.1"
PORT = 8831
QUERY_CACHE_TTL = 600     # 直达/中转段共用缓存 10 分钟（官方余票本身分钟级延迟）
TRANSFER_CACHE_TTL = 1800  # 拼接结果缓存 30 分钟
MAX_QUERY_AHEAD_DAYS = 14  # 12306 预售期 15 天（含当天）
MAX_FLIGHT_AHEAD_DAYS = 330
MAX_NAME_LEN = 20

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"^[A-Z]{3}$")


# ---------------- 纯逻辑（可单测） ----------------

def validate_date(s: str, today: date_cls | None = None,
                  max_ahead: int = MAX_QUERY_AHEAD_DAYS) -> str:
    """校验乘车日期，合法则原样返回，否则抛 ValueError(中文原因)。"""
    today = today or date_cls.today()
    if not DATE_RE.match(s or ""):
        raise ValueError("日期格式须为 YYYY-MM-DD")
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("日期不存在")
    if d < today:
        raise ValueError("不能查询过去日期")
    if d > today + timedelta(days=max_ahead):
        raise ValueError(f"超出可查询范围（最晚 {today + timedelta(days=max_ahead)}）")
    return s


def resolve_station(q: str, stations: list) -> dict:
    """车站名/电报码 -> 车站条目。找不到抛 ValueError，附带候选提示。"""
    q = (q or "").strip()
    if not q:
        raise ValueError("车站不能为空")
    if len(q) > MAX_NAME_LEN:
        raise ValueError("车站名过长")
    if CODE_RE.match(q):
        for st in stations:
            if st["code"] == q:
                return st
        raise ValueError(f"未知电报码 {q}")
    exact = [st for st in stations if st["name"] == q]
    if exact:
        return exact[0]
    hint = [st["name"] for st in stations if q in st["name"]][:5]
    suffix = f"，你是不是想找：{'、'.join(hint)}" if hint else ""
    raise ValueError(f"找不到车站「{q}」{suffix}")


# ---------------- HTTP 层 ----------------

def make_handler(client: RailClient, stations_provider, started_at: float,
                 cache: Cache, client_factory=None):
    """stations_provider: () -> list，失败可抛异常（延迟加载车站表）。
    client_factory: 为中转并行线程造独立会话客户端（测试可注入假 client）。"""
    client_lock = threading.Lock()
    fresh_factory = client_factory or (lambda: RailClient(timeout=8))

    def cached_query(date: str, frm: str, to: str,
                     fresh_client: bool = False) -> dict:
        """带 DB 缓存的余票查询。fresh_client=True 用独立会话（中转线程用）。"""
        ck = f"q|{date}|{frm}|{to}"
        hit = cache.get(ck)
        if hit is not None:
            return hit
        if fresh_client:
            data = fresh_factory().query_trains(date, frm, to)
        else:
            with client_lock:
                data = client.query_trains(date, frm, to)
        cache.put(ck, data, QUERY_CACHE_TTL)
        return data

    class Handler(BaseHTTPRequestHandler):
        server_version = "train12306/1.1"

        # ---- 工具 ----
        def _json(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, data, code: int = 200):
            self._json(code, {"ok": True, "data": data, "error": None})

        def _err(self, code: int, msg: str):
            self._json(code, {"ok": False, "data": None, "error": msg})

        def log_message(self, fmt, *args):  # 静默默认访问日志，保持控制台干净
            pass

        def _resolve_route(self, params, max_ahead: int = MAX_QUERY_AHEAD_DAYS):
            """解析 date/from/to 三参数 -> (date, frm_station, to_station)。"""
            date = validate_date((params.get("date") or [""])[0], max_ahead=max_ahead)
            stations = stations_provider()
            frm = resolve_station((params.get("from") or [""])[0], stations)
            to = resolve_station((params.get("to") or [""])[0], stations)
            return date, frm, to

        # ---- 路由 ----
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path, params = parsed.path, urllib.parse.parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    return self._index()
                if path == "/api/health":
                    return self._health()
                if path == "/api/stations":
                    return self._stations()
                if path == "/api/query":
                    return self._query(params)
                if path == "/api/transfer":
                    return self._transfer(params)
                if path == "/api/flights":
                    return self._flights(params)
                return self._err(404, "not found")
            except BrokenPipeError:
                pass  # 前端中途断开，忽略
            except Exception as e:  # 兜底：任何未预期异常不暴露堆栈
                return self._err(500, f"服务器内部错误: {type(e).__name__}")

        def _index(self):
            f = STATIC_DIR / "index.html"
            if not f.exists():
                return self._err(404, "static/index.html 缺失")
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _health(self):
            try:
                n = len(stations_provider())
            except Exception:
                n = 0
            st = cache.stats()
            self._ok({"stations": n, "cacheBackend": st["backend"],
                      "cacheEntries": st["entries"],
                      "uptimeSec": int(time.time() - started_at)})

        def _stations(self):
            try:
                self._ok(stations_provider())
            except Exception as e:
                self._err(502, f"车站表不可用: {e}")

        def _query(self, params):
            t0 = time.time()
            try:
                date, frm, to = self._resolve_route(params)
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(502, f"车站表不可用: {e}")

            ck = f"q|{date}|{frm['code']}|{to['code']}"
            was_cached = cache.get(ck) is not None
            try:
                data = dict(cached_query(date, frm["code"], to["code"]))
            except QueryError as e:
                return self._err(502, f"12306 查询失败：{e}")
            data.update({"cached": was_cached, "date": date,
                         "from": frm["name"], "to": to["name"],
                         "elapsedMs": int((time.time() - t0) * 1000)})
            self._ok(data)

        def _transfer(self, params):
            t0 = time.time()
            try:
                date, frm, to = self._resolve_route(params)
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(502, f"车站表不可用: {e}")

            tk = f"t|{date}|{frm['code']}|{to['code']}"
            hit = cache.get(tk)
            if hit is not None:
                hit = dict(hit)
                hit["cached"] = True
                hit["elapsedMs"] = int((time.time() - t0) * 1000)
                return self._ok(hit)

            def leg(f_code, t_code):
                # 每线程独立 RailClient（requests.Session 不保证跨线程安全）
                return cached_query(date, f_code, t_code,
                                    fresh_client=True)["trains"]

            try:
                res = find_transfers(leg, frm["code"], to["code"], HUBS)
            except Exception as e:
                return self._err(502, f"中转查询失败：{e}")
            res.update({"date": date, "from": frm["name"], "to": to["name"]})
            cache.put(tk, res, TRANSFER_CACHE_TTL)
            res = dict(res)
            res["cached"] = False
            res["elapsedMs"] = int((time.time() - t0) * 1000)
            self._ok(res)

        def _flights(self, params):
            try:
                date = validate_date((params.get("date") or [""])[0],
                                     max_ahead=MAX_FLIGHT_AHEAD_DAYS)
            except ValueError as e:
                return self._err(400, str(e))
            frm = (params.get("from") or [""])[0].strip()
            to = (params.get("to") or [""])[0].strip()
            if not frm or not to or len(frm) > MAX_NAME_LEN or len(to) > MAX_NAME_LEN:
                return self._err(400, "请填写有效的出发/到达城市")
            links = flight_links(frm, to, date)
            if not links:
                return self._err(400, f"暂不支持「{frm}」或「{to}」的机场城市，"
                                      "请换成大中城市名（如 北京、成都）")
            self._ok({"date": date, "from": frm, "to": to, "links": links})

    return Handler


# ---------------- 车站表加载（网络 + 落盘缓存双保险） ----------------

def load_stations() -> list:
    """优先实时拉官方车站表；失败时回落到本地缓存文件；再失败抛异常。"""
    try:
        stations = fetch_stations()
        STATION_CACHE_FILE.write_text(
            json.dumps(stations, ensure_ascii=False), encoding="utf-8")
        return stations
    except Exception as e:
        if STATION_CACHE_FILE.exists():
            stations = json.loads(STATION_CACHE_FILE.read_text(encoding="utf-8"))
            if stations:
                print(f"[warn] 实时车站表失败({e})，用本地缓存 {len(stations)} 条")
                return stations
        raise


def create_server(port: int = PORT, client: RailClient | None = None,
                  stations: list | None = None,
                  cache: Cache | None = None,
                  client_factory=None) -> ThreadingHTTPServer:
    """stations/cache/client_factory 传入则跳过网络加载与真实客户端（测试注入用）。"""
    stations = stations if stations is not None else load_stations()
    cache = cache or create_cache()
    provider = lambda: stations  # noqa: E731
    rail = client or RailClient()
    httpd = ThreadingHTTPServer((HOST, port),
                                make_handler(rail, provider, time.time(), cache,
                                             client_factory=client_factory))
    httpd.rail_client = rail  # 供启动预热取真实会话
    return httpd


def main():
    print("加载车站表...")
    httpd = create_server()
    # 预热 12306 会话 Cookie，首个用户查询省掉 init 往返
    threading.Thread(target=lambda: httpd.rail_client.session, daemon=True).start()
    print(f"train-12306 就绪: http://{HOST}:{PORT}  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

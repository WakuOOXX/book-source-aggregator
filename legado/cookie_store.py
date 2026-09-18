# -*- coding: utf-8 -*-
"""运行时 Cookie 库(参照 Legado CookieStore):响应 Set-Cookie 自动按域保存,
请求前按域取出合并 —— 会话跨线程、跨次运行维持,登录一次后不过期。

存储:sqlite(shuyuan/cookies.db,gitignore 内)+ 内存镜像。读多写少:
内存即时生效,落盘由定时线程批量刷(默认 10s)与 atexit 兜底,避免 384
并发下每响应一次磁盘写。
"""
import atexit
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def db_path() -> Path:
    """cookie 库路径:BOOKDL_COOKIE_DB 环境变量 > 当前数据目录/shuyuan/cookies.db。

    动态解析(每次调用),数据目录热迁移(core.config.set_data_dir)后
    自动跟随新目录;不缓存,避免 import 顺序造成路径固化。
    """
    env = (os.environ.get("BOOKDL_COOKIE_DB") or "").strip()
    if env:
        return Path(env)
    from core import config
    return config.DATA_DIR / "shuyuan" / "cookies.db"

_lock = threading.Lock()
_mem = {}            # domain(小写 host) -> {name: value}
_loaded = False
_flush_evt = threading.Event()
_started = False


def _load():
    global _loaded
    with _lock:
        if _loaded:
            return
        try:
            con = sqlite3.connect(str(db_path()))
            try:
                con.execute("CREATE TABLE IF NOT EXISTS cookie("
                            "domain TEXT PRIMARY KEY, cookies TEXT, updated REAL)")
                for dom, ck, _ in con.execute("SELECT domain, cookies, updated FROM cookie"):
                    try:
                        _mem[dom] = json.loads(ck)
                    except Exception:
                        pass
            finally:
                con.close()
        except Exception:
            pass                       # 库损坏/目录只读 → 退化为纯内存模式
        _loaded = True


def _flush_loop():
    while not _flush_evt.wait(10.0):
        try:
            flush()
        except Exception:
            pass


def _ensure_bg():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    _load()
    threading.Thread(target=_flush_loop, daemon=True).start()
    atexit.register(flush)


def _match(host, dom):
    """标准 Cookie 域匹配:host 与 dom 相等或为其子域。"""
    return host == dom or host.endswith("." + dom)


def domain_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def get_for(url):
    """取出对该 URL 生效的全部运行时 Cookie(host 与父域合并, 越具体越优先)。"""
    _ensure_bg()
    host = domain_of(url)
    if not host:
        return {}
    doms = sorted((d for d in _mem if _match(host, d)),
                  key=lambda d: d.count("."))          # 父域先合, 具体域覆盖
    out = {}
    with _lock:
        for d in doms:
            out.update(_mem.get(d) or {})
    return out


def save_from(resp, url):
    """从响应的 CookieJar 提取 Set-Cookie, 存到最终 URL 的 host 下。"""
    _ensure_bg()
    host = domain_of(str(getattr(resp, "url", "") or url))
    if not host:
        return
    jar = getattr(resp, "cookies", None)
    if jar is None:
        return
    items = {}
    try:                                    # requests / curl_cffi 均支持 items()
        for k, v in jar.items():
            items[str(k)] = str(v)
    except Exception:
        try:                                # 兜底: 标准库 CookieJar 迭代
            inner = getattr(jar, "jar", None) or jar
            for c in inner:
                if c.value is not None:
                    items[c.name] = c.value
        except Exception:
            return
    if not items:
        return
    with _lock:
        cur = dict(_mem.get(host) or {})
        cur.update(items)
        _mem[host] = cur


def put(host, cookies: dict):
    """外部直接写入(如登录头窗口);host 为小写主机名。"""
    _ensure_bg()
    with _lock:
        cur = dict(_mem.get(host) or {})
        cur.update(cookies or {})
        _mem[host] = cur
    flush()


def clear():
    _ensure_bg()
    with _lock:
        _mem.clear()
    flush()


def flush():
    """内存 → sqlite 全量落盘(调用方持有语义:幂等、可并发重入)。"""
    with _lock:
        snap = {d: dict(ck) for d, ck in _mem.items()}
    p = db_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(p))
        try:
            now = time.time()
            con.execute("CREATE TABLE IF NOT EXISTS cookie("
                        "domain TEXT PRIMARY KEY, cookies TEXT, updated REAL)")
            con.execute("DELETE FROM cookie")
            con.executemany("INSERT INTO cookie(domain, cookies, updated) VALUES(?,?,?)",
                            [(d, json.dumps(ck, ensure_ascii=False), now)
                             for d, ck in snap.items() if ck])
            con.commit()
        finally:
            con.close()
    except Exception:
        pass

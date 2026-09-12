# -*- coding: utf-8 -*-
"""HTTP 抓取层:每个书源独立会话,浏览器 TLS 指纹,自动识别编码,统一超时。

参照 Legado HttpHelper 的三层容错:
  1. curl_cffi impersonate=chrome124 —— 请求带真实 Chrome TLS 指纹,强 WAF
     (指纹检测型 403/502)的主要对策;curl_cffi 不可用时自动回退 requests。
  2. 运行时 Cookie 库(cookie_store)—— 请求前按域合并、响应后自动保存,
     会话跨线程/跨次运行维持,显式 Cookie(用户登录头)按名优先。
  3. URL option 重试 —— {"retry":N} 对非 2xx 与连接异常重试 N 次(退避),
     默认不重试(与 Legado 一致,避免死源拖长全量校验/搜索的尾部)。
"""
import threading
import time

import requests
from bs4 import UnicodeDammit

from . import cookie_store

try:                                    # 浏览器 TLS 指纹伪装
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

IMPERSONATE = "chrome124"


def _system_proxies():
    """系统代理(requests 会自动读 Windows 注册表,curl_cffi 只认环境变量 ——
    不显式传入的话,Clash 等系统代理用户会全部直连,被墙站点一律连接重置)。"""
    try:
        import urllib.request
        px = urllib.request.getproxies() or {}
        return {k: v for k, v in px.items() if k in ("http", "https") and v}
    except Exception:
        return {}

if cffi_requests is not None:
    TIMEOUT_EXCS = (requests.exceptions.Timeout, cffi_requests.exceptions.Timeout)
else:
    TIMEOUT_EXCS = (requests.exceptions.Timeout,)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_sessions = {}
_lock = threading.Lock()


def new_session():
    """带浏览器 TLS 指纹的 Session;curl_cffi 不可用时回退 requests。"""
    if cffi_requests is not None:
        try:
            return cffi_requests.Session(impersonate=IMPERSONATE,
                                         proxies=_system_proxies() or None)
        except Exception:
            pass
    return requests.Session()


def _session(key: str):
    """同一 key(书源名+线程)复用独立 Session,避免 Cookie 串源。"""
    tid = threading.get_ident()
    k = (key, tid)
    with _lock:
        s = _sessions.get(k)
        if s is None:
            s = new_session()
            try:
                s.headers.update({"User-Agent": DEFAULT_UA,
                                  "Accept-Language": "zh-CN,zh;q=0.9"})
                s.trust_env = True
            except Exception:
                pass
            _sessions[k] = s
        return s


def _jar_dict(s, host):
    """会话 CookieJar 里属于 host(含子域)的 Cookie,避免跨域串包。"""
    out = {}
    jar = getattr(s, "cookies", None)
    inner = getattr(jar, "jar", None) or jar
    try:
        for c in inner:
            d = (getattr(c, "domain", "") or "").lstrip(".").lower()
            if d and not (host == d or host.endswith("." + d)):
                continue
            if c.value is not None:
                out[c.name] = c.value
    except Exception:
        pass
    return out


def _merge_cookie_header(s, headers, url):
    """合成最终 Cookie 头:会话 jar < 运行时库 < 显式(书源 header/用户登录头)。"""
    host = cookie_store.domain_of(url)
    stored = cookie_store.get_for(url)
    if not host or not (stored or headers.get("Cookie")):
        return headers
    explicit = {}
    for part in (headers.get("Cookie") or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            explicit[k.strip()] = v.strip()
    merged = {**_jar_dict(s, host), **stored, **explicit}
    headers = dict(headers)
    headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in merged.items())
    return headers


def decode_html(raw: bytes) -> str:
    """自动解码:优先 meta/头声明,失败按 gb18030 兜底。"""
    try:
        return UnicodeDammit(raw, ["utf-8", "gb18030", "gbk", "big5"]).unicode_markup or raw.decode("utf-8", "ignore")
    except Exception:
        return raw.decode("utf-8", "ignore")


def _raw_request(s, method, url, *, headers, body, timeout, retry,
                 verify=False, allow_redirects=True):
    """带重试的请求执行:retry>0 时对非 2xx 与连接异常重试(退避 0.5s×次数)。

    curl 的 timeout 是"总时长"语义(requests 是连接/读各算),按 Legado
    callTimeout=max(15,read)*2 的口径对 curl 会话放宽一倍,避免慢站被误杀。
    """
    t = timeout * 2 if cffi_requests is not None and isinstance(s, cffi_requests.Session) else timeout
    attempts = max(1, int(retry or 0) + 1)
    last = None
    for i in range(attempts):
        try:
            if method.upper() == "POST":
                last = s.post(url, data=body, headers=headers, timeout=t,
                              verify=verify, allow_redirects=allow_redirects)
            else:
                last = s.get(url, headers=headers, timeout=t,
                             verify=verify, allow_redirects=allow_redirects)
        except Exception:
            if i + 1 < attempts:
                time.sleep(0.5 * (i + 1))
                continue
            raise
        if last.status_code < 400:
            return last
        if i + 1 < attempts:
            time.sleep(0.5 * (i + 1))
    return last


def _fallback_raw(method, url, *, headers, body, timeout,
                  verify=False, allow_redirects=True):
    """兼容回退:指纹会话连不上时用原 requests 会话再试一次(等价 Legado
    COMPATIBLE_TLS —— 老协议/老 TLS 站点指纹会话握手会失败)。单次,无重试。"""
    s = requests.Session()
    try:
        s.headers.update({"User-Agent": DEFAULT_UA})
        s.trust_env = True
    except Exception:
        pass
    if method.upper() == "POST":
        return s.post(url, data=body, headers=headers, timeout=timeout,
                      verify=verify, allow_redirects=allow_redirects)
    return s.get(url, headers=headers, timeout=timeout,
                 verify=verify, allow_redirects=allow_redirects)


def _save_cookies(resp, url):
    try:
        cookie_store.save_from(resp, str(getattr(resp, "url", "") or url))
    except Exception:
        pass


def fetch(key: str, url: str, method: str = "GET", body=None,
          headers=None, timeout: float = 12.0, referer: str = "",
          retry: int = 0, charset: str = "") -> tuple:
    """返回 (final_url, text)。失败抛出异常由调用方决定是否吞掉。

    retry/charset 来自 URL option ,{"retry":N,"charset":"gbk"}(rules.parse_request)。
    """
    s = _session(key)
    hdrs = dict(headers or {})
    if referer:
        hdrs.setdefault("Referer", referer)
    hdrs = _merge_cookie_header(s, hdrs, url)
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    try:
        resp = _raw_request(s, method, url, headers=hdrs or None, body=body_bytes,
                            timeout=timeout, retry=retry)
    except Exception:
        if cffi_requests is None or not isinstance(s, cffi_requests.Session):
            raise
        resp = _fallback_raw(method, url, headers=hdrs or None, body=body_bytes,
                             timeout=timeout)          # 兼容回退, 不重试
    _save_cookies(resp, url)
    resp.raise_for_status()
    raw = resp.content
    if charset:
        try:
            return resp.url, raw.decode(charset, "replace")
        except Exception:
            pass
    return resp.url, decode_html(raw)


def request(method: str, url: str, headers=None, timeout: float = 12.0,
            retry: int = 0, verify=False, allow_redirects=True, body=None):
    """模块级请求入口(指纹会话 + cookie 库),返回 response 对象 —— 校验等无源场景用。"""
    s = _session("__probe__")
    hdrs = _merge_cookie_header(s, dict(headers or {}), url)
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    try:
        resp = _raw_request(s, method, url, headers=hdrs or None, body=body_bytes,
                            timeout=timeout, retry=retry, verify=verify,
                            allow_redirects=allow_redirects)
    except Exception:
        if cffi_requests is None or not isinstance(s, cffi_requests.Session):
            raise
        resp = _fallback_raw(method, url, headers=hdrs or None, body=body_bytes,
                             timeout=timeout, verify=verify,
                             allow_redirects=allow_redirects)
    _save_cookies(resp, url)
    return resp

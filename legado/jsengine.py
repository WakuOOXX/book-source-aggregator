# -*- coding: utf-8 -*-
"""JS 引擎(可选依赖 mini-racer/V8)—— 解锁含 JS 的书源。

设计要点:
* 可选依赖:import 失败 HAS_JS=False,上层(engine/rules)据此回退旧行为
  (整源跳过),绝不崩。
* 线程模型:MiniRacer 实例不能跨线程并发 eval,但每源一线程(最多 384)各建
  一个 isolate 内存开销过大 —— 改用**隔离池**(默认 32 个,BOOKDL_JS_POOL 可调),
  信号量限流 + LifoQueue 复用;isolate 跑满若干脚本后回收重建,防变量累积。
* 同步桥:mini-racer 0.14 的 wrap_py_function 只提供异步(Promise)回调,而
  Legado 源脚本是同步调用 java.ajax(url) 拿文本 —— 不可行。故用
  **挂起-重跑(suspend-and-rerun)** 协议:JS 侧 java.* 先查备忘录 __memo,
  命中直接返回;未命中把 (方法名, JSON 参数) 写入全局 __pend 并抛哨兵字符串;
  Python 捕获后执行宿主操作(网络走 fetcher/编解码走标准库),结果写回
  __memo 再重跑脚本。重跑用**函数作用域内的直接 eval** —— 变量(含
  const/let/function)每次落在全新作用域,互不串扰,且顶层补全值
  (Rhino 语义,脚本是表达式即其值)与 Legado 一致;脚本最后若非表达式则
  回退取 result 变量。典型脚本 1~3 个 java.* 调用,重跑 2~4 次,每次毫秒级。
* 所有失败(JS 报错/超时/网络失败/未实现的 API)一律返回 None,由规则层
  归约为空结果 —— 单源失败不影响其它源,更不影响整轮搜索。
"""
import base64
import hashlib
import inspect
import json
import logging
import os
import queue
import re
import threading
import time

log = logging.getLogger("bookdl.js")

# ------------------------------------------------------------ 可用性探测 ----
HAS_JS = False
MiniRacer = JSEvalException = JSTimeoutException = None
for _mod in ("py_mini_racer", "mini_racer"):          # 0.14 装的是 py_mini_racer
    try:
        _m = __import__(_mod)
        MiniRacer = _m.MiniRacer
        JSEvalException = _m.JSEvalException
        JSTimeoutException = _m.JSTimeoutException
        # 需要 timeout_sec 参数(0.9+);老版 py-mini-racer 语义不同,宁可不用
        if "timeout_sec" not in inspect.signature(MiniRacer.eval).parameters:
            raise ImportError("MiniRacer.eval 不支持 timeout_sec")
        _probe = MiniRacer()
        _probe.eval("1")
        _probe.close()
        HAS_JS = True
        break
    except Exception as _e:                           # 未安装/DLL 起不来
        MiniRacer = JSEvalException = JSTimeoutException = None
        log.debug("JS 引擎 %s 不可用: %s", _mod, _e)

EVAL_TIMEOUT = 5.0        # 单次 eval 秒数(防脚本死循环挂死)
TOTAL_TIMEOUT = 20.0      # 单条脚本总预算(含挂起-重跑多轮)
MAX_ROUNDS = 64           # 挂起-重跑轮数上限
POOL_SIZE = max(2, int(os.environ.get("BOOKDL_JS_POOL", "32")))
RECYCLE_AFTER = 300       # 每个 isolate 服务这么多条脚本后回收重建

_SUSPEND = "__JSBRIDGE_SUSPEND__"
_MEMO_KEY_SEP = "\u0001"

# ---------------------------------------------------------------- JS 胶水 ----
# __call:查备忘录 → 命中返回;未命中记 __pend 抛哨兵(由 Python 驱动重跑)。
# java.* 覆盖普查 Top API;未提供的名字调用即挂起后由 Python 判"未实现"→ 整条空。
_JS_GLUE = r"""
var __memo = {}, __pend = null;
function __call(name, argsJson) {
    var k = name + "\u0001" + argsJson;
    if (typeof __memo[k] !== "undefined") return JSON.parse(__memo[k]);
    __pend = { name: name, args: argsJson };
    throw "__JSBRIDGE_SUSPEND__";
}
var java = {
    ajax: function (u, o) { return __call("ajax", JSON.stringify([u, (o === undefined ? null : o)])); },
    ajaxAll: function (l) { return __call("ajaxAll", JSON.stringify([l === undefined ? [] : l])); },
    connect: function (u, o) { return __call("ajax", JSON.stringify([u, (o === undefined ? null : o)])); },
    get: function (u, h) { return __call("get", JSON.stringify([u, (h === undefined ? null : h)])); },
    post: function (u, b, h) { return __call("post", JSON.stringify([u, (b === undefined ? null : b), (h === undefined ? null : h)])); },
    base64Encode: function (s) { return __call("b64e", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    base64Decode: function (s) { return __call("b64d", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    md5Encode: function (s) { return __call("md5", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    md5Encode16: function (s) { return __call("md5_16", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    urlEncode: function (s) { return __call("urlEncode", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    urlDecode: function (s) { return __call("urlDecode", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    utf8Decode: function (s) { return __call("utf8Decode", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    gbkDecode: function (s) { return __call("gbkDecode", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    gbkEncode: function (s) { return __call("gbkEncode", JSON.stringify([s === undefined || s === null ? "" : String(s)])); },
    timeFormat: function (t) { return __call("timeFormat", JSON.stringify([t === undefined ? null : t])); },
    getString: function (r, u) { return __call("getString", JSON.stringify([r === undefined || r === null ? "" : String(r), u ? 1 : 0])); },
    getVerification: function () { return __call("__unsup__getVerification", "[]"); },
    startBrowser: function () { return __call("__unsup__startBrowser", "[]"); },
    log: function () {}
};
// 运行入口:函数作用域内直接 eval —— 脚本变量每次落在全新作用域(含 const/let),
// 顶层补全值即脚本是表达式时的值(Rhino 语义);非表达式结尾回退取 result 变量。
function __run() {
    var __v = eval(__script);
    if (typeof __v === "undefined" || __v === null) {
        __v = (typeof result === "undefined") ? "" : result;
    }
    if (__v !== null && typeof __v === "object") {
        try { __v = JSON.stringify(__v); } catch (e) { __v = ""; }
    }
    return String(__v);
}
"""


def _js_literal(name, val):
    """把 Python 值变成 JS var 赋值语句(ensure_ascii 转义,杜绝引号注入)。"""
    return "var %s = %s" % (name, json.dumps(val))


# ------------------------------------------------------------ 隔离池 --------
class _Lease:
    """一个借出的 isolate:MiniRacer 实例 + 本次脚本的备忘录/上下文。"""

    __slots__ = ("mr", "memo", "vars", "served", "dead")

    def __init__(self):
        self.mr = MiniRacer()
        self.mr.eval(_JS_GLUE)
        self.memo = {}
        self.vars = {}
        self.served = 0
        self.dead = False


_sem = threading.BoundedSemaphore(POOL_SIZE)
_free = queue.LifoQueue()


def _acquire():
    _sem.acquire()
    try:
        return _free.get_nowait()
    except queue.Empty:
        return _Lease()               # 创建失败由 run() 统一兜底


def _release(lease):
    if lease.dead or lease.served >= RECYCLE_AFTER:
        try:
            lease.mr.close()
        except Exception:
            pass
    else:
        _free.put(lease)
    _sem.release()


# ------------------------------------------------------------ 宿主操作 ------
def _resolve_url(lease, url):
    from urllib.parse import urljoin
    u = str(url or "").strip()
    if not u:
        return ""
    base = lease.vars.get("base_url") or ""
    if base and not u.lower().startswith(("http://", "https://")):
        u = urljoin(base, u)
    return u


def _opt_dict(v):
    """option 参数:JSON 字符串或 dict → dict。"""
    if isinstance(v, str) and v.strip():
        try:
            v = json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _http_url_option(lease, url, option=None):
    """java.ajax/connect:URL(可带 ,{json} 请求配置)+ option 覆盖 → 响应文本。"""
    from . import rules as R, fetcher as F
    u = _resolve_url(lease, url)
    if not u:
        return None
    req = R.parse_request(u, {})
    method = req["method"]
    body = req.get("body", "")
    headers = req.get("headers") or {}
    retry = req.get("retry") or 0
    charset = req.get("charset") or ""
    opt = _opt_dict(option)
    if opt:
        if opt.get("method"):
            method = str(opt["method"]).upper()
        if opt.get("body") is not None:
            body = opt["body"]
        if isinstance(opt.get("headers"), dict):
            headers = {**headers, **opt["headers"]}
        if opt.get("charset"):
            charset = str(opt["charset"])
        try:
            retry = max(retry, int(opt.get("retry") or 0))
        except Exception:
            pass
    _, text = F.fetch(lease.vars.get("src_key") or "js", req["url"], method,
                      body or "", headers or None, 12.0, retry=retry,
                      charset=charset)
    return text or None


def _op_ajax(lease, url, option=None):
    return _http_url_option(lease, url, option)


def _op_get(lease, url, headers=None):
    from . import fetcher as F
    u = _resolve_url(lease, url)
    if not u:
        return None
    hd = _opt_dict(headers) or None
    _, text = F.fetch(lease.vars.get("src_key") or "js", u, "GET", "",
                      hd, 12.0)
    return text or None


def _op_post(lease, url, body=None, headers=None):
    from . import fetcher as F
    u = _resolve_url(lease, url)
    if not u:
        return None
    hd = _opt_dict(headers) or None
    _, text = F.fetch(lease.vars.get("src_key") or "js", u, "POST",
                      body or "", hd, 12.0)
    return text or None


def _op_ajax_all(lease, urls):
    out = []
    for u in (urls if isinstance(urls, list) else []):
        try:
            out.append(_http_url_option(lease, u) or "")
        except Exception:
            out.append("")
    return out


def _op_b64e(lease, s):
    return base64.b64encode(str(s or "").encode("utf-8")).decode("ascii")


def _op_b64d(lease, s):
    s = re.sub(r"\s+", "", str(s or ""))
    s = s.replace("-", "+").replace("_", "/")     # URL 安全变体归一
    s += "=" * (-len(s) % 4)                      # Legado 容忍缺省 padding
    try:
        raw = base64.b64decode(s)
    except Exception:
        return None
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("gb18030", "replace")


def _op_md5(lease, s):
    return hashlib.md5(str(s or "").encode("utf-8")).hexdigest()


def _op_md5_16(lease, s):
    return hashlib.md5(str(s or "").encode("utf-8")).hexdigest()[8:24]


def _op_url_encode(lease, s):
    from urllib.parse import quote
    return quote(str(s or ""), safe="")


def _op_url_decode(lease, s):
    from urllib.parse import unquote_plus
    return unquote_plus(str(s or ""))


def _op_utf8_decode(lease, s):
    return str(s or "")                           # JS 里已是文本,字节流场景罕见


def _op_gbk_decode(lease, s):
    s = str(s or "")
    try:
        return s.encode("latin-1").decode("gbk")
    except Exception:
        return s


def _op_gbk_encode(lease, s):
    s = str(s or "")
    try:
        return s.encode("gbk").decode("latin-1")
    except Exception:
        return s


def _op_time_format(lease, ts=None):
    if ts in (None, "", 0):
        t = time.time()
    else:
        try:
            t = float(ts)
        except Exception:
            return None
        if t > 1e11:                              # 毫秒时间戳
            t /= 1000.0
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _op_get_string(lease, rule, is_url=0):
    """java.getString:在最近抓取的页面上再跑一条规则(页文本由 rules 层注入)。"""
    from . import rules as R
    page = lease.vars.get("page_text") or ""
    rule = str(rule or "").strip()
    if not page or not rule:
        return ""
    st = page.lstrip()
    root = None
    if st[:1] in ("{", "["):
        try:
            root = json.loads(page)
        except Exception:
            root = None
    if root is None:
        root = R.parse_dom(page)
    return R.extract_value(root, rule) or ""


_OPS = {
    "ajax": _op_ajax,
    "ajaxAll": _op_ajax_all,
    "get": _op_get,
    "post": _op_post,
    "b64e": _op_b64e,
    "b64d": _op_b64d,
    "md5": _op_md5,
    "md5_16": _op_md5_16,
    "urlEncode": _op_url_encode,
    "urlDecode": _op_url_decode,
    "utf8Decode": _op_utf8_decode,
    "gbkDecode": _op_gbk_decode,
    "gbkEncode": _op_gbk_encode,
    "timeFormat": _op_time_format,
    "getString": _op_get_string,
}


def _dispatch(name, args, lease):
    """执行一次挂起的 java.* 调用;None 表示失败(整条 JS 结果为空)。"""
    try:
        if name.startswith("__unsup__"):
            log.debug("JS 调用不支持的 API: %s", name[len("__unsup__"):])
            return None
        fn = _OPS.get(name)
        if fn is None:
            log.debug("JS 调用未实现的 java.%s(共 %s)", name, args)
            return None
        return fn(lease, *args)
    except Exception as e:
        log.debug("java.%s 执行失败: %s", name, e)
        return None


# ------------------------------------------------------------ 对外入口 ------
def run(script, variables=None, base_url="", page_text=None,
        src_key="js", timeout=EVAL_TIMEOUT):
    """跑一段 Legado JS,返回字符串;失败/超时/无引擎返回 None。

    variables: 注入脚本的全局变量(result/key/page/baseUrl 等)。
    page_text: 最近抓取的页面原文(java.getString 用)。
    """
    if not HAS_JS or not script or not str(script).strip():
        return None
    script = str(script)
    if timeout <= 0:
        timeout = EVAL_TIMEOUT
    try:
        lease = _acquire()
    except Exception as e:
        log.debug("JS isolate 创建失败: %s", e)
        return None
    try:
        mr = lease.mr
        lease.memo = {}
        lease.vars = {"base_url": base_url or "", "page_text": page_text or "",
                      "src_key": src_key or "js"}
        vars_ = {"result": "", "baseUrl": base_url or ""}
        if variables:
            for k, v in variables.items():
                if v is not None:
                    vars_[str(k)] = v
        deadline = time.monotonic() + max(TOTAL_TIMEOUT, timeout * 3)
        for _ in range(MAX_ROUNDS):
            if time.monotonic() > deadline:
                log.debug("JS 脚本超总预算(%d 轮)", MAX_ROUNDS)
                return None
            for k, v in vars_.items():
                mr.eval(_js_literal(k, v))
            mr.eval(_js_literal("__memo", lease.memo))
            mr.eval(_js_literal("__script", script))
            try:
                out = mr.eval("__run()", timeout_sec=timeout)
                lease.served += 1
                if out is None:
                    return ""
                return out if isinstance(out, str) else str(out)
            except JSTimeoutException:
                # 超时后 isolate 可能残留被终止的执行态,回收重建不再复用
                lease.dead = True
                log.debug("JS 脚本超时(%.1fs)", timeout)
                return None
            except JSEvalException as e:
                if _SUSPEND not in str(e):
                    log.debug("JS 脚本报错: %s", str(e).splitlines()[0][:200])
                    return None
                pend = mr.eval("JSON.stringify(__pend)")
                if not isinstance(pend, str) or pend == "null":
                    return None
                info = json.loads(pend)
                mr.eval("__pend = null")
                res = _dispatch(info["name"], json.loads(info["args"]), lease)
                if res is None:
                    return None
                lease.memo[info["name"] + _MEMO_KEY_SEP + info["args"]] = \
                    json.dumps(res, ensure_ascii=False)
        log.debug("JS 挂起-重跑超过 %d 轮,放弃", MAX_ROUNDS)
        return None
    except Exception as e:
        log.debug("JS 引擎异常: %s", e)
        return None
    finally:
        try:
            _release(lease)
        except Exception:
            pass

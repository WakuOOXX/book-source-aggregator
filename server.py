# -*- coding: utf-8 -*-
"""server.py —— WinUI 3 前端的 Python 后端子进程(JSONL over stdio,零 tkinter)。

结构照抄 cli.py,复用同一套模式:
  _discover_files / _init_engine(_merge_sources) 引擎注入与工作源合并、
  core.search.search_run / core.verify.verify_run / core.download.download_run
  三个 core 编排入口、后台线程 + threading.Event 的停止语义(run_with_cancel)。
差别只有一处:cli.py 把事件 print 到终端,本进程把事件 json.dumps 成一行写
stdout —— 「print 输出」换成「JSONL 输出」。

协议(计划 §3.2 方案 A + §8):
  前端 → stdin:每行一个 {"cmd": ...} JSON 命令;
  后端 → stdout:每行一个 {"type": ...} JSON 对象(ensure_ascii=False, UTF-8,
  flush=True,整行原子写)。

后端 → 前端的消息类型:
  hello  init 的应答(版本 / JS 引擎 / 源统计);
  event  core 的 15 种 kind 原样平移:
         {"type":"event","kind":<kind>,"payload":<payload>};
  ack    命令受理确认与只读查询结果(stop / sources.* / auth.* / config.get),
         带 "cmd" 字段与命令专属数据;
  busy   已有重操作在跑,拒绝新的重操作(带 "cmd");
  error  协议级错误(JSON 解析失败 / 未知命令 / 参数错误 / 编排异常),
         区别于业务 dlerr。

线程模型(与计划 §3.2 一致):主线程只循环读 stdin 以便随时收 stop;
重操作(search / verify / download / auth.fetch)跑在后台线程,同一时刻只允许
一个(busy 字段);stop 置当前线程的 threading.Event。emit 回调带锁整行原子写,
保证并发工作线程的事件不会撕裂 stdout 行。

发射契约关键点:core 工作线程调用 emit((kind, payload)) —— **单个二元组**,
不是 emit(kind, payload) 两个参数。emit 里按下标解包,arity 不符即回协议级
error(Android 线踩过"两参调用"的真 bug,此处固化防线)。

零 tkinter:本文件对 tkinter 零依赖(守卫测试覆盖)。--selftest 自检协议。

唯一两处协议适配(见 _project_book):dlone / dldone 的 book 结构里含全书正文
(engine.load_book 的 "chapters" 字段),整本正文走 JSONL 无意义 —— 仅剔除该
字段,其余字段名与 core 结构逐字同名;章级进度仍由 dlprog 事件承载。
"""
import json
import shutil
import sys
import threading
from pathlib import Path

from legado import engine, jsengine
from legado.normalize import dedupe_sources

import core
from core import config as _config
from core import mem as core_mem
from core import auth_manager as core_auth
from core.artifacts import is_verify_artifact, load_auth_state, _url_host
from core.download import download_run
from core.search import relevant, search_run
from core.verify import load_deep_tables, verify_run

try:
    import cdp_cookie
except Exception:                                  # 无 CDP 依赖时降级(仍给接口)
    cdp_cookie = None

VERSION = "1.9.1"

# 重操作:同一时刻只允许一个(busy 语义)。
HEAVY_CMDS = ("search", "verify", "download", "auth.fetch")


# ------------------------------------------------------------ JSONL 输出 -----
def _reconfigure_stdio():
    """UTF-8 输入输出(Windows 控制台/管道默认 cp936,中文事件会乱码/报错)。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _project_book(book):
    """dlone/dldone 的 book 去正文:剔除全书 "chapters" 大字段。

    其余字段名与 core 结构逐字同名(不改名);章级细节由 dlprog 事件承载。
    """
    if isinstance(book, dict):
        return {k: v for k, v in book.items() if k != "chapters"}
    return book


def _project_payload(kind, payload):
    """事件 payload 的 JSON 投影。除 dlone/dldone 去正文外一律原样。

    返回的字段名与 core 结构逐字同名;payload 本体类型(元组)保持不变,
    json 序列化时元组自然落为数组。
    """
    if kind == "dlone":
        book, path, bi, n = payload
        return (_project_book(book), path, bi, n)
    if kind == "dldone":
        mode, fmt, ok_list, fail_list = payload
        ok2 = [(_project_book(b), p) for b, p in ok_list]
        return (mode, fmt, ok2, fail_list)
    return payload


# ------------------------------------------------------ 引擎注入 / 工作源 ----
def _discover_files(source_dir):
    """书源目录下的原始表清单(剔除校验产物与 auth_state.json)。与 cli.py 同口径。"""
    source_dir = Path(source_dir)
    if not source_dir.exists():
        return []
    return sorted(p for p in source_dir.iterdir()
                  if p.is_file() and p.suffix.lower() == ".json"
                  and not is_verify_artifact(p.name)
                  and p.name != "auth_state.json")


def _init_engine(source_dir, log=None):
    """启动注入:登录头 + 深度判定表(对应 cli._init_engine / App.__init__)。

    set_auth 灌登录头,load_deep_tables 灌深度判定表。log 为消息回调(可省)。
    """
    _config.SOURCE_DIR = Path(source_dir)
    auth = load_auth_state()
    core.set_auth(auth)
    fns = [p.name for p in _discover_files(_config.SOURCE_DIR)]
    deep = load_deep_tables(fns) if fns else {}
    if log:
        log("书源目录: %s" % _config.SOURCE_DIR)
        log("已注入登录头 %d 条 · 深度判定 %d 条。" % (len(auth), len(deep)))
    return auth, deep


def _merge_sources(source_dir, files=None, log=None):
    """合并工作源(照抄 cli._merge_sources):有 .good.json 用有效表,否则全量表,
    跨文件按 (书源名, 站点 URL) 去重、有效表来源优先。

    files: 只加载这些文件名(服务端=sources 清单;None=目录下全部原始表,
    与 cli.py 语义一致)。
    """
    source_dir = Path(source_dir)
    names = None if files is None else set(files)
    merged = []
    for p in _discover_files(source_dir):
        if names is not None and p.name not in names:
            continue
        try:
            srcs = engine.load_sources(str(p))
        except Exception as e:
            if log:
                log("✘ 书源文件读取失败,已跳过: %s(%s)" % (p.name, e))
            continue
        good = p.with_name(p.stem + ".good.json")
        use, use_good = srcs, False
        if good.exists():
            try:
                use = engine.load_sources(str(good))
                use_good = True
            except Exception:
                use = srcs
        for s in use:
            s["_file"] = p.name
            s["_from_good"] = use_good
        if log:
            log("已加载 %s:%s %d 源"
                % (p.name, "有效表" if use_good else "全量", len(use)))
        merged.extend(use)
    srcs, n_dup = dedupe_sources(merged)
    if log:
        log("合并工作源 %d 个(去重 %d)。" % (len(srcs), n_dup))
    return srcs


# ------------------------------------------------------------- 服务主体 ------
class Server:
    """JSONL 命令服务:handle_line 逐行处理,emit 回调写事件。"""

    def __init__(self, out=None, source_dir=None, state_file=None):
        self.out = out if out is not None else sys.stdout
        self.source_dir = Path(source_dir) if source_dir else _config.SOURCE_DIR
        # state_file=None 时读真实 sel_state.json 恢复 sources 清单;测试传
        # state_file=False 跳过(等价"全选")。
        self._state_file = _config.STATE_FILE if state_file is None else state_file
        self._wlock = threading.Lock()
        self._lock = threading.Lock()
        self.busy = False
        self._busy_cmd = ""
        self._stop = None
        self._thread = None
        self._auth_handle = None            # CDP 抓取浏览器句柄
        self.checked_files = self._load_checked()   # None = 清单未定(用全部)

    # ---------------------------------------------------------- 底层输出 ----
    def _write(self, obj):
        """整行原子写(工作线程并发 emit 时防撕裂)。"""
        line = json.dumps(obj, ensure_ascii=False)
        with self._wlock:
            self.out.write(line + "\n")
            try:
                self.out.flush()
            except Exception:
                pass

    def reply(self, typ, **kw):
        obj = {"type": typ}
        obj.update(kw)
        self._write(obj)

    def ack(self, cmd, **kw):
        self.reply("ack", cmd=cmd, **kw)

    def error(self, message, cmd=None):
        obj = {"type": "error", "message": str(message)}
        if cmd:
            obj["cmd"] = cmd
        self._write(obj)

    def emit(self, ev):
        """core 的 emit 回调:接收**单个 (kind, payload) 元组**。

        arity 不符(误写成 emit(kind, payload))时这里解包失败,回协议级
        error —— 固化"单元组 vs 两参"的真 bug 防线。
        """
        try:
            kind, payload = ev
        except Exception:
            self.error("emit 契约错误:需要单个 (kind, payload) 单元组,收到 %r"
                       % (ev,))
            return
        self.reply("event", kind=kind, payload=_project_payload(kind, payload))

    def emit_log(self, msg):
        self.emit(("log", msg))

    # ------------------------------------------------------ 重操作调度 ------
    def _start_heavy(self, cmd, fn):
        """起后台线程跑重操作;已有重操作在跑则回 busy 拒绝。返回是否起成功。"""
        with self._lock:
            if self.busy:
                self.reply("busy", cmd=cmd, running=self._busy_cmd)
                return False
            self.busy = True
            self._busy_cmd = cmd
            self._stop = threading.Event()
            stop = self._stop

        def run():
            try:
                fn(stop)
            except Exception as e:
                self.error("重操作异常(%s): %s" % (cmd, e), cmd=cmd)
            finally:
                with self._lock:
                    self.busy = False
                    self._busy_cmd = ""
                    self._stop = None

        self.ack(cmd)                       # 受理确认
        t = threading.Thread(target=run, daemon=True)
        self._thread = t
        t.start()
        return True

    def join(self, timeout=5.0):
        """测试/收尾用:等当前重操作线程结束。"""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return not (t is not None and t.is_alive())

    # ------------------------------------------------------- 命令分发 -------
    def handle_line(self, line):
        line = (line or "").strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except Exception as e:
            self.error("JSON 解析失败: %s" % e)
            return
        if not isinstance(msg, dict):
            self.error("命令必须是 JSON 对象")
            return
        cmd = msg.get("cmd") or ""
        fn = getattr(self, "_cmd_" + str(cmd).replace(".", "_"), None)
        if fn is None:
            self.error("未知命令: %s" % (cmd or "(空)"), cmd=cmd or None)
            return
        try:
            fn(msg)
        except Exception as e:
            self.error("命令 %s 处理失败: %s" % (cmd, e), cmd=cmd)

    # --------------------------------------------------- sources 清单 -------
    def _load_checked(self):
        """从运行记忆恢复书源清单(与 app.checked_files 同源:sel_state["sources"])。"""
        if not self._state_file:
            return None
        try:
            mem = core_mem.load_state(self._state_file)
            s = mem.get("sources")
            return list(s) if s is not None else None
        except Exception:
            return None

    def _all_files(self):
        return [p.name for p in _discover_files(self.source_dir)]

    def _checked(self):
        """清单生效值:未定(None)→ 目录下全部原始表;否则仅清单内且存在的文件。"""
        allf = self._all_files()
        if self.checked_files is None:
            return allf
        return [fn for fn in self.checked_files if fn in allf]

    # ------------------------------------------------------- 命令实现 -------
    def _cmd_init(self, msg):
        files = self._all_files()
        srcs = _merge_sources(self.source_dir, self._checked(), log=None)
        auth = load_auth_state()
        self.reply("hello", cmd="init", version=VERSION,
                   js=bool(jsengine.HAS_JS), sources=len(srcs),
                   files=len(files), checked=len(self._checked()),
                   auth=len(auth), source_dir=str(self.source_dir))

    def _cmd_config_get(self, msg):
        C = _config
        self.ack("config.get",
                 search_workers=C.SEARCH_WORKERS,
                 verify_workers=C.VERIFY_WORKERS,
                 download_workers=C.DOWNLOAD_WORKERS,
                 deep_keyword=C.DEEP_KEYWORD,
                 auto_para_tabs=C.AUTO_PARA_TABS,
                 source_dir=str(C.SOURCE_DIR),
                 default_source=str(C.DEFAULT_SOURCE),
                 default_out=str(C.DEFAULT_OUT),
                 state_file=str(C.STATE_FILE),
                 version=VERSION, js=bool(jsengine.HAS_JS))

    def _cmd_search(self, msg):
        key = (msg.get("keyword") or "").strip()
        if not key:
            self.error("search 缺少 keyword", cmd="search")
            return
        fuzzy = bool(msg.get("fuzzy", True))
        rel = bool(msg.get("rel", True))
        domain = msg.get("domain") or "自动"
        deep_only = bool(msg.get("deep_only", False))
        self._start_heavy("search", lambda stop: self._run_search(
            key, fuzzy, rel, domain, deep_only, stop))

    def _run_search(self, key, fuzzy, rel, domain, deep_only, stop):
        srcs = _merge_sources(self.source_dir, self._checked(), log=self.emit_log)
        if deep_only:
            skipped = [s for s in srcs if engine.deep_search_ok(s) is False]
            if skipped:
                srcs = [s for s in srcs if engine.deep_search_ok(s) is not False]
                self.emit_log("深度过滤:跳过 %d 个试搜未通过源" % len(skipped))
        if not srcs:
            self.emit_log("没有可用书源,无法搜索。")
            self.emit(("sres", (0, fuzzy)))      # 仍需收尾信号给前端解锁
            return
        keep = (lambda h: relevant(h, key, domain)) if rel else None
        search_run(srcs, key, emit=self.emit, stop=stop, fuzzy=fuzzy, keep=keep)

    def _cmd_verify(self, msg):
        deep = bool(msg.get("deep", False))
        files = msg.get("files") or None
        if files is not None and not isinstance(files, list):
            self.error("verify 的 files 必须是数组", cmd="verify")
            return
        self._start_heavy("verify", lambda stop: self._run_verify(
            deep, files, stop))

    def _run_verify(self, deep, files, stop):
        if files:
            fl = []
            for f in files:
                p = Path(f)
                if not p.is_absolute():
                    p = self.source_dir / f
                if p.exists():
                    fl.append((p.name, p))
                else:
                    self.emit_log("⚠ 校验跳过缺失文件: %s" % f)
        else:
            fl = [(p.name, p) for p in _discover_files(self.source_dir)]
        if not fl:
            self.emit_log("没有可校验的书源文件(书源目录为空或文件缺失)。")
            self.emit(("vdone", (0, 0, 0, 0.0, False)))
            return
        verify_run(fl, emit=self.emit, stop=stop, deep=deep,
                   reload_files=[fn for fn, _ in fl])

    def _cmd_download(self, msg):
        hits = msg.get("hits")
        if not isinstance(hits, list) or not hits:
            self.error("download 缺少非空 hits", cmd="download")
            return
        # 前端口径 merge → core 内部口径 batch(照抄 cli.cmd_download 的映射)。
        mode = "batch" if (msg.get("mode") or "single") == "merge" else "single"
        fmt = msg.get("fmt") or "epub"
        out = str(msg.get("out") or _config.DEFAULT_OUT)
        self._start_heavy("download", lambda stop: self._run_download(
            hits, out, mode, fmt, stop))

    def _run_download(self, hits, out, mode, fmt, stop):
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.emit(("dlerr", "导出目录创建失败: %s" % e))
            return
        download_run(hits, out, mode, fmt, emit=self.emit, stop=stop)

    def _cmd_stop(self, msg):
        with self._lock:
            stop = self._stop
            busy = self.busy
            running = self._busy_cmd
        if stop is not None:
            stop.set()
        self.ack("stop", stopped=bool(stop), busy=busy, running=running)

    # ---------------------------------------------------- sources 命令族 -----
    def _cmd_sources_list(self, msg):
        allf = self._all_files()
        checked = set(self._checked())
        self.ack("sources.list",
                 files=[{"name": fn, "checked": fn in checked,
                         "exists": (self.source_dir / fn).exists()}
                        for fn in allf],
                 checked=self._checked(), dir=str(self.source_dir))

    def _cmd_sources_add(self, msg):
        paths = msg.get("paths")
        if not isinstance(paths, list) or not paths:
            self.error("sources.add 缺少 paths", cmd="sources.add")
            return
        try:
            self.source_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.error("书源目录创建失败: %s" % e, cmd="sources.add")
            return
        checked = list(self._checked())
        added, copied, skipped = [], [], []
        for p in paths:
            src = Path(p)
            if is_verify_artifact(src.name):     # 校验产物=缓存,不能当书源输入
                skipped.append(src.name)
                continue
            dest = self.source_dir / src.name
            try:
                if src.resolve() != dest.resolve():
                    shutil.copy2(str(src), str(dest))   # 不在 shuyuan/ 的复制进去
                    copied.append(src.name)
            except Exception as e:
                self.error("复制失败 %s: %s" % (src, e), cmd="sources.add")
                continue
            if src.name not in checked:
                checked.append(src.name)
                added.append(src.name)
        self.checked_files = checked
        self.ack("sources.add", added=added, copied=copied, skipped=skipped,
                 checked=checked)

    def _cmd_sources_remove(self, msg):
        fn = msg.get("file")
        if not fn:
            self.error("sources.remove 缺少 file", cmd="sources.remove")
            return
        checked = list(self._checked())
        if fn in checked:
            checked.remove(fn)
        self.checked_files = checked
        self.ack("sources.remove", removed=fn, checked=checked,
                 note="仅移出清单,磁盘文件保留在 shuyuan/")

    # ------------------------------------------------------- auth 命令族 -----
    def _cmd_auth_list(self, msg):
        auth = load_auth_state()
        self.ack("auth.list", count=len(auth),
                 entries=[{"url": u,
                           "cookie": (c or {}).get("cookie") or "",
                           "header": (c or {}).get("header") or {}}
                          for u, c in auth.items()])

    def _cmd_auth_save(self, msg):
        urls = msg.get("urls")
        if not urls and msg.get("url"):
            urls = [msg["url"]]
        if not isinstance(urls, list) or not urls:
            self.error("auth.save 缺少 urls", cmd="auth.save")
            return
        header = msg.get("header") or {}
        if isinstance(header, str):
            header = core_auth.parse_header(header)
        if header is None:
            self.error("header 不是合法的 {\"键\": \"值\"} JSON", cmd="auth.save")
            return
        cookie = msg.get("cookie") or ""
        auth = load_auth_state()
        # 两项都空 = 移除所选源的配置(与 app.save_sel 语义一致)。
        core_auth.apply_to_urls(auth, urls, cookie, header)
        core_auth.persist(auth)
        self.ack("auth.save", urls=urls, count=len(auth))

    def _cmd_auth_fetch(self, msg):
        """浏览器登录抓取(单站,两段式,对应 app 的 launch→grab):

        {"cmd":"auth.fetch","url":"https://…"}         → 拉起浏览器打开登录页
        {"cmd":"auth.fetch","grab":true,"host":"xx.com"} → 抓取该主机 Cookie 并关浏览器
        TODO: 批量/并行(三段式)抓取未接入,后续按需把 core.auth_manager 状态机
        包成独立命令族;当前接口先行,满足单站语义。
        """
        if cdp_cookie is None:
            self.error("cdp_cookie 不可用,无法抓取", cmd="auth.fetch")
            return
        if msg.get("grab"):
            self._start_heavy("auth.fetch", lambda stop: self._run_auth_grab(msg))
        elif (msg.get("url") or "").strip():
            self._start_heavy("auth.fetch", lambda stop: self._run_auth_launch(msg))
        else:
            self.error("auth.fetch 需要 url(启动)或 grab=true(抓取)",
                       cmd="auth.fetch")

    def _run_auth_launch(self, msg):
        url = (msg.get("url") or "").strip()
        profile = _config.APP_DIR / "auth_profile"
        try:
            self._auth_handle = cdp_cookie.launch_for_auth(url, profile)
        except Exception as e:
            self.error("启动抓取浏览器失败: %s" % e, cmd="auth.fetch")
            return
        self.ack("auth.fetch", phase="launched", url=url,
                 grab_host=_url_host(url))

    def _run_auth_grab(self, msg):
        url = (msg.get("url") or "").strip()
        host = (msg.get("host") or _url_host(url) or "").lower()
        handle = self._auth_handle
        if handle is None:
            self.error("尚无已启动的抓取浏览器(先发 auth.fetch 带 url)",
                       cmd="auth.fetch")
            return
        try:
            cookie = cdp_cookie.fetch_cookies(handle, host)
        except Exception as e:
            self.error("抓取失败: %s" % e, cmd="auth.fetch")
            return
        try:
            cdp_cookie.close(handle)
        except Exception:
            pass
        self._auth_handle = None
        self.ack("auth.fetch", phase="captured", host=host,
                 cookie=cookie or "")


# ------------------------------------------------------------- 自检 ----------
def _selftest():
    """不联网自检:跑 init / config.get / 未知命令,校验协议输出形状。"""
    import io
    buf = io.StringIO()
    srv = Server(out=buf, source_dir=_config.SOURCE_DIR, state_file=False)
    srv.handle_line('{"cmd":"init"}')
    srv.handle_line('{"cmd":"config.get"}')
    srv.handle_line('{"cmd":"nope"}')
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    objs = [json.loads(l) for l in lines]
    kinds = [o.get("type") for o in objs]
    assert "hello" in kinds, kinds
    assert any(o.get("cmd") == "config.get" for o in objs), objs
    assert any(o.get("type") == "error" for o in objs), objs
    sys.stdout.write("selftest OK · %d 条输出\n%s\n"
                     % (len(objs), "\n".join(lines)))
    return 0


# -------------------------------------------------------------- main ---------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    _reconfigure_stdio()
    srv = Server()
    _init_engine(srv.source_dir, log=srv.emit_log)
    srv.emit_log("后端就绪:server.py %s · JS 引擎%s"
                 % (VERSION, "已启用" if jsengine.HAS_JS else "未安装"))
    for line in sys.stdin:
        srv.handle_line(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

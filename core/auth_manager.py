# -*- coding: utf-8 -*-
"""core.auth_manager —— 「登录头管理」窗口业务核(自 app.py 剥离,零 tkinter)。

UI(app.open_auth_manager)只保留 Toplevel/Treeview/按钮/Spinbox 等控件与
win.after 轮询;本模块承载全部业务:源列表过滤、Cookie/header 合并保存、
CDP 抓取编排(单站抓取 + 批量手动逐站 + 全自动三段式状态机)。

事件契约:worker 线程一律 emit((kind, payload)) 单元组,由 UI 侧的窗口局部
队列在 poll 轮询里消费(与 core 其他模块同模式;这些 kind 只属于登录头
窗口,与 App 级 15 种事件契约无关):
  log            msg                  → UI 日志(self.log)
  stat           (msg, color)         → 状态栏标签
  launched       warn(str)            → 浏览器已打开(单站或手动逐站当前站)
  captured       cookie(str)          → 单站抓到 Cookie(批量模式不经此事件)
  error          msg                  → 失败(UI 负责批量中止/单站复位)
  batch_opening  (idx1, total, host)  → 手动逐站:正在打开第 idx1 站
  batch_done     msg                  → 手动逐站自然结束(UI batch_finish)
  auto_done      dict(n_instant, n_para, n_login, n_login_got, stopped)
  login_stage    (n_instant, n_para, n_left, k) → 进入并行登录扫(一次性入口)
  pprog          (done, total, k)     → 并行扫进度
  lprog          (done, total, k, got, skip, cur_host, cur_idle)
                                      → 并行登录扫进度(暂停时不发,保住暂停文案)

线程纪律:所有 CDP 调用都在 worker 线程;控制入口(begin/launch/grab_current/
skip_site/request_stop/toggle_pause/finish/abort)只置标志或派线程,可从
tk 主线程直接调用;UI 永不直接触碰 cdp_cookie。
"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cdp_cookie
from legado import engine

from core.config import APP_DIR, AUTO_IDLE_SECS, AUTO_PARA_SETTLE, AUTO_PARA_TABS
from core.artifacts import (_url_host, _merge_cookie_str, _host_match,
                            _para_idle, _tab_due, save_auth_state)


# ------------------------------------------------------------- 纯逻辑函数 ----


def parse_header(raw):
    """header 输入解析:JSON 优先,失败回退 ast(容错单引号/无引号写法)。"""
    if not raw:
        return {}
    header = None
    try:
        header = json.loads(raw)
    except Exception:
        try:
            import ast
            header = ast.literal_eval(raw)
        except Exception:
            header = None
    return header if isinstance(header, dict) else None


def filter_sources(sources, auth, kw):
    """源列表收集与过滤:按名称/URL 子串(不区分大小写)过滤,已配置登录头
    的源加「● 」标记。返回 [(标记后名称, url)] 列表,顺序保持原表。"""
    kw = (kw or "").strip().lower()
    out = []
    for s in sources:
        nm = (s.get("bookSourceName") or "").strip()
        u = (s.get("bookSourceUrl") or "").strip()
        if kw and kw not in nm.lower() and kw not in u.lower():
            continue
        out.append((("● " if u in auth else "") + nm, u))
    return out


def collect_targets(urls, auth, skip_configured):
    """批量抓取目标收集:URL 按站点主机去重(无协议补 http://),可跳过已配
    Cookie 的站点(重跑批量秒级)。返回 (targets{host: {url}}, skipped, n_all)。"""
    from urllib.parse import urlsplit
    targets_all = {}
    for u in urls:
        u = u.strip()
        if "://" not in u:
            u = "http://" + u
        h = (urlsplit(u).hostname or "").lower()
        if h:
            targets_all.setdefault(h, set()).add(u)
    n_all = len(targets_all)
    targets = targets_all
    skipped = 0
    if skip_configured:
        targets = {h: us for h, us in targets_all.items()
                   if not any((auth.get(u) or {}).get("cookie") for u in us)}
        skipped = n_all - len(targets)
    return targets, skipped, n_all


def build_login_map(sources):
    """host → 纯网址 loginUrl(登录页常在 passport 等子域;JS loginUrl 不可直接
    打开,排除)。每主机只记第一个出现的登录页。"""
    login_map = {}
    for s in sources:
        b = (s.get("bookSourceUrl") or "").strip()
        h = _url_host(b if "://" in b else "https://" + b)
        lu = (s.get("loginUrl") or "").strip()
        if h and h not in login_map and lu.startswith(("http://", "https://")) \
                and not re.search(r"<js>|@js:", lu, re.I):
            login_map[h] = lu
    return login_map


def apply_to_urls(auth, urls, cookie, header):
    """「保存到所选」:cookie/header 至少一项非空 → 整项覆盖写入;两项都空
    = 移除所选源的配置(原语义)。只改内存 dict,落盘/注入引擎用 persist。"""
    if cookie or header:
        item = {}
        if cookie:
            item["cookie"] = cookie
        if header:
            item["header"] = header
        for u in urls:
            auth[u] = dict(item)
    else:
        for u in urls:                       # 两项都空 = 移除所选源的配置
            auth.pop(u, None)


def remove_urls(auth, urls):
    """「删除所选」:移除已配置的源,返回实际删除的 url 列表。"""
    removed = [u for u in urls if u in auth]
    for u in removed:
        auth.pop(u, None)
    return removed


def persist(auth):
    """登录头落盘(shuyuan/auth_state.json)并注入引擎(校验/搜索/详情/目录/
    正文全链路生效)。"""
    save_auth_state(auth)
    engine.set_auth(auth)


def _log_via(emit, msg):
    emit(("log", msg))


# ------------------------------------------------------------- 编排状态机 ----


class AuthManager:
    """登录头抓取编排:单站 CDP 抓取 + 批量(手动逐站 / 全自动三段式)状态机。

    三段式(自动模式,v1.8.1):
      Phase0 秒过   浏览器已有 Cookie 的域,拆库直存,零访问;
      Phase1 并行扫 其余站点 K 路标签并行访问抓取;
      Phase2 并行登录扫 仍没抓到的站进入 K 路标签池持续补位的登录扫,
                    用户在页面里登录即零点击抓走,不再回串行。
    """

    def __init__(self, auth, emit, profile_dir=None):
        self.auth = auth                 # 与 UI 共享同一 dict(●标记/列表刷新读它)
        self.emit = emit                 # emit((kind, payload))
        self.profile = profile_dir or (APP_DIR / "auth_profile")
        self.handle = None               # CDP 浏览器句柄(单站/批量共用)
        # 状态机字段(原 open_auth_manager 的 batch dict)
        self.active = False
        self.auto = False
        self.paused = False
        self.stop_req = False
        self.stage = "idle"              # idle / serial / para / login
        self.hosts, self.targets, self.login_map = [], {}, {}
        self.idx = -1
        self.k = AUTO_PARA_TABS
        self.para_done = self.para_total = 0
        self.login_total = self.login_done = self.login_got = self.login_skip = 0
        self.cur_host = ""
        self.cur_idle = AUTO_IDLE_SECS

    # -------------------------------------------------- 生命周期/控制入口 ----

    def close_browser(self):
        """关掉我们拉起的抓取浏览器(幂等)。"""
        if self.handle:
            cdp_cookie.close(self.handle)
        self.handle = None

    def finish(self):
        """批量收尾的业务半边(原 batch_finish 前半):复位状态机 + 关浏览器。
        按钮复位/列表刷新/日志由 UI 完成。"""
        self.active = False
        self.stage = "idle"
        self.stop_req = False
        self.close_browser()

    def abort(self):
        """窗口关闭:停扫 + 关浏览器。"""
        self.active = False
        self.close_browser()

    def toggle_pause(self):
        """暂停/继续:冻结并行登录扫全部倒计时(暂停时长顺延各标签时间戳)。"""
        self.paused = not self.paused
        return self.paused

    def request_stop(self):
        """「结束批量」(并行登录扫阶段):置 stop_req 后由工作线程关池内标签
        收尾,已抓到的保留。"""
        self.stop_req = True

    def begin(self, hosts, targets, login_map, k, auto):
        """批量参数落位(原 batch_start 后半,不含任何 UI/确认框)。"""
        self.hosts, self.targets, self.login_map = hosts, targets, login_map
        self.idx = -1
        self.active = True
        self.auto = auto
        self.paused = False
        self.stop_req = False
        self.k = k
        if auto:
            self.stage = "para"
            self.para_done = 0
            self.para_total = len(hosts)
        else:
            self.stage = "serial"

    def launch(self):
        """begin 之后派发:自动模式起工作线程跑三段式;手动逐站打开第一站。"""
        if self.auto:
            threading.Thread(target=self._run_auto, daemon=True).start()
        else:
            self.next_site()

    # -------------------------------------------------------- 单站抓取 -------

    def launch_single(self, url):
        """单站「浏览器登录抓取」第一步:弹浏览器打开登录页(worker 线程)。
        发 launched(warn)/error 事件。"""
        def _launch():
            try:
                self.handle = cdp_cookie.launch_for_auth(url, self.profile)
                self.emit(("launched",
                           cdp_cookie.page_mismatch(self.handle, url) or ""))
            except Exception as e:
                self.emit(("error", str(e)))

        threading.Thread(target=_launch, daemon=True).start()

    def grab_single(self, host):
        """单站第二步:从浏览器按主机抓 Cookie(worker 线程)。
        发 captured(cookie)/error 事件。"""
        handle = self.handle

        def _grab():
            try:
                self.emit(("captured", cdp_cookie.fetch_cookies(handle, host)))
            except Exception as e:
                self.emit(("error", "抓取失败: %s" % e))

        threading.Thread(target=_grab, daemon=True).start()

    # -------------------------------------------------------- 手动逐站 -------

    def next_site(self):
        """推进到下一站(原 batch_next):发 batch_opening 并开页;越界即发
        batch_done 结束。"""
        self.idx += 1
        if self.idx >= len(self.hosts):
            self.emit(("batch_done",
                       "批量抓取结束: 共处理 %d 个站点,Cookie 已全部注入。"
                       % len(self.hosts)))
            return
        host = self.hosts[self.idx]
        self.emit(("batch_opening", (self.idx + 1, len(self.hosts), host)))
        self.open_site()

    def skip_site(self):
        """「跳过该站」(原 batch_skip):并行阶段无单站语义,提示后不动;
        串行阶段直接跳下一站。"""
        if self.stage in ("para", "login"):
            self.emit(("stat", ("并行阶段进行中, 不支持单站跳过;超时的站会自动跳过,"
                                "也可点「结束批量」。", "#cc0000")))
            return
        self.next_site()

    def open_site(self):
        """(重新)打开当前站点的页面(worker 线程);浏览器死了会自动重开。

        自动模式不做 page_mismatch 阻塞核验(最多等 8s)—— 倒计时 tick
        本来就观察页面 URL,打不开的站静默期满自然跳过;手动模式保留核验警告。
        """
        host = self.hosts[self.idx]
        url = self.login_map.get(host) or sorted(self.targets[host])[0]

        def _open():
            try:
                if self.handle is None or not cdp_cookie.is_alive(self.handle):
                    if self.handle:
                        cdp_cookie.close(self.handle)
                    self.handle = cdp_cookie.launch_for_auth(url, self.profile)
                else:
                    cdp_cookie.navigate(self.handle, url)
                warn = "" if self.auto else \
                    cdp_cookie.page_mismatch(self.handle, url)
                self.emit(("launched", warn))
            except Exception as e:
                self.emit(("error", "打开 %s 失败: %s" % (host, e)))

        threading.Thread(target=_open, daemon=True).start()

    def grab_current(self):
        """手动逐站「抓取本站」(原 batch_grab 的 worker 部分):抓 Cookie →
        合并保存到该站全部源 → 跳下一站。浏览器被手动关闭时不中止批量,
        重新打开当前站点。"""
        host = self.hosts[self.idx]
        handle = self.handle

        def _grab():
            try:
                if handle is None or not cdp_cookie.is_alive(handle):
                    self.handle = None
                    self._log("批量: 检测到抓取浏览器已关闭,正在重新打开 %s"
                              % host)
                    self.open_site()
                    return
                ck = cdp_cookie.fetch_cookies(handle, host)
            except Exception as e:
                self.emit(("error", "抓取失败: %s" % e))
                return
            if self.active:
                self._capture_and_next(host, ck)
            else:
                self.emit(("captured", ck))   # 批量已结束的迟到结果,回单站语义

        threading.Thread(target=_grab, daemon=True).start()

    def _capture_and_next(self, host, cookie):
        """抓到的 Cookie 合并进该站全部源并落盘/注入,然后跳下一站(worker 线程)。"""
        n = 0
        if cookie:
            for u in self.targets[host]:
                cfg = dict(self.auth.get(u) or {})
                cfg["cookie"] = _merge_cookie_str(cfg.get("cookie"), cookie)
                self.auth[u] = cfg
                n += 1
            persist(self.auth)
            self._log("批量: 已保存 %s 的 Cookie(%d 个源)" % (host, n))
        else:
            self._log("批量: %s 未抓到 Cookie(可能打不开/不是目标站),已跳过"
                      % host)
        self.next_site()

    # ---------------------------------------------------- 全自动三段式 -------

    def _run_auto(self):
        """三段式主流程(原 _auto_run,worker 线程):Phase0 秒过 → Phase1
        并行扫 → Phase2 并行登录扫,终点发 auto_done 汇总。"""
        try:
            first = self.hosts[0]
            self.handle = cdp_cookie.launch_for_auth(
                sorted(self.targets[first])[0], self.profile)
            # —— Phase 0: 浏览器已有 Cookie 的域, 拆库直存, 零访问 ——
            allc = cdp_cookie.all_cookies(self.handle)
            instant, remaining = {}, []
            for h in self.hosts:
                cks = [c for c in allc
                       if cdp_cookie._domain_matches(h, c.get("domain") or "")]
                if cks:
                    instant[h] = "; ".join(
                        "%s=%s" % (c["name"], c["value"]) for c in cks)
                else:
                    remaining.append(h)
            for h, ck in instant.items():
                for u in self.targets[h]:
                    cfg = dict(self.auth.get(u) or {})
                    cfg["cookie"] = _merge_cookie_str(cfg.get("cookie"), ck)
                    self.auth[u] = cfg
            if instant:
                persist(self.auth)
            # —— Phase 1: 其余站点并行扫 ——
            self.emit(("pprog", (0, self.para_total, self.k)))
            results = self._para_sweep(remaining) if remaining else {}
            got = sum(1 for ck in results.values() if ck)
            for h, ck in results.items():
                if ck:
                    for u in self.targets[h]:
                        cfg = dict(self.auth.get(u) or {})
                        cfg["cookie"] = _merge_cookie_str(cfg.get("cookie"), ck)
                        self.auth[u] = cfg
            if got:
                persist(self.auth)
            # —— Phase 2: 仍没抓到的进入并行登录扫(标签池持续补位,
            #    跳过逻辑并入硬时限, 不再回串行) ——
            left = [h for h in remaining if not results.get(h)]
            n_login_got = 0
            if left and self.active:
                self.stage = "login"
                self.emit(("login_stage", (len(instant), got, len(left), self.k)))
                results2 = self._login_sweep(self.handle, left)
                n_login_got = sum(1 for ck in results2.values() if ck)
            self.emit(("auto_done", {
                "n_instant": len(instant),
                "n_para": got, "n_login": len(left),
                "n_login_got": n_login_got,
                "stopped": bool(self.stop_req) or not self.active}))
        except Exception as e:
            self.emit(("error", str(e)))

    def _para_sweep(self, hosts):
        """Phase 1 并行扫:k 个标签同时开, 每站加载+settle 后按域取 Cookie
        并关标签。返回 {host: cookie 字符串}(空串=没抓到)。"""
        k = max(1, self.k)
        results, lock = {}, threading.Lock()

        def one(h):
            url = sorted(self.targets[h])[0]
            ck = ""
            try:
                tid = cdp_cookie.open_tab(self.handle, url)
            except Exception:
                pass
            else:
                t0 = time.time()
                committed = False
                while time.time() - t0 < 8 and self.active:
                    ph = _url_host(cdp_cookie.tab_url(self.handle, tid))
                    if _host_match(ph, h):
                        committed = True
                        break
                    time.sleep(0.3)
                time.sleep(AUTO_PARA_SETTLE if committed else 0.5)
                try:
                    allc = cdp_cookie.all_cookies(self.handle)
                    ck = "; ".join("%s=%s" % (c["name"], c["value"])
                                   for c in allc
                                   if cdp_cookie._domain_matches(
                                       h, c.get("domain") or ""))
                except Exception:
                    ck = ""
                cdp_cookie.close_tab(self.handle, tid)
            with lock:
                results[h] = ck
                self.para_done += 1
                self.emit(("pprog", (self.para_done, self.para_total, self.k)))

        with ThreadPoolExecutor(max_workers=k) as ex:
            list(ex.map(one, hosts))
        return results

    def _login_sweep(self, handle, hosts):
        """Phase 2 并行登录扫:K 路标签池持续补位, 每标签独立监控(0.5s 一拍)。

        到站(url 主机命中目标域)且该标签静默满 idle 秒(分级: 已配过 Cookie
        1.5s / 全新站 5s)→ all_cookies 按域抓取保存(该站全部源),关标签补位;
        没抓到也关标签补位;始终没到站超过硬时限 max(10s, 3×idle) → 跳过补位
        (死站不再逐站干等)。用户在页面里登录(点击/输入/页面跳转)重置该标签
        倒计时,静默期满自动抓走 —— 全程零点击。「暂停」冻结全部倒计时
        (暂停时长顺延各标签时间戳),「结束批量」置 stop_req 后关池内标签收尾。
        返回 {host: cookie}(空串=没抓到/跳过)。
        """
        k = max(1, int(self.k or AUTO_PARA_TABS))
        results = {}
        waitq = list(hosts)
        pool = []      # 池内标签:{tid, host, idle, arrived, opened_t, last_act, act_t}
        self.login_total = len(hosts)
        self.login_done = self.login_got = self.login_skip = 0
        last_emit = 0.0
        paused_at = None
        try:
            while (waitq or pool) and self.active and not self.stop_req:
                now = time.time()
                if self.paused:
                    paused_at = paused_at or now   # 冻结:不推进任何计时
                    time.sleep(0.3)
                    continue
                if paused_at is not None:          # 恢复:暂停时长顺延全部倒计时
                    back = now - paused_at
                    paused_at = None
                    for s in pool:
                        s["opened_t"] += back
                        s["last_act"] += back
                # 补位:池有空位且队列有站 → 开下一个标签
                while len(pool) < k and waitq and self.active \
                        and not self.stop_req:
                    h = waitq.pop(0)
                    url = self.login_map.get(h) \
                        or sorted(self.targets[h])[0]  # 有登录页先开登录页
                    configured = any((self.auth.get(u) or {}).get("cookie")
                                     for u in self.targets[h])
                    idle = _para_idle(configured)
                    try:
                        tid = cdp_cookie.open_tab(handle, url)
                    except Exception as e:
                        self._log("并行登录: 打不开 %s(%s),已跳过" % (h, e))
                        results[h] = ""
                        self.login_done += 1
                        self.login_skip += 1
                        continue
                    self.cur_host = h          # 状态栏提示跟着新标签走
                    self.cur_idle = idle
                    pool.append({"tid": tid, "host": h, "idle": idle,
                                 "arrived": False, "opened_t": now,
                                 "last_act": now, "act_t": 0.0})
                if not pool:
                    time.sleep(0.3)
                    continue
                time.sleep(0.5)                # 监控一拍 0.5s
                try:
                    tmap = cdp_cookie.tab_urls(handle)  # 一次取全部标签 URL
                except Exception:
                    tmap = {}
                if not tmap:
                    if not cdp_cookie.is_alive(handle):
                        self._log("⚠ 并行登录: 抓取浏览器已关闭,剩余站点跳过。")
                        for s in pool:
                            results[s["host"]] = ""
                            self.login_done += 1
                            self.login_skip += 1
                        pool = []
                        for h in waitq:
                            results[h] = ""
                            self.login_done += 1
                            self.login_skip += 1
                        waitq = []
                        break
                    continue                   # 瞬时抖动: 下一拍重试
                now = time.time()
                for s in pool:                 # 到站判定
                    ph = _url_host(tmap.get(s["tid"]) or "")
                    if _host_match(ph, s["host"]):
                        s["arrived"] = True
                # 活动检测节流:每标签最低 1.5s 一次, 一拍最多查 2 个(CDP 省调用)
                for s in sorted(pool, key=lambda x: x["act_t"])[:2]:
                    if now - s["act_t"] < 1.5:
                        break
                    s["act_t"] = now
                    try:
                        ms = cdp_cookie.page_activity(handle, s["tid"])
                    except Exception:
                        ms = 0
                    if ms and ms / 1000.0 > s["last_act"]:
                        s["last_act"] = ms / 1000.0   # 页面内点击/输入 = 活动
                for s in list(pool):
                    verdict = _tab_due(s["arrived"], s["idle"],
                                       now - s["last_act"], now - s["opened_t"])
                    if verdict != "wait" and now - s["act_t"] > 0.3:
                        # 判定前补一次新鲜活动检测: 刚点击/输入过的标签不抢抓
                        s["act_t"] = now
                        try:
                            ms = cdp_cookie.page_activity(handle, s["tid"])
                        except Exception:
                            ms = 0
                        if ms and ms / 1000.0 > s["last_act"]:
                            s["last_act"] = ms / 1000.0
                        verdict = _tab_due(s["arrived"], s["idle"],
                                           now - s["last_act"],
                                           now - s["opened_t"])
                    if verdict == "wait":
                        continue
                    ck = ""
                    if verdict == "grab":
                        try:
                            allc = cdp_cookie.all_cookies(handle)
                            ck = "; ".join(
                                "%s=%s" % (c["name"], c["value"])
                                for c in allc
                                if cdp_cookie._domain_matches(
                                    s["host"], c.get("domain") or ""))
                        except Exception:
                            ck = ""
                    cdp_cookie.close_tab(handle, s["tid"])
                    pool.remove(s)
                    self.login_done += 1
                    if ck:
                        n = 0
                        for u in self.targets[s["host"]]:
                            cfg = dict(self.auth.get(u) or {})
                            cfg["cookie"] = _merge_cookie_str(
                                cfg.get("cookie"), ck)
                            self.auth[u] = cfg
                            n += 1
                        try:
                            persist(self.auth)
                        except Exception:
                            pass
                        results[s["host"]] = ck
                        self.login_got += 1
                        self._log("并行登录: 已保存 %s 的 Cookie(%d 个源)"
                                  % (s["host"], n))
                    else:
                        results[s["host"]] = ""
                        self.login_skip += 1
                        why = ("超时未到站(⚠ 页面不符或打不开),已跳过"
                               if verdict == "timeout"
                               else "未抓到 Cookie,已跳过")
                        page = tmap.get(s["tid"]) or ""
                        if verdict == "grab" and page:
                            why += "(页面在 %s)" % page   # 便于诊断为何没抓到
                        self._log("并行登录: %s %s" % (s["host"], why))
                # 进度事件:1s 节流;暂停时停发(保住「已暂停」文案不被刷掉)
                if not self.paused and now - last_emit >= 1.0:
                    last_emit = now
                    self.emit(("lprog", (self.login_done, self.login_total,
                                         self.k, self.login_got,
                                         self.login_skip, self.cur_host,
                                         self.cur_idle)))
        except Exception as e:                 # 任何异常不许带走整个批量
            self._log("⚠ 并行登录扫异常(已收尾): %s" % e)
            for s in pool:
                cdp_cookie.close_tab(handle, s["tid"])
                results.setdefault(s["host"], "")
        for s in pool:                         # 收尾:关掉池内全部标签
            cdp_cookie.close_tab(handle, s["tid"])
        return results

    def _log(self, msg):
        _log_via(self.emit, msg)

# -*- coding: utf-8 -*-
"""浏览器登录抓取 Cookie:弹默认浏览器(独立临时配置)→ 用户登录 → CDP 取 Cookie。

流程(配合 GUI「登录头」管理窗口):
  1. launch_for_auth(site_url, profile_dir) —— 用本机 Chrome/Edge 内核浏览器以
     --remote-debugging-port 打开源站点,返回 handle;profile_dir 复用,登录态
     跨次保留。独立 user-data-dir 是必须的:浏览器已在运行时,默认配置下新开
     窗口会并入既有进程,调试端口会被忽略。
  2. 用户在弹出的浏览器里正常登录,回程序点「我登录好了」。
  3. fetch_cookies(handle, host) —— 经 CDP Storage.getCookies 抓全部 Cookie
     (含 httpOnly,F12 手动复制反而拿不全),按域名过滤后拼成 Cookie 头字符串。
  4. close(handle) —— 关掉我们拉起的浏览器。

只接受 CDP 兼容内核(chrome/msedge/chromium);默认浏览器是 Firefox 等时自动
回退 Edge → Chrome。依赖 websocket-client(延迟 import,缺失由 GUI 提示安装)。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import winreg


def _default_browser_exe():
    """从注册表解析默认浏览器 exe;解析不出或非 CDP 兼容内核返回 None。"""
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\https\UserChoice") as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            prog_id + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        m = re.search(r'"?(.+?\.(?:exe|EXE))"?', cmd)
        if not m:
            return None
        exe = m.group(1)
        if os.path.basename(exe).lower() in ("chrome.exe", "msedge.exe",
                                             "chromium.exe"):
            return exe
    except Exception:
        pass
    return None


def _fallback_browser_exe():
    """默认浏览器不可用时按 Edge → Chrome → PATH 找 CDP 兼容浏览器。"""
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("msedge"), shutil.which("chrome"), shutil.which("chromium"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def find_browser_exe():
    """默认浏览器优先,非 CDP 内核/解析失败回退 Edge→Chrome;找不到返回 None。"""
    return _default_browser_exe() or _fallback_browser_exe()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _kill_profile_processes(profile_dir):
    """强杀仍占用本 profile 的浏览器残留进程。

    只按命令行里的 profile 路径过滤 —— 绝不碰用户日常使用的浏览器。
    terminate() 是异步的,主进程死后残留子进程可能短暂占着 profile 锁,
    导致下一次 launch 的新进程"启动即退出"。
    """
    try:
        pat = str(profile_dir).replace("'", "''")
        ps = ("Get-CimInstance Win32_Process "
              "-Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
              "Where-Object { $_.CommandLine -like '*%s*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % pat)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=20)
    except Exception:
        pass


_last_handle = None          # 本模块当前拉起的浏览器会话(同一 profile 只能有一个)


def launch_for_auth(site_url, profile_dir, timeout=30):
    """拉起独立配置的浏览器打开 site_url,返回 handle(dict)供 fetch_cookies/close。

    handle = {"proc": Popen, "port": int, "ws_url": str}。浏览器起不来/调试端口
    不通超时,抛 RuntimeError。
    """
    global _last_handle
    if _last_handle is not None:      # 同 profile 已有会话:先关,否则新进程
        close(_last_handle)           # 会把请求交给旧实例后立刻退出
        _last_handle = None
    exe = find_browser_exe()
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge 浏览器,请手动粘贴 Cookie。")
    site_url = normalize_url(site_url)
    os.makedirs(profile_dir, exist_ok=True)
    import urllib.request
    last_err = None
    for _attempt in range(3):                # 同 profile 旧实例未死透会顶掉新进程
        port = _free_port()
        proc = subprocess.Popen(
            [exe, "--user-data-dir=" + str(profile_dir),
             "--remote-debugging-port=%d" % port,
             "--remote-allow-origins=*",     # Chrome 111+ 默认拒绝 WS 握手
             "--no-first-run", "--no-default-browser-check",
             "--window-size=960,860", site_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                last_err = "浏览器启动后立即退出了(可能有同配置的旧实例未退出)"
                break
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/version" % port, timeout=2) as r:
                    ws_url = json.loads(r.read().decode("utf-8"))[
                        "webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.5)
        if ws_url:
            handle = {"proc": proc, "port": port, "ws_url": ws_url}
            _last_handle = handle
            return handle
        try:
            proc.terminate()
        except Exception:
            pass
        _kill_profile_processes(profile_dir)   # 清残留后重试
        time.sleep(1.0)
    raise RuntimeError(last_err or "浏览器调试端口未就绪(超时)。")


def _domain_matches(host, cookie_domain):
    """标准 Cookie 域匹配:host 与 domain(去前导点)相等或为其子域。"""
    d = cookie_domain.lstrip(".").lower()
    h = (host or "").lower()
    return h == d or h.endswith("." + d)


def normalize_url(url):
    """清洗站点网址:去空格/引号/#片段,缺协议头补 https://;无效则抛 RuntimeError。

    书源包里 bookSourceUrl 脏值不少(尾部空格、无协议头、#后带乱码)——
    不清洗就传给浏览器会打不开目标站,Edge 会兜底显示自己的主页,
    用户看到的就是"跳到了 Edge 主页,什么都没发生"。
    """
    from urllib.parse import urlsplit
    u = (url or "").strip().strip("\"'").split("#", 1)[0].strip()
    if not u:
        raise RuntimeError("网址为空。")
    if "://" not in u:
        u = "https://" + u
    if not (urlsplit(u).hostname or "").strip():
        raise RuntimeError("网址无效(解析不出主机名): %s" % url)
    return u


def page_urls(handle):
    """当前全部页面标签的 URL(诊断:确认浏览器真到了目标站)。"""
    try:
        return [t.get("url") or "" for t in _http_json(handle["port"], "/json/list")
                if t.get("type") == "page"]
    except Exception:
        return []


def page_mismatch(handle, url, wait=8):
    """等待并检查浏览器页面是否落在目标主机。返回 ""(正常)或警告文本。"""
    from urllib.parse import urlsplit
    host = (urlsplit(normalize_url(url)).hostname or "").lower()
    deadline = time.time() + wait
    while time.time() < deadline:
        for p in page_urls(handle):
            ph = (urlsplit(p).hostname or "").lower()
            if ph and (ph == host or ph.endswith("." + host)
                       or host.endswith("." + ph)):
                return ""
        time.sleep(0.5)
    return ("注意: 浏览器打开的页面不是 %s —— 可能网址打不开,落到了起始页,"
            "Cookie 会抓不到。" % host)


def _http_json(port, path):
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def _cdp_call(handle, msg_id, method, params=None, timeout=8):
    """浏览器级 CDP 单次调用(连接→发送→等对应 id 的响应)。"""
    from websocket import create_connection
    ws = create_connection(handle["ws_url"], timeout=timeout)
    try:
        ws.send(json.dumps({"id": msg_id, "method": method,
                            "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                return resp
        raise RuntimeError("CDP 响应超时: %s" % method)
    finally:
        ws.close()


def is_alive(handle):
    """浏览器进程还在且调试端口可达(用户手动关窗/进程崩溃都会变 False)。"""
    if not handle or handle["proc"].poll() is not None:
        return False
    try:
        _http_json(handle["port"], "/json/version")
        return True
    except Exception:
        return False


def page_activity(handle):
    """注入页面活动监听(幂等)并返回最近一次用户活动的时间戳(ms, 无则 0)。

    监听 click/keydown/submit/touchstart/change —— 用户在页面里点击/输入
    会刷新 __bd_a;自动批量模式据此判断"用户还在操作"以暂停倒计时。
    需要 attach 到页面 target 的会话执行 Runtime.evaluate。
    """
    try:
        from websocket import create_connection
        pages = [t for t in _http_json(handle["port"], "/json/list")
                 if t.get("type") == "page"]
        if not pages:
            return 0
        ws = create_connection(handle["ws_url"], timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "Target.attachToTarget",
                                "params": {"targetId": pages[0]["id"],
                                           "flatten": True}}))
            sid = None
            deadline = time.time() + 5
            while time.time() < deadline:
                resp = json.loads(ws.recv())
                if resp.get("id") == 1:
                    sid = (resp.get("result") or {}).get("sessionId")
                    break
            if not sid:
                return 0
            js = ("(function(){if(!window.__bd_w){window.__bd_w=1;"
                  "window.__bd_a=Date.now();"
                  "['click','keydown','submit','touchstart','change']"
                  ".forEach(function(t){document.addEventListener(t,"
                  "function(){window.__bd_a=Date.now();},true);});}"
                  "return window.__bd_a||0;})()")
            ws.send(json.dumps({"id": 2, "sessionId": sid,
                                "method": "Runtime.evaluate",
                                "params": {"expression": js,
                                           "returnByValue": True}}))
            deadline = time.time() + 5
            while time.time() < deadline:
                resp = json.loads(ws.recv())
                if resp.get("id") == 2:
                    v = ((resp.get("result") or {}).get("result") or {}).get("value")
                    try:
                        return int(v or 0)
                    except Exception:
                        return 0
            return 0
        finally:
            ws.close()
    except Exception:
        return 0


def open_tab(handle, url):
    """新开一个标签页打开 url(不关闭其它标签 —— 并行抓取用)。返回 targetId。"""
    url = normalize_url(url)
    resp = _cdp_call(handle, 1, "Target.createTarget", {"url": url})
    tid = (resp.get("result") or {}).get("targetId")
    if not tid:
        raise RuntimeError("打开新标签页失败。")
    return tid


def close_tab(handle, target_id):
    """关闭指定标签页(忽略已关闭的情况)。"""
    try:
        _cdp_call(handle, 2, "Target.closeTarget", {"targetId": target_id},
                  timeout=5)
    except Exception:
        pass


def tab_url(handle, target_id):
    """指定标签页当前 URL(未找到返回空串)—— 并行扫的提交判断。"""
    try:
        for t in _http_json(handle["port"], "/json/list"):
            if t.get("id") == target_id:
                return t.get("url") or ""
    except Exception:
        pass
    return ""


def all_cookies(handle):
    """浏览器全部 Cookie(原始列表, 浏览器级一次调用)。"""
    try:
        resp = _cdp_call(handle, 3, "Storage.getCookies", {})
        return (resp.get("result") or {}).get("cookies") or []
    except Exception:
        return []


def navigate(handle, url):
    """在既有浏览器中新开标签页打开 url,并关闭其它页面标签(单标签模式)。

    批量抓取换站用:浏览器与登录态保持,只换页面。返回新 targetId。
    """
    url = normalize_url(url)
    resp = _cdp_call(handle, 1, "Target.createTarget", {"url": url})
    new_id = (resp.get("result") or {}).get("targetId")
    if not new_id:
        raise RuntimeError("打开新标签页失败。")
    try:                                     # 收掉旧标签,防站点多时堆内存
        for t in _http_json(handle["port"], "/json/list"):
            if t.get("type") == "page" and t.get("id") != new_id:
                try:
                    _cdp_call(handle, 2, "Target.closeTarget",
                              {"targetId": t["id"]}, timeout=5)
                except Exception:
                    pass
    except Exception:
        pass
    return new_id


def fetch_cookies(handle, host):
    """经 CDP 抓浏览器全部 Cookie,过滤出 host 的,拼成 "k=v; k2=v2" 字符串。"""
    try:
        from websocket import create_connection      # websocket-client
    except ImportError:
        raise RuntimeError("缺少 websocket-client 库: pip install websocket-client")
    ws = create_connection(handle["ws_url"], timeout=8)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies",
                            "params": {}}))
        deadline = time.time() + 8
        while time.time() < deadline:
            resp = json.loads(ws.recv())
            if resp.get("id") == 1:
                break
        else:
            raise RuntimeError("CDP 响应超时。")
    finally:
        ws.close()
    cookies = (resp.get("result") or {}).get("cookies") or []
    pairs = ["%s=%s" % (c["name"], c["value"]) for c in cookies
             if _domain_matches(host, c.get("domain") or "")]
    return "; ".join(pairs)


def close(handle):
    """优雅关闭抓取浏览器(CDP Browser.close 会把内存 Cookie 落盘),
    失败再 terminate 兜底 —— 强杀会丢 Cookie, 重跑时 Phase 0 就没得秒过了。"""
    global _last_handle
    if handle is _last_handle:
        _last_handle = None
    try:
        _cdp_call(handle, 9, "Browser.close", {}, timeout=3)
    except Exception:
        pass
    try:
        handle["proc"].terminate()
    except Exception:
        pass

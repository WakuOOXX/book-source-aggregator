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
    os.makedirs(profile_dir, exist_ok=True)
    port = _free_port()
    proc = subprocess.Popen(
        [exe, "--user-data-dir=" + str(profile_dir),
         "--remote-debugging-port=%d" % port,
         "--remote-allow-origins=*",     # Chrome 111+ 默认拒绝 WS 握手
         "--no-first-run", "--no-default-browser-check",
         "--window-size=960,860", site_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request
    deadline = time.time() + timeout
    ws_url = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("浏览器启动后立即退出了。")
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/version" % port, timeout=2) as r:
                ws_url = json.loads(r.read().decode("utf-8"))[
                    "webSocketDebuggerUrl"]
            break
        except Exception:
            time.sleep(0.5)
    if not ws_url:
        proc.terminate()
        raise RuntimeError("浏览器调试端口未就绪(超时)。")
    handle = {"proc": proc, "port": port, "ws_url": ws_url}
    _last_handle = handle
    return handle


def _domain_matches(host, cookie_domain):
    """标准 Cookie 域匹配:host 与 domain(去前导点)相等或为其子域。"""
    d = cookie_domain.lstrip(".").lower()
    h = (host or "").lower()
    return h == d or h.endswith("." + d)


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
    """关掉本模块拉起的浏览器进程(用户自己关了也不报错)。"""
    global _last_handle
    if handle is _last_handle:
        _last_handle = None
    try:
        handle["proc"].terminate()
    except Exception:
        pass

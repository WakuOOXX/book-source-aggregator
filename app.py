# -*- coding: utf-8 -*-
"""小说下载器 —— 桌面 GUI(输入书名 → 选书 → 导出 TXT/EPUB)。"""
import json
import re
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import ttk, filedialog, messagebox, simpledialog
from pathlib import Path


import cdp_cookie
from selkit import TreeMultiSelect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from legado import engine, export, fetcher, jsengine
from legado.fetcher import DEFAULT_UA
from legado.normalize import merge_hits, dedupe_hits, dedupe_sources
from selpolicy import (MIN_DRAG, DOUBLE_MS, apply_click, apply_range,
                       apply_rubber, restore_filter)

try:                                       # 校验自签名站时不刷 InsecureRequestWarning
    from urllib3 import disable_warnings
    disable_warnings()
except Exception:
    pass

if getattr(sys, "frozen", False):          # PyInstaller 打包后:资源文件放 exe 同目录
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
SOURCE_DIR = APP_DIR / "shuyuan"           # 书源 JSON 统一放这里
DEFAULT_SOURCE = SOURCE_DIR / "bookSource.json"
DEFAULT_OUT = APP_DIR / "downloads"
STATE_FILE = APP_DIR / "sel_state.json"    # 多选/选中项记忆 + 校验原始表路径(见 _mem_*)

# ---------------------------------------------------------------- 并发度 -----
# 搜索与校验:每个书源只发 1 个请求,3393 个源分布在 2107 个域名上,
# 对单个站的压力不随总并发上升,所以可以开大。
# 实测(2026-09-10,全量 3393 源、12 逻辑核,关键词「剑来」):
#   并发  40 → 90.2s      并发 256 → 28.2 / 28.3s
#   并发 384 → 18.3 / 20.4s   并发 512 → 18.7 / 42.1s(开始不稳)
# CPU 全程只占 1~1.4/12 核 —— 瓶颈是"最慢单源最多等 12s"的超时尾巴,
# 不是算力。384 是收益/稳定性的拐点,再加只会放大尾部波动。
# 注意:小批量搜索(十几个到几百个源)提并发没有意义,因为总耗时会撞上
# 12s 超时下界:实测 240 个源在并发 40~600 之间都是 12s 左右。
SEARCH_WORKERS = 384
VERIFY_WORKERS = 384
# 校验搜索兜底:这些失效原因的源值得用真实搜索规则复测(首页被 WAF 拦/超时
# ≠ 源不可用,起点 202、69书吧 403 这类首页拦爬虫但搜索接口正常的源很多)。
SEARCH_FALLBACK_REASONS = {"timeout", "http_403", "http_429", "http_503"}
# 批量自动抓取:站点内无鼠标/键盘操作满该秒数 → 自动抓取并跳下一站;
# 用户在页面里点击/输入(CDP 注入监听)会重置倒计时,给登录留时间。
AUTO_IDLE_SECS = 5
AUTO_IDLE_FAST = 1.5    # 已配过 Cookie 的站刷新会话用快档
AUTO_PARA_TABS = 10     # 自动批并行扫的标签数(不需要登录的站并行开)
AUTO_PARA_SETTLE = 8.0  # 并行扫: 页面提交后再等的秒数(Set-Cookie 落地)
# 正文下载是另一回事:同一本书的章节全来自同一个站,并发越高越容易触发限流/封禁,
# 所以刻意压低,不要跟着搜索一起调大。
DOWNLOAD_WORKERS = 10

# 书源校验的请求头统一走 engine.source_headers(书源 header + 用户登录头/Cookie,
# UA 缺省 DEFAULT_UA)—— 旧版独立 VERIFY_UA(Chrome/114 Edg/114)已被 WAF 大量
# 拦截,且连书源自带 header 都不传,是误判失效的元凶之一(2026-09-12 v1.5.5/5.6)。

# 深度校验(v1.8.0):活性探测之后对每个活源追加"试搜 + 分类探测"两步真实
# 业务探测。试搜关键词与搜索兜底救援一致;每源至多 2 个额外请求(试搜 1 +
# 分类 1),并发沿用 VERIFY_WORKERS。
DEEP_KEYWORD = "我的"


def good_table_path(origin: Path) -> Path:
    """原始全量表对应的有效书源表:<原名>.good.json(与 verify_sources.py 输出一致)。"""
    name = origin.name
    if name.lower().endswith(".json"):
        return origin.with_name(name[:-5] + ".good.json")
    return origin.with_name(origin.stem + ".good.json")


def deep_table_path(origin: Path) -> Path:
    """原始全量表对应的深度校验产物:<原名>.deep.json(v1.8.0)。"""
    name = origin.name
    if name.lower().endswith(".json"):
        return origin.with_name(name[:-5] + ".deep.json")
    return origin.with_name(origin.stem + ".deep.json")


def deep_classify(search_err, n_hits, explore_err, n_items):
    """深度校验判定(纯函数,便于单测):
    → (search_verdict, explore_verdict, level)。

    search_verdict: ok(≥1 命中)/ no_result(HTTP 成功但书列表空)/
      error(网络/HTTP 失败)/ no_search(无法试搜,不判死)。
    explore_verdict: ok / no_result / error / none(无分类能力)/
      untested(中断未测)。
    level(质量等级): 试搜 ok → 分类 ok=完整可用,其余=可搜(分类死≠源
      不可用,只作附加字段);试搜空转→搜索空转;试搜失败→试搜失败;
      无法试搜→无法试搜。"""
    if search_err == "no_search":
        sv = "no_search"
    elif search_err:
        sv = "error"
    elif n_hits >= 1:
        sv = "ok"
    else:
        sv = "no_result"
    if explore_err == "none":
        ev = "none"
    elif explore_err is None:                    # 中断未测到分类阶段
        ev = "untested"
    elif explore_err:
        ev = "error"
    elif n_items >= 1:
        ev = "ok"
    else:
        ev = "no_result"
    if sv == "ok":
        level = "完整可用" if ev == "ok" else "可搜"
    elif sv == "no_result":
        level = "搜索空转"
    elif sv == "error":
        level = "试搜失败"
    else:
        level = "无法试搜"
    return sv, ev, level


def write_deep_table(origin: Path, fn, rows, keyword, interrupted=False):
    """写深度校验产物 <原名>.deep.json(原子写,与 error.json 同风格)。

    rows 每项 = {"name","url","level","search":{verdict,hits,ms,reason},
    "explore":{verdict,items,ms,reason}}。与 good 表"停止不写"不同,
    深度结果是分析产物,中断时已测源的结果照常落盘(_meta.interrupted=true)。
    返回 _meta 计数 dict(供日志摘要)。"""
    counts = {"alive": len(rows), "search_ok": 0, "search_no_result": 0,
              "search_error": 0, "no_search": 0,
              "explore_ok": 0, "explore_no_result": 0, "explore_error": 0}
    for r in rows:
        sv = (r.get("search") or {}).get("verdict")
        if sv == "ok":
            counts["search_ok"] += 1
        elif sv == "no_result":
            counts["search_no_result"] += 1
        elif sv == "error":
            counts["search_error"] += 1
        elif sv == "no_search":
            counts["no_search"] += 1
        ev = (r.get("explore") or {}).get("verdict")
        if ev == "ok":
            counts["explore_ok"] += 1
        elif ev == "no_result":
            counts["explore_no_result"] += 1
        elif ev == "error":
            counts["explore_error"] += 1
    meta = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_file": fn, "keyword": keyword,
            "interrupted": bool(interrupted)}
    meta.update(counts)
    table = {"_meta": meta, "results": rows}
    out = deep_table_path(origin)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, out)
    return counts


def _fmt_deep_summary(counts: dict) -> str:
    """深度校验分布 → 一行中文日志(试搜 + 分类)。分类分母 = 实测分类的源数
    (无分类能力的源不计)。"""
    tested = (counts.get("explore_ok", 0) + counts.get("explore_no_result", 0)
              + counts.get("explore_error", 0))
    return ("试搜:可搜 %d · 空转 %d · 失败 %d · 无法试搜 %d;"
            "分类:可用 %d / %d"
            % (counts.get("search_ok", 0), counts.get("search_no_result", 0),
               counts.get("search_error", 0), counts.get("no_search", 0),
               counts.get("explore_ok", 0), tested))


def _fmt_reason_summary(summary: dict) -> str:
    """失效原因分布 → 一行中文日志,按数量降序。"""
    zh = {"connect": "连不上", "timeout": "超时", "invalid_url": "无URL"}
    return " · ".join("%s %d" % (zh.get(k, k), v) for k, v in
                      sorted(summary.items(), key=lambda kv: -kv[1]))


def _pick_login_url(source):
    """选择"浏览器登录抓取"的打开目标:优先源 loginUrl(纯网址),回退站点 URL。

    返回 (open_url, grab_host):open_url 是要在浏览器里打开并登录的页面,
    grab_host 是 Cookie 抓取过滤用的主机名 —— 永远取 bookSourceUrl 的主机,
    这样登录页(passport 等子域)设置的域级 Cookie 也能被匹配进来。
    返回 (None, None) 表示该源没有可用网址。
    """
    if not source:
        return None, None
    base = (source.get("bookSourceUrl") or "").strip()
    if "://" not in base:
        base = "https://" + base
    grab_host = _url_host(base)
    if not grab_host:
        return None, None
    lu = (source.get("loginUrl") or "").strip()
    if lu.startswith(("http://", "https://")) and not re.search(
            r"<js>|@js:", lu, re.I):
        return lu, grab_host
    return base, grab_host


def _url_host(url):
    """取 URL 主机名(小写);解析失败返回空串。"""
    try:
        from urllib.parse import urlsplit
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _merge_cookie_str(old, new):
    """按键合并两段 Cookie 头;new 里的同名键覆盖 old,old 独有的键保留。

    浏览器被强杀时内存 Cookie 可能没落盘 —— 直接整体覆盖会让新抓取
    反而丢掉旧键, 所以抓取结果一律按名合并。
    """
    d = {}
    for part in (old or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    for part in (new or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return "; ".join("%s=%s" % (k, v) for k, v in d.items() if k)


def auth_state_path() -> Path:
    """每源登录头存储:shuyuan/auth_state.json。

    属用户凭据,**不属于缓存** —— 「清除缓存」不清理,只能在新加的
    「登录头」管理窗口里查看/修改/清空。写入与 good 表同款原子写。
    """
    return SOURCE_DIR / "auth_state.json"


def load_auth_state() -> dict:
    """读登录头表 {bookSourceUrl: {"cookie": str, "header": dict}};损坏按空表。"""
    try:
        data = json.loads(auth_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_auth_state(auth: dict) -> None:
    p = auth_state_path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


VERIFY_ARTIFACT_SUFFIXES = (".good.json", ".error.json", ".deep.json")   # 校验产物后缀 = 可再生缓存


def is_verify_artifact(name) -> bool:
    """校验产物判定:*.good.json / *.error.json / *.deep.json。

    它们是「校验书源」跑出来的缓存(有效表/失效表/深度校验结果),可由原始
    表重新生成,既不能当书源输入(否则"产物再校验"形成滚雪球),也不属于
    用户配置。
    """
    return str(name).lower().endswith(VERIFY_ARTIFACT_SUFFIXES)


def scan_verify_artifacts():
    """shuyuan/ 下现存的全部校验产物(含无主的孤儿产物),按文件名排序。"""
    try:
        if SOURCE_DIR.exists():
            return [p for p in sorted(SOURCE_DIR.iterdir())
                    if p.is_file() and is_verify_artifact(p.name)]
    except Exception:
        pass
    return []


class DownloadDialog:
    """下载方式选择弹窗:单一(自动跳过被封书源) / 合并(每本都下) + 导出格式单选。"""

    def __init__(self, parent, n, default_fmt="epub", default_mode="single"):
        self.result = None
        top = tk.Toplevel(parent)
        top.title("下载选项")
        top.transient(parent)
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top = top

        frm = ttk.Frame(top, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="已选中 %d 本书,请选择下载方式:" % n,
                  font=("Microsoft YaHei UI", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.mode = tk.StringVar(value=default_mode)
        ttk.Radiobutton(frm, text="下载单一",
                        variable=self.mode, value="single").grid(row=1, column=0, sticky="nw")
        ttk.Label(frm, text="按顺序探测选中的书源,自动跳过被封/需登录的,\n"
                           "只用第一本能成功下载的源,其余丢弃。",
                  foreground="#555", justify="left").grid(row=1, column=1, sticky="w")

        ttk.Radiobutton(frm, text="合并下载",
                        variable=self.mode, value="batch").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        ttk.Label(frm, text="选中的每一本都下载,各自导出为独立文件。\n"
                            "被封的源会跳过并在日志中标红。",
                  foreground="#555", justify="left").grid(row=2, column=1, sticky="w", pady=(8, 0))

        ttk.Separator(frm, orient="horizontal").grid(row=3, column=0, columnspan=2,
                                                     sticky="ew", pady=12)

        ttk.Label(frm, text="导出格式(二选一):").grid(row=4, column=0, columnspan=2, sticky="w")
        self.fmt = tk.StringVar(value=default_fmt)
        ttk.Radiobutton(frm, text="EPUB", variable=self.fmt, value="epub").grid(
            row=5, column=0, sticky="w", padx=(12, 0))
        ttk.Radiobutton(frm, text="TXT", variable=self.fmt, value="txt").grid(
            row=5, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=self._cancel, width=8).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="开始下载", command=self._ok, width=10).pack(side="right")

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        top.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        top.grab_set()
        top.focus_force()
        parent.wait_window(top)

    def _ok(self):
        self.result = (self.mode.get(), self.fmt.get())
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


class App:
    def __init__(self, root):
        self.root = root
        root.title("小说下载器 · Legado书源  →  TXT / EPUB")
        root.geometry("1040x720")
        root.minsize(900, 620)

        self.q = queue.Queue()
        self.hits = []
        self._gtag = {}                # 同书分组 → 底色交替序号(见 _insert_hit_row)
        self.sources = []              # 工作书源(搜索/下载用)= 勾选文件合并后的源列表
        self.checked_files = []        # 勾选的书源文件名(相对 shuyuan/,保序,重启不丢)
        self.verify_dones = {}         # {文件名: {"origin": 路径, "time": 完成时刻}}
                                       # ——每文件一条;只有本程序校验跑完生成的 good 表才采用
        self.verify_origin = ""        # 旧单文件字段:仅供一次性迁移读取,不再新增语义
        self.verify_done = {}
        self.stop_search = threading.Event()
        self.stop_dl = threading.Event()
        self.stop_verify = threading.Event()
        self.busy_search = False
        self.busy_dl = False
        self.busy_verify = False
        self._last_key = ""
        # —— 每源登录头(用户凭据):读盘并注入引擎,校验/搜索/详情/目录/正文全链路生效 ——
        self._auth = load_auth_state()
        engine.set_auth(self._auth)
        # —— 运行记忆:先读盘,勾选/取消/选项都持久,重启原样恢复(见 _mem_*)——
        mem = self._mem_load()
        self.verify_origin = mem.get("verify_origin") or ""
        self.verify_done = mem.get("verify_done") or {}
        # 勾选文件清单:记忆里有就用(含空 = 用户全不选);无字段(旧记忆/全新)→ 迁移或默认
        checked = mem.get("sources")
        self.checked_files = list(checked) if checked is not None else \
            ([Path(self.verify_origin).name] if self.verify_origin
             else [DEFAULT_SOURCE.name])
        self.verify_dones = dict(mem.get("verify_dones") or {})
        self._mem_last = mem       # 记忆缓存(含 selected key 列表)

        # 顶部勾选框 + 下载弹窗选项:默认值 = 上次记忆(取消过的保持取消);
        # 挂 trace 后任一勾选变化都立即写盘,不会再"回到默认勾上"。
        self.var_fuzzy = tk.BooleanVar(value=bool(mem.get("fuzzy", True)))
        self.var_rel = tk.BooleanVar(value=bool(mem.get("rel", True)))
        self.var_fmt = tk.StringVar(value=mem.get("fmt") or "epub")      # epub / txt
        self.var_mode = tk.StringVar(value=mem.get("mode") or "single")  # single / batch
        self.var_domain = tk.StringVar(value=mem.get("domain") or "自动")  # 搜索域
        self.var_deep_only = tk.BooleanVar(value=bool(mem.get("deep_only", False)))
        for _v in (self.var_fuzzy, self.var_rel, self.var_fmt, self.var_mode,
                   self.var_domain, self.var_deep_only):
            _v.trace_add("write", self._mem_save_opts)
        # 深度校验判定表(引擎模块级,搜索预筛过滤用):键=(书源名,bookSourceUrl)
        self.deep_table = {}

        # —— 多选交互状态(资源管理器式,常开;见 MultiSelect 相关方法)——
        self._drag = None          # 进行中的橡皮筋状态 dict 或 None
        self._press = None         # 左键按下信息
        self._last_click = None    # 上次单击信息,用于双击判定
        self._panel = None         # 书源文件下拉面板(见 _src_panel_*)

        self._build_ui()
        self._anchor = None        # Shift 连续选锚点行(资源管理器语义)
        self.root.after(120, self._drain)
        # —— JS 引擎可用性提示(v1.7.0):决定含 JS 书源是否解锁 ——
        if jsengine.HAS_JS:
            self.log("JS 引擎:已启用(mini-racer/V8,1484 个含 JS 书源解锁)。")
        else:
            self.log("JS 引擎:未安装 mini-racer,含 JS 书源仍跳过(pip install mini-racer)。")
        self._reload_all()
        self._restore_pending = bool(mem.get("selected"))   # 搜索结果到达后尝试恢复

    # ------------------------------------------------------------- UI -------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)

        # 书源文件 = 已加入清单(见 _src_panel_*):点开下拉面板增删文件
        # 触发器样式与「书源分组/搜索域」统一:只读 Combobox + 下箭头;
        # 点击/箭头/方向键一律拦截内置下拉(return "break"),改弹自绘清单面板
        self.var_src_txt = tk.StringVar(value="书源文件(0)")
        self.cmb_src = ttk.Combobox(top, textvariable=self.var_src_txt, width=18,
                                    state="readonly", values=("书源文件",))
        self.cmb_src.pack(side="left", **pad)
        for _seq in ("<Button-1>", "<Down>", "<F4>", "<Alt-Down>"):
            self.cmb_src.bind(_seq, self._src_trigger_press, add="+")
        self.cmb_src.bind("<Return>", lambda e: self._src_trigger_press(e))
        ttk.Button(top, text="打开目录", command=self._open_src_dir).pack(side="left")
        self.btn_verify = ttk.Button(top, text="✔ 校验书源", command=self.start_verify)
        self.btn_verify.pack(side="left")
        # 深度校验(活性 → 试搜 + 分类探测,deep.json 质量分级,v1.8.0)
        self.btn_deep = ttk.Button(top, text="🔬 深度校验", command=self.start_deep_verify)
        self.btn_deep.pack(side="left", padx=(6, 0))
        ttk.Button(top, text="登录头", command=self.open_auth_manager,
                   width=7).pack(side="left", padx=(6, 0))
        self.lbl_verify = ttk.Label(top, text="", foreground="#555")
        self.lbl_verify.pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(self.root)
        row2.pack(fill="x", padx=8, pady=2)
        ttk.Label(row2, text="书源分组:").pack(side="left")
        self.var_group = tk.StringVar(value="全部")
        self.cmb_group = ttk.Combobox(row2, textvariable=self.var_group, width=26, state="readonly")
        self.cmb_group.pack(side="left", **pad)
        ttk.Label(row2, text="搜索:").pack(side="left", padx=(14, 2))
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(row2, textvariable=self.var_key, width=22)
        self.ent_key.pack(side="left", **pad)
        self.ent_key.bind("<Return>", lambda ev: self.start_search())
        self.cmb_domain = ttk.Combobox(row2, textvariable=self.var_domain, width=5,
                                       state="readonly",
                                       values=["自动", "书名", "作者", "分类"])
        self.cmb_domain.pack(side="left")
        self.btn_search = ttk.Button(row2, text="🔍 搜索", command=self.start_search)
        self.btn_search.pack(side="left")
        cb = ttk.Checkbutton(row2, text="模糊搜索", variable=self.var_fuzzy)
        cb.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(row2, text="只看相关结果", variable=self.var_rel).pack(side="left", padx=(8, 0))
        # 深度校验联动:开启后跳过 deep.json 判定"空转/试搜失败"的源(无记录不过滤)
        ttk.Checkbutton(row2, text="只搜试搜通过源",
                        variable=self.var_deep_only).pack(side="left", padx=(8, 0))
        self.btn_stop = ttk.Button(row2, text="停止", command=self.stop_all, state="disabled")
        self.btn_stop.pack(side="left", **pad)
        self.lbl_progress = ttk.Label(row2, text="", foreground="#555")
        self.lbl_progress.pack(side="left", padx=10)

        # 结果表:普通 tree + 滚动条(tree 持鼠标捕获,橡皮筋选框为拖动时临时 Toplevel)
        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("name", "author", "kind", "last", "src")
        # 选择全部由 self._apply_selection 维护,selectmode 恒 extended
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        heads = {"name": ("书名", 300), "author": ("作者", 110), "kind": ("分类", 90),
                 "last": ("最新章节", 160), "src": ("书源", 160)}
        for c, (t, w) in heads.items():
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda ev: self._sync_sel_label())  # 键盘增选时同步计数
        self.tree.tag_configure("blocked", foreground="#b00020")   # 被封/失败的源标红
        # 同书分组底色:相邻组交替,一眼看出哪些行是同一本书
        self.tree.tag_configure("grp1", background="#eef4fb")
        self.tree.tag_configure("grp2", background="#ffffff")
        # 移除 Treeview 类级绑定(其内置"单击替换选择/拖拽行选"会与自定义交互冲突),
        # 滚轮与方向键等必要行为由下方自行接管。
        try:
            self.tree.bindtags((str(self.tree), ".", "all"))
        except Exception:
            pass
        self.tree.bind("<MouseWheel>", self._ms_wheel)
        self.tree.bind("<Button-4>", lambda e: self._ms_wheel_scroll(e, 1))
        self.tree.bind("<Button-5>", lambda e: self._ms_wheel_scroll(e, -1))
        self.tree.bind("<KeyRelease>", self._on_key_nav)
        self.tree.bind("<ButtonPress-1>", self._ms_press)
        self.tree.bind("<B1-Motion>", self._ms_motion)
        self.tree.bind("<ButtonRelease-1>", self._ms_release)
        self.tree.bind("<Button-3>", self._ms_right)
        self.root.bind("<Escape>", lambda e: self._ms_escape())
        self.idx2iid = {}                                          # hits 下标 -> 行 id
        self._rubber_top = None                                    # 橡皮筋半透明层(临时)
        self.lbl_hits = ttk.Label(self.root, text="未搜索", foreground="#888")
        self.lbl_hits.pack(anchor="w", padx=10)

        # 选择辅助(多选模式下全部可用;单选模式下 全选/反选 置灰)
        selbar = ttk.Frame(self.root)
        selbar.pack(fill="x", padx=8, pady=(0, 2))
        self.btn_sel_all = ttk.Button(selbar, text="全选", command=self.sel_all, width=7)
        self.btn_sel_all.pack(side="left")
        self.btn_sel_inv = ttk.Button(selbar, text="反选", command=self.sel_invert, width=7)
        self.btn_sel_inv.pack(side="left", padx=4)
        self.btn_sel_none = ttk.Button(selbar, text="清空选择", command=self.sel_none, width=9)
        self.btn_sel_none.pack(side="left")
        ttk.Button(selbar, text="清除缓存", command=self._cache_clear, width=9).pack(side="left", padx=4)
        self.lbl_sel = ttk.Label(selbar, text="已选 0 本", foreground="#0066cc")
        self.lbl_sel.pack(side="left", padx=12)
        self.lbl_sel_hint = ttk.Label(
            selbar,
            text="单击=单选 · Ctrl+单击=增减 · Shift+单击=连续选 · 按住拖动=框选(Shift=追加) · Esc 取消",
            foreground="#888")
        self.lbl_sel_hint.pack(side="left")

        # 下载区
        dl = ttk.Frame(self.root)
        dl.pack(fill="x", padx=8, pady=4)
        self.btn_dl = ttk.Button(dl, text="⬇ 下载选中", command=self.start_download)
        self.btn_dl.pack(side="left")
        ttk.Label(dl, text="保存到:").pack(side="left", padx=(16, 2))
        self.var_out = tk.StringVar(value=str(DEFAULT_OUT))
        ttk.Entry(dl, textvariable=self.var_out, width=34).pack(side="left")
        ttk.Button(dl, text="浏览…", command=self.pick_out).pack(side="left", **pad)
        ttk.Button(dl, text="打开目录", command=self.open_out).pack(side="left", **pad)

        self.pbar = ttk.Progressbar(self.root, mode="determinate")
        self.pbar.pack(fill="x", padx=8, pady=2)
        self.lbl_dl = ttk.Label(self.root, text="", foreground="#444")
        self.lbl_dl.pack(anchor="w", padx=10)
        self.lbl_tick = ttk.Label(self.root, text="", foreground="#888")
        self.lbl_tick.pack(anchor="w", padx=10)

        logf = ttk.LabelFrame(self.root, text="运行日志")
        logf.pack(fill="both", expand=False, padx=8, pady=(2, 6))
        self.txt_log = tk.Text(logf, height=7, state="disabled", font=("Microsoft YaHei UI", 9))
        sb = ttk.Scrollbar(logf, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ------------------------------------------------------------ 动作 -------
    def log(self, s):
        self.q.put(("log", s))

    # ------------------------------------------- 书源文件下拉面板(清单式) ----
    # 语义(2026-09-09 拍板):顶部「书源文件」Combobox 触发器点开面板,面板只列
    # "已加入清单"(checked_files,参与搜索/下载);行尾 ✕ = 仅移出清单(不删
    # 磁盘文件);底部「+ 新加入书源…」= 直接弹系统文件对话框多选(不在
    # shuyuan/ 的自动复制进去)。清单 = 记忆 sources,重启原样恢复。
    def _src_trigger_press(self, e):
        """Combobox 触发器:不弹内置下拉,改开/关自绘清单面板。"""
        self._src_panel_toggle()
        return "break"

    def _src_files_missing(self, fn):
        return not (SOURCE_DIR / fn).exists()

    def _update_src_text(self):
        n = len(self.checked_files)
        try:
            self.var_src_txt.set("书源文件(%d)" % n)
        except Exception:
            pass

    def _src_panel_toggle(self):
        if getattr(self, "_panel", None) and self._panel.winfo_exists():
            self._src_panel_close()
        else:
            self._src_panel_open()

    def _src_panel_open(self):
        self._src_panel_close()
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
        except Exception:
            pass
        wrap = tk.Frame(top, bd=1, relief="solid", bg="#f0f0f0")
        wrap.pack(fill="both", expand=True)
        frm = ttk.Frame(wrap, padding=6)
        frm.pack(fill="both", expand=True)

        n = len(self.checked_files)
        ttk.Label(frm, text="书源文件 · %d" % n,
                  font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", pady=(0, 3))

        rows = ttk.Frame(frm)
        rows.pack(fill="both", expand=True)
        if not self.checked_files:
            ttk.Label(rows, text="(空 — 点下方「+ 新加入书源…」)",
                      foreground="#888").pack(anchor="w", pady=4)
        for fn in list(self.checked_files):
            row = ttk.Frame(rows)
            row.pack(fill="x", pady=1)
            missing = self._src_files_missing(fn)
            ttk.Label(row, text=("⚠ %s" % fn) if missing else fn,
                      foreground="#b00020" if missing else "").pack(side="left")
            ttk.Button(row, text="✕", width=2,
                       command=lambda f=fn: self._remove_source_file(f)).pack(side="right")

        ttk.Separator(frm, orient="horizontal").pack(fill="x", pady=4)
        ttk.Button(frm, text="+ 新加入书源…",
                   command=self._add_sources_browse).pack(fill="x")

        top.update_idletasks()
        x = self.cmb_src.winfo_rootx()
        y = self.cmb_src.winfo_rooty() + self.cmb_src.winfo_height()
        w = max(self.cmb_src.winfo_width(), 300)
        top.geometry("%dx%d+%d+%d" % (w, top.winfo_reqheight(), x, y))
        top.focus_set()
        top.bind("<Escape>", lambda e: self._src_panel_close())
        self._panel = top
        # 点击面板外任意处关闭(触发器自身除外——由 _src_trigger_press toggle 收尾)
        self._outside_id = self.root.bind_all(
            "<Button-1>", self._src_panel_outside, add="+")

    def _src_panel_outside(self, e):
        p = getattr(self, "_panel", None)
        if not p or not p.winfo_exists():
            return
        try:
            w = str(e.widget)
        except Exception:
            return
        if w.startswith(str(p) + "."):               # 面板内部
            return
        if w in (str(self.cmb_src),):                # 触发器自身:由 toggle 收尾
            return
        self._src_panel_close()

    def _src_panel_close(self):
        try:
            self.root.unbind_all("<Button-1>", self._outside_id)
        except Exception:
            pass
        p = getattr(self, "_panel", None)
        self._panel = None
        if p and p.winfo_exists():
            try:
                p.destroy()
            except Exception:
                pass

    def _src_panel_refresh(self):
        """面板开着就重建(行增删后刷新);关着则只更新按钮文本。"""
        if getattr(self, "_panel", None) and self._panel.winfo_exists():
            self._src_panel_open()
        else:
            self._update_src_text()

    def _cleanup_verify_artifacts(self, fn, origin):
        """删除指定书源文件的校验产物(.good.json / .error.json / .deep.json)
        并清除验证记录。"""
        for suffix in VERIFY_ARTIFACT_SUFFIXES:
            p = SOURCE_DIR / fn if isinstance(origin, str) else origin
            target = p.with_name(p.stem + suffix)
            if target.exists():
                try:
                    target.unlink()
                    self.log("已清理: %s" % target.name)
                except Exception as e:
                    self.log("⚠ 清理 %s 失败: %s" % (target.name, e))
        self.verify_dones.pop(fn, None)

    def _remove_source_file(self, fn):
        """行尾 ✕:仅移出清单(磁盘文件保留,可经「新加入」再加回)。"""
        try:
            self.checked_files.remove(fn)
        except ValueError:
            return
        self._cleanup_verify_artifacts(fn, SOURCE_DIR / fn)
        self.log("已移出书源清单(文件保留在 shuyuan/): %s" % fn)
        self._reload_all()
        self._src_panel_refresh()

    # -------------------------------------------- 新加入书源(直接文件对话框) --
    # v1.5.1:去掉二级勾选窗口(transient+grab 模态窗最小化后无法找回 = 死锁
    # 观感),点「+ 新加入书源…」= 关面板 → 直接弹 Windows 文件对话框多选
    # (定位 shuyuan/;不在 shuyuan/ 的自动复制进去) → 加入清单并合并重载。
    def _add_sources_browse(self):
        self._src_panel_close()
        try:
            SOURCE_DIR.mkdir(parents=True, exist_ok=True)   # 全新安装时目录尚不存在
        except Exception as e:
            messagebox.showerror("目录错误", str(e))
            return
        ps = filedialog.askopenfilenames(
            title="新加入书源(可多选;不在 shuyuan/ 的会自动复制进去;"
                  "校验产物 *.good/.error/.deep.json 会被忽略)",
            filetypes=[("JSON 书源", "*.json"), ("所有文件", "*.*")],
            initialdir=str(SOURCE_DIR))
        if not ps:
            return
        added, copied, skipped = [], [], []
        for p in ps:
            src = Path(p)
            if is_verify_artifact(src.name):    # 校验产物=缓存,不能当书源输入
                skipped.append(src.name)
                continue
            dest = SOURCE_DIR / src.name
            if src.resolve() != dest.resolve():
                try:
                    import shutil
                    shutil.copy2(str(src), str(dest))
                    copied.append(src.name)
                except Exception as e:
                    messagebox.showerror("复制失败", "%s\n%s" % (src, e))
                    continue
            if src.name not in self.checked_files:
                self.checked_files.append(src.name)
                added.append(src.name)
        if skipped:
            self.log("⚠ 已忽略校验产物(校验生成的缓存,非书源): %s"
                     % "、".join(skipped))
        if not added:
            if skipped and not copied:
                messagebox.showinfo(
                    "已忽略",
                    "所选的都是校验产物(*.good.json / *.error.json / *.deep.json),\n"
                    "它们是「校验书源」生成的缓存,不能作为书源加入。\n"
                    "若想清掉它们,请点「清除缓存」。")
            else:
                self.log("所选文件已在清单中,无新增。")
            return
        prefix = ("已复制进 shuyuan/: %s → " % "、".join(copied)) if copied else ""
        self.log("%s已加入清单: %s" % (prefix, "、".join(added)))
        self._reload_all()

    def _open_src_dir(self):
        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(SOURCE_DIR))
        except Exception:
            pass

    @staticmethod
    def _fmt_time(t):
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(float(t)))
        except Exception:
            return "?"

    def _set_groups(self):
        groups = ["全部"] + [g for g, _ in engine.source_groups(self.sources)]
        self.cmb_group["values"] = groups
        if self.var_group.get() not in groups:
            self.var_group.set("全部")

    def _reload_all(self):
        """按勾选文件清单(shuyuan/ 下)逐文件加载并合并成工作源 self.sources。

        good 信任逐文件独立判定:verify_dones[文件名].origin 匹配该文件当前路径
        且其 .good.json 存在 → 搜索/下载用 good 表;否则该文件用全量表
        (外部/旧表不自动采用)。缺失/读取失败的文件保留勾选,跳过并日志提示。
        """
        # 自愈:清单里若混入校验产物(*.good.json/*.error.json)一律剔除,并清掉它们
        # 的孤立校验记录 —— 产物是缓存,当书源输入会形成"产物→再校验→再产物"滚雪球。
        bad = [fn for fn in self.checked_files if is_verify_artifact(fn)]
        if bad:
            for fn in bad:
                try:
                    self.checked_files.remove(fn)
                except ValueError:
                    pass
                self.verify_dones.pop(fn, None)
            self.log("⚠ 已从清单剔除校验产物(校验生成的缓存,非书源): %s"
                     % "、".join(bad))
        merged, n_good = [], 0
        for fn in self.checked_files:
            p = SOURCE_DIR / fn
            if not p.exists():
                self._cleanup_verify_artifacts(fn, p)
                self.log("⚠ 书源文件缺失,已跳过(勾选保留): %s" % fn)
                continue
            try:
                srcs = engine.load_sources(str(p))
            except Exception as e:
                self.log("✘ 书源文件读取失败,已跳过: %s(%s)" % (fn, e))
                continue
            done = self.verify_dones.get(fn) or {}
            good = good_table_path(p)
            use = srcs
            use_good = False
            if done.get("origin") == str(p) and good.exists():
                try:
                    use = engine.load_sources(str(good))
                    n_good += 1
                    use_good = True
                    self.log("已加载 %s:有效表 %s(%d 源 · 校验于 %s)"
                             % (fn, good.name, len(use),
                                self._fmt_time(done.get("time"))))
                except Exception:
                    use = srcs
                    self.log("有效表 %s 读取失败,%s 暂用全量 %d 源。"
                             % (good.name, fn, len(srcs)))
            else:
                hint = ("发现旧有效表 %s(非本程序校验生成),未采用" % good.name) \
                    if good.exists() else "尚无有效表,可点\"校验书源\"生成"
                self.log("已加载 %s:全量 %d 源(%s)" % (fn, len(srcs), hint))
            for s in use:
                s["_file"] = fn                     # 运行时归属标记,不写回书源 JSON
                s["_from_good"] = use_good          # 供跨文件去重"good 优先"
            merged.extend(use)
        # 清理孤立的 verify_dones 记录(已不在清单中的文件)
        stale = [k for k in self.verify_dones if k not in self.checked_files]
        for k in stale:
            self._cleanup_verify_artifacts(k, SOURCE_DIR / k)
        # 🆕 跨文件合并重复书源:按 (书源名, 站点URL) 去重,good 表来源优先
        self.sources, n_dup = dedupe_sources(merged)
        if self.checked_files and self.sources:
            self.log("合并工作源 %d 个(勾选 %d 文件 · %d 个用有效表 · 去重 %d 个重复源)。"
                     % (len(self.sources), len(self.checked_files), n_good, n_dup))
            if len(self.sources) > 5000:
                self.log("⚠ 合并源数较大(%d),建议先\"校验书源\"再搜索。"
                         % len(self.sources))
        elif self.checked_files:
            self.log("⚠ 勾选的 %d 个文件都没有可用书源。" % len(self.checked_files))
        else:
            self.log("未加入任何书源文件;点\"书源文件\"下拉 →「+ 新加入书源…」。")
        self._set_groups()
        self._load_deep_tables()   # 深度判定预灌(各文件 .deep.json,搜索过滤用)
        self.lbl_verify.config(text="勾选 %d 文件 · 合并 %d 源(去重 %d)"
                               % (len(self.checked_files),
                                  len(self.sources), n_dup))
        self._update_src_text()
        self._mem_save_core()

    # --------------------------------------------------------- 书源校验 -----
    # 多文件按序校验:每个勾选的原始表独立 VERIFY_WORKERS 并发探测 → 各自写
    # .good.json(有效表)与 .error.json(机器可读的失效记录,含原因分类);
    # 停止 = 当前文件中止 + 后续不再开始(已完成的产物保留)。
    # 判定:并发 GET bookSourceUrl,status < 400 即有效(202/301 等也算活;
    # 失败重试一次再定生死。旧版"200 独裁 + 5s 超时 + 老 UA"曾把约六成活源误判失效。
    def _check_one(self, s):
        """探测单源,返回 (reason, status, elapsed_ms)。

        reason ∈ ok / timeout / connect(连不上、DNS 死)/ http_<code> / invalid_url;
        status 为最后一次 HTTP 状态码(非 HTTP 失败为 None);elapsed_ms 含重试的
        总耗时(回写 respondTime 供搜索排序);中止返回 None。
        """
        if self.stop_verify.is_set():
            return None                                          # None = 中止未检测
        url = (s.get("bookSourceUrl") or "").strip()
        if not url:
            return ("invalid_url", None, 0)
        t0 = time.time()
        reason, status = ("connect", None)
        for _attempt in (0, 1):                                  # 失败重试一次
            if self.stop_verify.is_set():
                return None
            try:
                hd = engine.source_headers(s)                    # 含用户登录头/Cookie
                r = fetcher.request("GET", url, headers=hd, timeout=12,
                                    verify=False, allow_redirects=True)
                if r.status_code < 400:
                    return ("ok", r.status_code, int((time.time() - t0) * 1000))
                reason, status = ("http_%d" % r.status_code, r.status_code)
            except fetcher.TIMEOUT_EXCS:
                reason, status = ("timeout", None)
            except Exception:
                reason, status = ("connect", None)
        return (reason, status, int((time.time() - t0) * 1000))

    @staticmethod
    def _write_error_table(origin, fn, srcs, results, rescued=0):
        """写机器可读失效记录 <原名>.error.json,返回原因分布 dict。

        面向 agent/脚本解析:_meta.reason_summary 一眼拿到失效原因分布,
        failures 逐源记录 name/url/reason/status。后缀已在 VERIFY_ARTIFACT_SUFFIXES
        中,「清除缓存」与重校验前的清理会把它当可再生缓存带走。
        """
        fails, summary = [], {}
        n_untested = 0
        for s, r in zip(srcs, results):
            if r is None:                                        # 中止未检测,不计入
                n_untested += 1
                continue
            if r[0] == "ok":
                continue
            reason, status = r[0], r[1]
            summary[reason] = summary.get(reason, 0) + 1
            fails.append({"source_name": (s.get("bookSourceName") or "").strip(),
                          "url": (s.get("bookSourceUrl") or "").strip(),
                          "reason": reason, "status": status,
                          "elapsed_ms": r[2] if len(r) > 2 else None})
        table = {"_meta": {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "source_file": fn, "total": len(srcs),
                           "ok": len(srcs) - len(fails) - n_untested,
                           "failed": len(fails), "untested": n_untested,
                           "rescued_by_search": rescued,
                           "reason_summary": summary},
                 "failures": fails}
        out = origin.with_name(origin.stem + ".error.json")
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, out)
        return summary

    def _collect_verify_files(self):
        """收集勾选且存在的原始表(校验/深度校验共用)。"""
        files = []
        for fn in self.checked_files:
            p = SOURCE_DIR / fn
            if p.exists():
                files.append((fn, p))
            else:
                self.log("⚠ 校验跳过缺失文件: %s" % fn)
        return files

    def _verify_ui_lock(self):
        """校验/深度校验共用的状态锁定。"""
        self.stop_verify.clear()
        self.busy_verify = True
        self.btn_verify.config(state="disabled")
        self.btn_deep.config(state="disabled")
        self.btn_search.config(state="disabled")
        self.btn_dl.config(state="disabled")
        self.btn_stop.config(state="normal")

    def start_verify(self):
        if self.busy_verify or self.busy_search or self.busy_dl:
            return
        files = self._collect_verify_files()
        if not files:
            messagebox.showwarning("提示", "没有可校验的书源文件。"
                                           "请先在\"书源文件\"下拉中勾选。")
            return
        self._verify_ui_lock()
        n_files = len(files)
        self.log("开始校验 %d 个书源文件(并发 %d · 超时 12s · status<400 · 重试 1 次)…"
                 % (n_files, VERIFY_WORKERS))
        self.lbl_verify.config(text="校验中 · 文件 0/%d" % n_files)
        threading.Thread(target=self._do_verify, args=(files,), daemon=True).start()

    def start_deep_verify(self):
        """深度校验入口:活性校验(复用 _do_verify 全套逻辑)→ 对活源试搜 +
        分类探测 → 写 <原名>.deep.json 质量分级。"""
        if self.busy_verify or self.busy_search or self.busy_dl:
            return
        files = self._collect_verify_files()
        if not files:
            messagebox.showwarning("提示", "没有可校验的书源文件。"
                                           "请先在\"书源文件\"下拉中勾选。")
            return
        self._verify_ui_lock()
        n_files = len(files)
        self.log("开始深度校验 %d 个书源文件:活性探测 → 试搜「%s」+ 分类探测"
                 "(并发 %d · 每源至多 2 个额外请求)…"
                 % (n_files, DEEP_KEYWORD, VERIFY_WORKERS))
        self.lbl_verify.config(text="深度校验 · 文件 0/%d" % n_files)
        threading.Thread(target=self._do_verify, args=(files, True),
                         daemon=True).start()

    def _do_verify(self, files, deep=False):
        """逐文件校验循环。files = [(filename, Path), ...]

        deep=True(v1.8.0 深度校验):活性校验照旧跑完全套(good/error 表
        语义不变),之后对每个活性通过的源追加试搜 + 分类探测,写
        <原名>.deep.json。全部失效的文件不进入深度阶段。
        """
        t_total = time.time()
        n_files = len(files)
        tot_ok, tot_bad = 0, 0
        aborted = False
        for fi, (fn, origin) in enumerate(files):
            if self.stop_verify.is_set():
                aborted = True
                break
            self.q.put(("vfile", (fi + 1, n_files, fn)))
            try:
                srcs = engine.load_sources(str(origin))
            except Exception as e:
                self.q.put(("log", "✘ 文件 %s 读取失败,跳过: %s" % (fn, e)))
                continue
            if not srcs:
                self.q.put(("log", "⚠ 文件 %s 无书源,跳过。" % fn))
                continue
            t0 = time.time()
            results, done = [], 0
            pool = ThreadPoolExecutor(max_workers=VERIFY_WORKERS)
            try:
                for r in pool.map(self._check_one, srcs):
                    results.append(r)
                    done += 1
                    if done % 25 == 0 or done == len(srcs):
                        n_ok = sum(1 for x in results if x and x[0] == "ok")
                        n_bad = sum(1 for x in results if x and x[0] != "ok")
                        self.q.put(("vprog", (fi + 1, n_files, fn,
                                              done, len(srcs), n_ok, n_bad)))
            except Exception as e:
                self.q.put(("log", "校验异常(%s): %s" % (fn, e)))
            finally:
                pool.shutdown(wait=False)
            n_bad = sum(1 for x in results if x and x[0] != "ok")
            n_ok = sum(1 for x in results if x and x[0] == "ok")
            n_untested = sum(1 for x in results if x is None)
            elapsed = time.time() - t0
            tot_ok += n_ok
            tot_bad += n_bad
            table = good_table_path(origin)
            if n_untested:
                self.q.put(("log", "文件 %s 校验中止(未检测 %d 个)" % (fn, n_untested)))
                aborted = True
                break
            # 搜索兜底救援:403/超时类源用真实搜索规则复测,搜到书即改判有效。
            n_rescued = 0
            if not self.stop_verify.is_set():
                cands = [s for s, r in zip(srcs, results)
                         if r and r[0] in SEARCH_FALLBACK_REASONS]
                if cands:
                    self.q.put(("log", "搜索兜底: 对 %d 个 403/超时类源用真实搜索规则复测…"
                                % len(cands)))
                    rescued = set()

                    def _on_hit(h, _rescued=rescued):
                        _rescued.add(((h.get("source") or {}).get("bookSourceUrl")
                                      or "").strip())

                    try:
                        engine.search_sources(cands, DEEP_KEYWORD, workers=VERIFY_WORKERS,
                                              stop=self.stop_verify, on_hit=_on_hit)
                    except Exception as e:
                        self.q.put(("log", "搜索兜底异常: %s" % e))
                    for i, (s, r) in enumerate(zip(srcs, results)):
                        if (r and r[0] in SEARCH_FALLBACK_REASONS
                                and (s.get("bookSourceUrl") or "").strip() in rescued):
                            results[i] = ("ok", None)
                            n_rescued += 1
                    if n_rescued:
                        n_ok = sum(1 for x in results if x and x[0] == "ok")
                        n_bad = sum(1 for x in results if x and x[0] != "ok")
                        self.q.put(("log", "搜索兜底救回 %d 个(文件 %s)"
                                    % (n_rescued, fn)))
            if n_ok == 0:
                # 断网保护:全部失效时清掉旧产物,避免 offline 全灭被缓存成"没有可用源"。
                self._cleanup_verify_artifacts(fn, origin)
                self.q.put(("log", "⚠ 文件 %s 全部 %d 个源失效(耗时 %.0fs),已清理旧有效表。"
                                    % (fn, n_bad, elapsed)))
            # 机器可读的失效记录(含原因分布)落盘,供 agent/脚本读取分析;
            # 全失效分支上面刚清过旧产物,这里写的是本次的新记录。
            try:
                summary = self._write_error_table(origin, fn, srcs, results,
                                                  rescued=n_rescued)
                if summary:
                    self.q.put(("log", "失效原因(%s): %s"
                                % (fn, _fmt_reason_summary(summary))))
            except Exception as e:
                self.q.put(("log", "✘ %s 失效记录写入失败: %s" % (fn, e)))
            if n_ok == 0:
                continue
            seen, good = set(), []
            for s, r in zip(srcs, results):
                if r and r[0] == "ok":
                    u = (s.get("bookSourceUrl") or "").strip()
                    if u not in seen:
                        seen.add(u)
                        g = dict(s)
                        if len(r) > 2 and r[2]:              # 响应耗时回写(Legado 同名字段)
                            g["respondTime"] = r[2]
                        good.append(g)
            try:
                tmp = table.with_name(table.name + ".tmp")
                tmp.write_text(json.dumps(good, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                os.replace(tmp, table)
            except Exception as e:
                self.q.put(("log", "✘ %s 有效表写入失败: %s" % (fn, e)))
                continue
            self.q.put(("vfile_done", (fn, str(origin), n_ok, n_bad, elapsed)))
            # 深度校验阶段:活源试搜 + 分类探测 → deep.json(全失效文件不进入)
            if deep and n_ok > 0:
                if self.stop_verify.is_set():
                    aborted = True
                    break
                try:
                    self._deep_stage(fi, n_files, fn, origin, srcs, results)
                except Exception as e:
                    self.q.put(("log", "✘ %s 深度校验异常: %s" % (fn, e)))
                if self.stop_verify.is_set():      # 深度阶段中途停止:后续文件不再开始
                    aborted = True
                    break
        elapsed_total = time.time() - t_total
        self.q.put(("vdone", (n_files, tot_ok, tot_bad, elapsed_total, aborted)))

    # ------------------------------------------------------ 深度校验阶段 -----
    # 活性通过的源逐个试搜(关键词 DEEP_KEYWORD,走 engine.search_one 单源路径)
    # + 分类探测(engine.explore_first_page),每源至多 2 个额外请求;并发沿用
    # VERIFY_WORKERS。中途停止:已测源结果照常写 deep.json(_meta.interrupted),
    # 与 good 表"停止不写"不同 —— 深度结果是分析产物,部分数据也有价值。
    def _deep_search_one(self, s):
        """试搜单源 → (reason, hits, ms);中止返回 None。reason 空串=网络存活。"""
        if self.stop_verify.is_set():
            return None
        t0 = time.time()
        try:
            hits, err = engine.search_one(s, DEEP_KEYWORD)
        except Exception as e:                     # 引擎层不该抛,兜底归因
            hits, err = [], "connect"
            self.q.put(("log", "⚠ 试搜异常(%s): %s"
                        % (s.get("bookSourceName", "?"), e)))
        return (err, len(hits), int((time.time() - t0) * 1000))

    def _deep_explore_one(self, s):
        """分类探测单源 → (reason, items, ms);中止返回 None。"""
        if self.stop_verify.is_set():
            return None
        t0 = time.time()
        try:
            items, err = engine.explore_first_page(s)
        except Exception as e:
            items, err = 0, "connect"
            self.q.put(("log", "⚠ 分类探测异常(%s): %s"
                        % (s.get("bookSourceName", "?"), e)))
        return (err, items, int((time.time() - t0) * 1000))

    def _deep_stage(self, fi, n_files, fn, origin, srcs, results):
        """对活性通过的源跑深度阶段并写 <原名>.deep.json + 日志分布摘要。"""
        alive = [s for s, r in zip(srcs, results) if r and r[0] == "ok"]
        if not alive:
            return
        pool = ThreadPoolExecutor(max_workers=VERIFY_WORKERS)

        def _run_phase(fn_one, phase, out):
            """并发跑一个探测阶段:as_completed 收割并节流上报进度(vdeep)。"""
            total = len(alive)
            futs = {pool.submit(fn_one, s): i for i, s in enumerate(alive)}
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    out[i] = fut.result()
                except Exception:
                    out[i] = None                  # 与中止同样按未测处理
                done += 1
                if done % 25 == 0 or done == total:
                    self.q.put(("vdeep", (fi + 1, n_files, fn, phase,
                                          done, total)))
            return out

        try:
            self.q.put(("vdeep", (fi + 1, n_files, fn, "试搜", 0, len(alive))))
            sres = _run_phase(self._deep_search_one, "试搜", [None] * len(alive))
            stopped = any(r is None for r in sres)
            eres = [None] * len(alive)
            if not stopped and not self.stop_verify.is_set():
                self.q.put(("vdeep", (fi + 1, n_files, fn, "分类", 0, len(alive))))
                eres = _run_phase(self._deep_explore_one, "分类", eres)
            else:
                stopped = True
        finally:
            pool.shutdown(wait=False)
        rows = []
        for s, sr, er in zip(alive, sres, eres):
            if sr is None:                         # 中止未检测,不进结果
                continue
            if er is None:                         # 分类阶段未测到(搜索阶段已中止)
                raw_ev, eitems, ems = None, 0, None
            else:
                raw_ev, eitems, ems = er
            sv, ev, level = deep_classify(sr[0], sr[1], raw_ev, eitems)
            rows.append({
                "name": (s.get("bookSourceName") or "").strip(),
                "url": (s.get("bookSourceUrl") or "").strip(),
                "level": level,
                "search": {"verdict": sv, "hits": sr[1], "ms": sr[2],
                           "reason": sr[0] or None},
                "explore": {"verdict": ev, "items": eitems, "ms": ems,
                            "reason": (raw_ev or None) if raw_ev is not None
                            else "untested"},
            })
        interrupted = stopped or self.stop_verify.is_set()
        counts = write_deep_table(origin, fn, rows, DEEP_KEYWORD,
                                  interrupted=interrupted)
        self.q.put(("vdeep", (fi + 1, n_files, fn, "完成", len(rows), len(alive))))
        self.q.put(("log", "深度结果(%s): %s%s"
                    % (fn, _fmt_deep_summary(counts),
                       " · 已中止,部分源未测" if interrupted else "")))
        # 深度判定灌入引擎(搜索过滤联动):全部文件合并后统一 set_deep
        self._load_deep_tables()

    def _load_deep_tables(self):
        """扫描清单内各文件的 .deep.json,合并灌入 engine.set_deep(搜索过滤用)。

        键 = (书源名, bookSourceUrl),与跨文件去重身份(§5.7)同口径;
        多文件合并去重后同一身份只留一条判定,重复源覆盖次序 = 清单顺序。
        """
        table = {}
        for fn in self.checked_files:
            dp = deep_table_path(SOURCE_DIR / fn)
            try:
                data = json.loads(dp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for row in (data.get("results") or []):
                k = ((row.get("name") or "").strip(),
                     (row.get("url") or "").strip())
                sv = (row.get("search") or {}).get("verdict")
                if k[0] and k[1] and sv:
                    table[k] = sv
        self.deep_table = table
        engine.set_deep(table)
        if table:
            n_skip = sum(1 for v in table.values() if v in ("no_result", "error"))
            self.log("深度校验记录已加载: %d 源(试搜未通过 %d —— 可勾选"
                     "「只搜试搜通过源」在搜索时过滤)。"
                     % (len(table), n_skip))


    def pick_out(self):
        p = filedialog.askdirectory(title="选择保存目录", initialdir=str(DEFAULT_OUT))
        if p:
            self.var_out.set(p)

    def open_out(self):
        d = self.var_out.get()
        Path(d).mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(d)  # noqa
        except Exception:
            pass

    def _chosen_sources(self):
        g = self.var_group.get()
        if g == "全部":
            return self.sources
        return [s for s in self.sources if (s.get("bookSourceGroup") or "(未分组)").strip() == g]

    def start_search(self):
        key = self.var_key.get().strip()
        if not key or self.busy_search or self.busy_verify:
            return
        if not self.sources:
            messagebox.showwarning("提示", "请先加载书源文件")
            return
        self.stop_search.clear()
        self._set_busy(True)
        self.hits = []
        self._gtag = {}              # 同书分组 → 底色交替序号(见 _insert_hit_row)
        for it in self.tree.get_children():
            self.tree.delete(it)
        self.idx2iid = {}
        self._drag = None            # 清掉可能的半途框选状态
        self._press = None
        self._last_click = None
        self._sync_sel_label()
        self._last_key = key
        self.lbl_hits.config(text="搜索中…")
        srcs = self._chosen_sources()
        # 深度校验联动:「只搜试搜通过源」—— 跳过 deep.json 判定"搜索空转/
        # 试搜失败"的源;未做深度校验的源(无记录)不跳过。
        if self.var_deep_only.get():
            skipped = [s for s in srcs if engine.deep_search_ok(s) is False]
            if skipped:
                srcs = [s for s in srcs if engine.deep_search_ok(s) is not False]
                self.log("深度过滤:跳过 %d 个试搜未通过源" % len(skipped))
            elif not self.deep_table:
                self.log("深度过滤已开启,但尚无深度校验记录(点「🔬 深度校验」生成),"
                         "本次未过滤。")
        fuzzy = self.var_fuzzy.get()
        domain = self.var_domain.get()
        domain_hint = " · 域[%s]" % domain if domain != "自动" else ""
        self.log("开始搜索《%s》,分组[%s],候选 %d 源,%s%s" %
                 (key, self.var_group.get(), len(srcs),
                  "模糊开启" if fuzzy else "精确模式", domain_hint))
        if domain in ("作者", "分类"):
            self.log("💡 网络层按书名发起搜索,%s为返回结果内的本地过滤。" % domain)
        threading.Thread(target=self._do_search, args=(key, srcs, fuzzy), daemon=True).start()

    def _do_search(self, key, srcs, fuzzy):
        def prog(done, total, msg):
            self.q.put(("sprog", "%d/%d 源 · %s" % (done, total, msg)))

        def onhit(h):
            self.q.put(("hit", h))

        try:
            hits = engine.search_sources(srcs, key, on_progress=prog, stop=self.stop_search,
                                         workers=SEARCH_WORKERS, on_hit=onhit, fuzzy=fuzzy)
        except Exception as e:
            self.q.put(("log", "搜索异常: %s" % e))
            hits = []
        self.q.put(("sres", (len(hits), fuzzy)))

    def _rel_terms(self):
        """相关性判定词:关键词 + 其模糊变体(变体重试产生的结果也算相关)。"""
        k = (self._last_key or self.var_key.get() or "").strip()
        if not k:
            return []
        terms = [k] + engine.make_key_variants(k)
        return [t.lower() for t in terms if len(t) >= 2]

    def _relevant(self, h):
        """只看相关结果:按当前域(自动/书名/作者/分类)判定是否保留。

        自动:书名/作者/分类/简介至少一处命中(现有行为)。
        书名/作者/分类:仅该域命中关键词或其变体时保留。
        很多小站无视搜索词、返回热门书充数,本地不过滤就会混进无关结果。
        """
        terms = self._rel_terms()
        if not terms:
            return True
        domain = self.var_domain.get()
        if domain == "书名":
            name = (h.get("name") or "").lower()
            return any(t in name for t in terms)
        elif domain == "作者":
            author = (h.get("author") or "").lower()
            return any(t in author for t in terms)
        elif domain == "分类":
            kind = (h.get("kind") or "").lower()
            return any(t in kind for t in terms)
        # 自动:四域任一命中
        text = " ".join((h.get("name") or "", h.get("author") or "",
                         h.get("kind") or "", h.get("intro") or "")).lower()
        return any(t in text for t in terms)

    def _score_hit(self, h):
        """相关度:书名同名/含词 > 作者含词 > 分类含词 > 简介含词 > 字符相似度。"""
        import difflib
        k = (self._last_key or "").strip()
        if not k:
            return 0
        kl = k.lower()
        name = (h.get("name") or "").lower()
        s = 0
        if kl == name:
            s += 100
        elif kl in name:
            s += 80
        else:
            for v in self._rel_terms():          # 变体命中书名也给分
                if v != kl and v in name:
                    s += 60
                    break
        if kl in (h.get("author") or "").lower():
            s += 40
        if kl in (h.get("kind") or "").lower():
            s += 25
        if kl in (h.get("intro") or "").lower():
            s += 15
        if s == 0:
            s += difflib.SequenceMatcher(None, kl, name).ratio() * 30
        return s

    def stop_all(self):
        self.stop_search.set()
        self.stop_dl.set()
        self.stop_verify.set()

    def _set_busy(self, b):
        self.busy_search = b
        self.btn_search.config(state="disabled" if b else "normal")
        self.btn_stop.config(state="normal" if b or self.busy_dl else "disabled")
        self.btn_dl.config(state="disabled" if self.busy_dl else "normal")

    # --------------------------------------------- 统一选中提交/回调 ---------
    def _hit_key(self, h):
        """条目的稳定 ID:(书源名, 详情URL)。用于记忆恢复与去重。"""
        return (h["source"].get("bookSourceName", "?"), h["book_url"])

    # --------------------------------------------- 同书分组展示/排序 ---------
    def _row_name(self, h):
        """书名列文本:组首行且同书命中多源时加"【N源】"前缀。"""
        if h.get("_group_first") and h.get("_src_count", 1) > 1:
            return "【%d源】%s" % (h["_src_count"], h["name"])
        return h["name"]

    def _insert_hit_row(self):
        """把 self.hits 末行插入表格(增量上屏与整体重建共用):
        同组行交替底色,组首行加【N源】前缀;tags[0] 恒为 hits 下标。"""
        i = len(self.hits) - 1
        h = self.hits[i]
        k = h.get("_group_key") or ("?", self._hit_key(h))
        seq = self._gtag.setdefault(k, len(self._gtag))
        tag = "grp1" if seq % 2 == 0 else "grp2"
        iid = self.tree.insert("", "end",
                               values=(self._row_name(h), h.get("author", ""),
                                       h.get("kind", ""),
                                       h.get("last_chapter", ""),
                                       h["source"]["bookSourceName"]),
                               tags=(str(i), tag))
        self.idx2iid[i] = iid

    @staticmethod
    def _completeness(h):
        """字段完整度:作者齐(+2)>分类/最新章节(+1)。组内排序用,
        完整度高的源排前,默认下载选中的质量更高。"""
        return ((2 if h.get("author") else 0) + (1 if h.get("kind") else 0)
                + (1 if h.get("last_chapter") else 0))

    def _ordered_hits(self):
        """重排 self.hits:同书组相邻;组间按组内最高相关度(相关度并列时
        保持先到先排);组内按字段完整度+相关度排前。"""
        groups, order = {}, []
        for h in self.hits:
            k = h.get("_group_key") or ("?", self._hit_key(h))
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(h)
        def _rt(x):
            """源响应耗时(ms, 校验时写入 respondTime);缺省视为最慢。"""
            try:
                return int((x.get("source") or {}).get("respondTime") or 10 ** 9)
            except Exception:
                return 10 ** 9

        for g in groups.values():
            g.sort(key=lambda x: (self._completeness(x), self._score_hit(x),
                                  -_rt(x)), reverse=True)
        order.sort(key=lambda k: max(self._score_hit(x) for x in groups[k]),
                   reverse=True)
        return [x for k in order for x in groups[k]]

    def _current_keys(self):
        """当前选中行的稳定 ID 列表(按行序)。"""
        keys = []
        for iid in self.tree.selection():
            tags = self.tree.item(iid, "tags")
            try:
                idx = int(tags[0]) if tags else None
            except Exception:
                idx = None
            if idx is not None and 0 <= idx < len(self.hits):
                keys.append(self._hit_key(self.hits[idx]))
        return keys

    def _apply_selection(self, iids, notify=True):
        """唯一写选中入口:落 Treeview → 刷新标签 → 按需通知(持久化/外部回调)。

        notify=True 表示"用户操作导致的最终状态",触发统一选中结果回调;
        框选拖动过程与程序性恢复传 notify=False,避免中间态写记忆。
        """
        iids = [i for i in iids if i in self.tree.get_children()]
        self.tree.selection_set(iids)
        self.tree.selection_remove([i for i in self.tree.get_children() if i not in iids])
        self._sync_sel_label()
        if notify:
            self._on_selection_changed(self._current_keys())

    def _on_selection_changed(self, keys):
        """统一选中结果回调(选中状态变化 → 上层)。
        当前只做记忆持久化;未来接入外部消费者在此扩展。
        """
        self._mem_save(keys)

    def _mem_load(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            # JSON 数组读回来是 list,而 _hit_key 产出 tuple;统一成 tuple 才能进集合
            sel = [tuple(k) if isinstance(k, list) else k
                   for k in (d.get("selected") or [])]
            # 勾选文件清单:无 sources 字段(旧记忆)→ 从旧 verify_origin 一次性迁移
            sources = d.get("sources")
            if sources is None:
                sources = ([Path(d["verify_origin"]).name]
                           if d.get("verify_origin") else None)
            dones = d.get("verify_dones")
            if not isinstance(dones, dict):
                dones = {}
            if sources and not dones and isinstance(d.get("verify_done"), dict) \
                    and d["verify_done"].get("origin"):
                dones = {sources[0]: d["verify_done"]}   # 旧单条校验记录 → 按文件挂
            return {"multi": True, "selected": sel,
                    "verify_origin": d.get("verify_origin") or "",
                    "verify_done": d.get("verify_done") or {},
                    "sources": [str(x) for x in sources] if sources else sources,
                    "verify_dones": dones,
                    "fuzzy": bool(d.get("fuzzy", True)),
                    "rel": bool(d.get("rel", True)),
                    "deep_only": bool(d.get("deep_only", False)),
                    "fmt": d.get("fmt") or "epub",
                    "mode": d.get("mode") or "single",
                    "domain": d.get("domain") or "自动"}
        except Exception:
            # 无记忆/文件损坏:空选中 + 出厂默认选项。多选交互常开(单击仍是单选,无害)。
            return {"multi": True, "selected": [], "verify_origin": "",
                    "verify_done": {}, "sources": None, "verify_dones": {},
                    "fuzzy": True, "rel": True, "deep_only": False,
                    "fmt": "epub", "mode": "single", "domain": "自动"}

    def _mem_save(self, keys=None):
        """选中变化后的落盘入口(keys=None 时取当前树选中)。"""
        self._mem_flush(keys)

    def _mem_save_core(self):
        """书源状态(勾选文件/校验记录)变化后的落盘入口;只用缓存选中,
        避免启动早期空树清空记忆。"""
        if self._mem_last:
            keys = list(self._mem_last.get("selected") or [])
        else:
            keys = self._current_keys()
        self._mem_flush(keys)

    def _mem_save_opts(self, *a):
        """选项勾选变化入口(顶部两勾选框 / 下载方式 / 导出格式):即改即存。"""
        if self._mem_last:
            keys = list(self._mem_last.get("selected") or [])
        else:
            keys = self._current_keys()
        self._mem_flush(keys)

    def _mem_flush(self, keys=None):
        """统一全量写盘:选中 + 校验记录 + 选项勾选一次写齐,互不覆盖。

        keys=None 时读当前树选中;非 None(启动早期树未填充等)用给定 keys。
        """
        try:
            if keys is None:
                keys = self._current_keys()
            data = {"multi": True,
                    "selected": [[s, u] for s, u in keys],
                    "sources": list(self.checked_files),
                    "verify_dones": dict(self.verify_dones),
                    # 旧单文件字段:过渡期继续读写(commit 3 校验多文件化后停止写入)
                    "verify_origin": getattr(self, "verify_origin", ""),
                    "verify_done": getattr(self, "verify_done", {}),
                    "fuzzy": bool(self.var_fuzzy.get()),
                    "rel": bool(self.var_rel.get()),
                    "deep_only": bool(self.var_deep_only.get()),
                    "fmt": self.var_fmt.get() or "epub",
                    "mode": self.var_mode.get() or "single",
                    "domain": self.var_domain.get() or "自动"}
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            self._mem_last = {"multi": True,
                              "selected": list(keys),
                              "sources": data["sources"],
                              "verify_dones": data["verify_dones"],
                              "verify_origin": data["verify_origin"],
                              "verify_done": data["verify_done"],
                              "fuzzy": data["fuzzy"],
                              "rel": data["rel"],
                              "deep_only": data["deep_only"],
                              "fmt": data["fmt"],
                              "mode": data["mode"],
                              "domain": data["domain"]}
        except Exception:
            pass

    def _cache_clear(self):
        """清除缓存入口:清掉可再生缓存,保留用户配置。

        清理对象(全部可再生):
          ① shuyuan/ 下全部校验产物(*.good.json / *.error.json / *.deep.json);
          ② 校验记录(verify_dones + 旧字段 verify_origin/verify_done);
          ③ 选项打勾(fuzzy/rel/deep_only/fmt/mode/domain)→ 恢复出厂默认;
          ④ 上次选中的书目(selected)。
        保留:书源勾选清单(checked_files)。它是用户配置而非缓存 ——
        旧「清除记忆」会把它压回默认单文件,导致"可用源突然只剩一个"。
        """
        arts = scan_verify_artifacts()
        total = 0
        for _p in arts:
            try:
                total += _p.stat().st_size
            except Exception:
                pass
        if len(arts) > 4:
            names = "、".join(p.name for p in arts[:4]) + " 等 %d 个" % len(arts)
        else:
            names = "、".join(p.name for p in arts)
        n_sel = len((self._mem_last or {}).get("selected") or [])
        msg = ("将清除以下缓存(均可再生,不影响 shuyuan/ 里的书源文件与已下载的书):\n\n"
               "· 校验产物 %d 个 · %.1f MB\n    %s\n"
               "· 校验记录 %d 条\n"
               "· 选项打勾 → 恢复默认(模糊开 · 相关开 · epub · 单本 · 自动)\n"
               "· 上次选中的书目 %d 本\n\n"
               "保留:书源勾选清单 %d 个文件(不清)\n\n确定清除?"
               % (len(arts), total / 1048576.0, names or "(无)",
                  len(self.verify_dones), n_sel, len(self.checked_files)))
        if not messagebox.askyesno("清除缓存", msg):
            return
        removed = freed = 0
        for _p in arts:
            try:
                sz = _p.stat().st_size
            except Exception:
                sz = 0
            try:
                _p.unlink()
                removed, freed = removed + 1, freed + sz
                self.log("已清除缓存: %s" % _p.name)
            except Exception as e:
                self.log("⚠ 清除缓存失败 %s: %s" % (_p.name, e))
        n_rec = len(self.verify_dones)
        self.verify_dones = {}
        self.verify_origin = ""
        self.verify_done = {}
        # 先把记忆缓存刷成目标态再改控件/重载:_mem_flush 的 selected 取自该缓存,
        # 不先置空的话重载会把旧选中项回填回去。
        self._mem_last = {"multi": True, "selected": [],
                          "sources": list(self.checked_files),
                          "verify_dones": {}, "verify_origin": "", "verify_done": {},
                          "fuzzy": True, "rel": True, "deep_only": False,
                          "fmt": "epub", "mode": "single", "domain": "自动"}
        for _v, _d in ((self.var_fuzzy, True), (self.var_rel, True),
                       (self.var_deep_only, False),
                       (self.var_fmt, "epub"), (self.var_mode, "single"),
                       (self.var_domain, "自动")):
            try:
                _v.set(_d)         # 触发 trace → _mem_save_opts → 落盘全新状态
            except Exception:
                pass
        self.deep_table = {}
        engine.set_deep({})        # deep.json 已随之删除,深度过滤表同步清空
        self._apply_selection([], notify=False)
        self._reload_all()
        messagebox.showinfo(
            "清除缓存",
            "已清除 %d 个校验产物(释放 %.1f MB)、%d 条校验记录;\n"
            "选项已恢复默认,上次选中的书目已清空。\n书源勾选清单保留 %d 个文件。"
            % (removed, freed / 1048576.0, n_rec, len(self.checked_files)))

    # --------------------------------------------------------- 登录头管理 -----
    # 每源 Cookie/自定义 header(shuyuan/auth_state.json,用户凭据,清除缓存不清)。
    # 保存即注入 engine:校验/搜索/详情/目录/正文全链路生效 —— 浏览器登录后把
    # Cookie 粘进来,即可救活需登录/反爬拦截的书源。
    def open_auth_manager(self):
        win = tk.Toplevel(self.root)
        win.title("登录头管理 · 每源 Cookie / 自定义 header")
        win.geometry("880x620")
        win.transient(self.root)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="过滤(名称/URL):").pack(side="left")
        var_filter = tk.StringVar()
        ttk.Entry(top, textvariable=var_filter).pack(side="left", padx=4,
                                                     fill="x", expand=True)
        # 选择交互与主窗口搜索结果表一致(selkit.TreeMultiSelect):
        # 单击=单选 · Ctrl=增减 · Shift=连续 · 拖动=框选 · Esc 取消;全选供批量操作
        ttk.Button(top, text="全选", width=6,
                   command=lambda: self._auth_kit.select_all()).pack(side="left")
        ttk.Button(top, text="清空", width=6,
                   command=lambda: self._auth_kit.clear_selection()).pack(side="left", padx=4)
        lbl_cnt = ttk.Label(top, text="已选 0", foreground="#0066cc")
        lbl_cnt.pack(side="left")

        mid = ttk.Frame(win)
        mid.pack(fill="both", expand=True, padx=8)
        tbl = ttk.Treeview(mid, columns=("name", "url"), show="headings", height=12)
        tbl.heading("name", text="书源(● = 已配置登录头)")
        tbl.heading("url", text="站点 URL")
        tbl.column("name", width=280, anchor="w")
        tbl.column("url", width=520, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=tbl.yview)
        tbl.configure(yscrollcommand=sb.set)
        tbl.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        det = ttk.LabelFrame(win, text="登录头(先选中源,再填/抓)")
        det.pack(fill="x", padx=8, pady=6)
        lbl_cur = ttk.Label(det, foreground="#888")
        lbl_cur.config(text="未选中 · 在上方点选书源(支持 Ctrl/Shift/拖动框选多选)")
        lbl_cur.pack(anchor="w", padx=6, pady=(4, 0))
        row_ck = ttk.Frame(det)
        row_ck.pack(fill="x", padx=6)
        ttk.Label(row_ck, text="Cookie:").pack(side="left")
        btn_fetch = ttk.Button(row_ck, text="浏览器登录抓取",
                               command=lambda: None, width=14)
        btn_fetch.pack(side="right")
        lbl_stat = ttk.Label(det, text="", foreground="#888")
        lbl_stat.pack(anchor="w", padx=6)
        txt_ck = tk.Text(det, height=4, wrap="char")
        txt_ck.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(det, text="自定义 header JSON(可空,如 {\"Referer\": \"https://xx.com/\"}):"
                  ).pack(anchor="w", padx=6)
        var_hd = tk.StringVar()
        ttk.Entry(det, textvariable=var_hd).pack(fill="x", padx=6, pady=(0, 4))

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        cur_url = [""]

        # —— 浏览器登录抓取(cdp_cookie):弹默认浏览器 → 登录 → CDP 抓 Cookie ——
        auth_sess = {"handle": None, "phase": "idle", "pending": None}
        poll_job = [None]

        def _stat(msg, color="#888"):
            lbl_stat.config(text=msg, foreground=color)

        def poll_fetch():
            """工作线程只写 auth_sess["pending"],UI 侧轮询取结果(ttk 非线程安全)。"""
            st = auth_sess["pending"]
            if st:
                auth_sess["pending"] = None
                if st["state"] == "para_done":
                    if not batch["active"]:
                        pass                       # 结束批量后迟到的结果, 丢弃
                    elif st["n_serial"]:
                        batch["stage"] = "serial"
                        btn_skip.pack(side="left", padx=4)
                        btn_bpause.config(text="暂停自动")
                        btn_bpause.pack(side="left", padx=4)
                        self.log("并行扫完成: 秒过 %d 站 + 并行抓到 %d 站;"
                                 "%d 站没抓到 Cookie, 进入逐站登录模式"
                                 % (st["n_instant"], st["n_para"], st["n_serial"]))
                        batch_next()
                    else:
                        batch_finish("全自动完成: 秒过 %d 站 + 并行抓到 %d 站,"
                                     "无需逐站登录。" % (st["n_instant"], st["n_para"]))
                elif st["state"] == "launched":
                    auth_sess["phase"] = "launched"
                    batch["last_act"] = time.time()      # 新站就绪: 倒计时从此起算
                    batch["launched_t"] = time.time()
                    batch["url_ok"] = False              # 页面到达目标站后才允许抓
                    warn = st.get("warn") or ""
                    if batch["active"]:
                        btn_fetch.config(text="抓取本站(%d/%d)"
                                         % (batch["idx"] + 1, len(batch["hosts"])),
                                         state="normal")
                        base = ("批量 %d/%d: %s —— 在浏览器里登录(已登录可忽略),"
                                "点「抓取本站」;不用配 Cookie 点「跳过该站」。"
                                % (batch["idx"] + 1, len(batch["hosts"]),
                                   batch["hosts"][batch["idx"]]))
                        _stat(base + (" ⚠ " + warn if warn else ""),
                              "#cc0000" if warn else "#888")
                    else:
                        btn_fetch.config(text="我登录好了 → 抓取", state="normal")
                        if warn:
                            _stat(warn + " 仍可尝试抓取,或换一个源。", "#cc0000")
                        else:
                            _stat("浏览器已启动,请在浏览器里登录;完成后点本按钮抓取 Cookie。")
                elif st["state"] == "captured":
                    txt_ck.delete("1.0", "end")
                    txt_ck.insert("1.0", st["cookie"])
                    if batch["active"]:
                        host = batch["hosts"][batch["idx"]]
                        n = 0
                        if st["cookie"]:
                            for u in batch["targets"][host]:
                                cfg = dict(self._auth.get(u) or {})
                                cfg["cookie"] = _merge_cookie_str(
                                    cfg.get("cookie"), st["cookie"])
                                self._auth[u] = cfg
                                n += 1
                            save_auth_state(self._auth)
                            engine.set_auth(self._auth)
                            self.log("批量: 已保存 %s 的 Cookie(%d 个源)" % (host, n))
                        else:
                            self.log("批量: %s 未抓到 Cookie(可能打不开/不是目标站),已跳过"
                                     % host)
                        batch_next()
                    else:
                        cdp_cookie.close(auth_sess["handle"])
                        auth_sess["handle"] = None
                        auth_sess["phase"] = "idle"
                        if st["cookie"]:
                            btn_fetch.config(text="浏览器登录抓取", state="normal")
                            _stat("已抓取 %d 条 Cookie 并填入 → 上方选中目标源(可多选)→ 点「保存到所选」" %
                                  len([p for p in st["cookie"].split("; ") if p]),
                                  "#0066cc")
                        else:
                            btn_fetch.config(text="重试抓取", state="normal")
                            _stat("没抓到该站点的 Cookie —— 浏览器打开的可能不是目标站点"
                                  "(网址无效会落到 Edge 主页),或该站没种 Cookie。"
                                  "可点「重试抓取」或手动粘贴。", "#cc0000")
                else:
                    msg = st["msg"]
                    if "10061" in msg or "积极拒绝" in msg:
                        msg += " —— 抓取浏览器已关闭或未就绪;重新点「浏览器登录抓取」即可。"
                    if batch["active"]:
                        batch_finish("批量抓取中止: %s" % msg, "#cc0000")
                    else:
                        if auth_sess["handle"]:
                            cdp_cookie.close(auth_sess["handle"])
                        auth_sess["handle"] = None
                        auth_sess["phase"] = "idle"
                        btn_fetch.config(text="浏览器登录抓取", state="normal")
                        _stat(msg, "#cc0000")
            if batch["active"] and batch.get("stage") == "para":
                now = time.time()
                if now - batch["stat_t"] >= 1.0:
                    batch["stat_t"] = now
                    _stat("并行抓取 %d/%d 站(%d 线程, 完成后进入下一阶段)…"
                          % (batch["para_done"], batch["para_total"],
                             AUTO_PARA_TABS))
            _auto_tick_checked()
            poll_job[0] = win.after(150, poll_fetch)

        def on_fetch():
            if auth_sess["phase"] in ("launching", "capturing"):
                return
            # 打开目标优先选源的登录页(纯网址 loginUrl)—— 登录页常在 passport
            # 等子域;Cookie 抓取过滤仍按站点主机, 域级 Cookie 能匹配进来
            src = next((s for s in self.sources
                        if (s.get("bookSourceUrl") or "").strip() == cur_url[0]),
                       None) if cur_url[0] else None
            open_url, grab_host = _pick_login_url(src)
            if not open_url:
                open_url = cur_url[0] or None
            if not open_url:
                open_url = simpledialog.askstring(
                    "浏览器登录抓取",
                    "要打开并登录的网址(抓取它的 Cookie):",
                    initialvalue="https://", parent=win)
                if not open_url or not open_url.strip():
                    return
                open_url = open_url.strip()
            if auth_sess["phase"] == "idle":
                auth_sess["phase"] = "launching"
                auth_sess["url"] = open_url
                auth_sess["grab_host"] = grab_host or _url_host(open_url)
                btn_fetch.config(state="disabled")
                _stat("正在启动浏览器…")

                def _launch(url=open_url):
                    try:
                        h = cdp_cookie.launch_for_auth(
                            url, APP_DIR / "auth_profile")
                        auth_sess["handle"] = h
                        auth_sess["pending"] = {
                            "state": "launched",
                            "warn": cdp_cookie.page_mismatch(h, url)}
                    except Exception as e:
                        auth_sess["pending"] = {"state": "error", "msg": str(e)}

                threading.Thread(target=_launch, daemon=True).start()
            else:                                     # launched → 抓取
                auth_sess["phase"] = "capturing"
                btn_fetch.config(state="disabled")
                _stat("正在从浏览器抓取 Cookie…")
                host = auth_sess.get("grab_host") or _url_host(open_url)
                handle = auth_sess["handle"]

                def _grab():
                    try:
                        auth_sess["pending"] = {
                            "state": "captured",
                            "cookie": cdp_cookie.fetch_cookies(handle, host)}
                    except Exception as e:
                        auth_sess["pending"] = {"state": "error",
                                                "msg": "抓取失败: %s" % e}

                threading.Thread(target=_grab, daemon=True).start()


        def close_win():
            if poll_job[0]:
                win.after_cancel(poll_job[0])
            batch["active"] = False
            if auth_sess["handle"]:
                cdp_cookie.close(auth_sess["handle"])
            win.destroy()

        # —— 批量抓取:选中源(默认全部)按站点去重,逐站访问;每站登录后点
        # 「抓取本站」即保存 Cookie 并自动换下一站(浏览器与登录态跨站保持)。
        # 自动模式(v1.6.4):页面内无操作满 AUTO_IDLE_SECS 秒自动抓取跳站,
        # 用户在页面里点击/输入会重置倒计时(给登录留时间),全程零点击。 ——
        batch = {"active": False, "auto": False, "paused": False, "stage": "idle",
                 "hosts": [], "targets": {}, "idx": -1, "idle": AUTO_IDLE_SECS,
                 "last_act": 0.0, "last_url": "", "tick_t": 0.0, "stat_t": 0.0,
                 "launched_t": 0.0, "url_ok": False, "para_done": 0,
                 "para_total": 0}

        def batch_finish(msg, color="#0066cc"):
            batch["active"] = False
            batch["stage"] = "idle"
            if auth_sess["handle"]:
                cdp_cookie.close(auth_sess["handle"])
            auth_sess["handle"] = None
            auth_sess["phase"] = "idle"
            btn_fetch.config(text="浏览器登录抓取", state="normal")
            btn_skip.pack_forget()
            btn_bstop.pack_forget()
            btn_bpause.pack_forget()
            btn_bstart.pack(side="left", padx=4)
            btn_bauto.pack(side="left")
            refresh_list()
            _stat(msg, color)
            self.log(msg)

        def _open_current():
            """(重新)打开 batch 当前站点的页面,工作线程执行;浏览器死了会自动重开。

            自动模式不做 page_mismatch 阻塞核验(最多等 8s)—— 倒计时 tick
            本来就观察页面 URL,打不开的站静默期满自然跳过;手动模式保留核验警告。
            """
            host = batch["hosts"][batch["idx"]]
            url = batch.get("login_map", {}).get(host)                 or sorted(batch["targets"][host])[0]   # 有登录页先开登录页

            def _open():
                try:
                    if auth_sess["handle"] is None or \
                            not cdp_cookie.is_alive(auth_sess["handle"]):
                        if auth_sess["handle"]:
                            cdp_cookie.close(auth_sess["handle"])
                        auth_sess["handle"] = cdp_cookie.launch_for_auth(
                            url, APP_DIR / "auth_profile")
                    else:
                        cdp_cookie.navigate(auth_sess["handle"], url)
                    warn = "" if batch.get("auto") else \
                        cdp_cookie.page_mismatch(auth_sess["handle"], url)
                    auth_sess["pending"] = {"state": "launched", "warn": warn}
                except Exception as e:
                    auth_sess["pending"] = {"state": "error",
                                            "msg": "打开 %s 失败: %s" % (host, e)}

            threading.Thread(target=_open, daemon=True).start()

        def batch_next():
            batch["idx"] += 1
            if batch["idx"] >= len(batch["hosts"]):
                batch_finish("批量抓取结束: 共处理 %d 个站点,Cookie 已全部注入。"
                             % len(batch["hosts"]))
                return
            host = batch["hosts"][batch["idx"]]
            # 分级静默: 该站已配过 Cookie → 快档(刷新会话), 全新站 → 慢档(留登录时间)
            urls_here = batch["targets"][host]
            configured = any((self._auth.get(u) or {}).get("cookie") for u in urls_here)
            batch["idle"] = AUTO_IDLE_FAST if configured else AUTO_IDLE_SECS
            btn_fetch.config(state="disabled")
            _stat("批量 %d/%d: 正在打开 %s …(静默 %ss)"
                  % (batch["idx"] + 1, len(batch["hosts"]), host, batch["idle"]))
            _open_current()

        def batch_start(auto=False):
            sel = self._auth_kit.get()
            urls = {tbl.item(i, "values")[1] for i in sel}
            if not urls:
                if not messagebox.askyesno(
                        "批量抓取", "没有选中书源。对全部 %d 个源按站点批量抓取?"
                        % len(self.sources), parent=win):
                    return
                urls = {tbl.item(i, "values")[1] for i in tbl.get_children()}
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
            if var_skip_cfg.get():      # 跳过已配 Cookie 的站点(重跑批量秒级)
                targets = {h: us for h, us in targets_all.items()
                           if not any((self._auth.get(u) or {}).get("cookie")
                                      for u in us)}
                skipped = n_all - len(targets)
                if not targets and n_all:
                    # 全被过滤不能是死路: 给"全部重抓"的机会, 保住自动化体验
                    if messagebox.askyesno(
                            "批量抓取",
                            "选中的 %d 个站点全都配过 Cookie。\n"
                            "要忽略跳过、全部重抓一遍吗?" % n_all, parent=win):
                        targets = targets_all
                        skipped = 0
                    else:
                        return
            login_map = {}
            for s in self.sources:
                b = (s.get("bookSourceUrl") or "").strip()
                h = _url_host(b if "://" in b else "https://" + b)
                lu = (s.get("loginUrl") or "").strip()
                if h and h not in login_map and lu.startswith(("http://", "https://"))                         and not re.search(r"<js>|@js:", lu, re.I):
                    login_map[h] = lu
            batch["hosts"] = sorted(targets)
            batch["targets"] = targets
            batch["login_map"] = login_map
            batch["idx"] = -1
            if not batch["hosts"]:
                messagebox.showinfo("批量抓取", "选中的源没有有效网址。", parent=win)
                return
            skip_note = "(已跳过 %d 个配过 Cookie 的站点)" % skipped if skipped else ""
            if auto:
                tip = ("全自动模式:%d 个站点%s —— 浏览器已有 Cookie 的站直接秒过,"
                       "其余 %d 线程并行访问抓取;抓不到 Cookie 的站最后逐站处理"
                       "(可在页面里登录)。确定开始?"
                       % (len(batch["hosts"]), skip_note, AUTO_PARA_TABS))
            else:
                tip = ("将依次访问 %d 个不同站点%s:每站登录后点「抓取本站」, "
                       "Cookie 自动保存并跳下一站(已登录的站点无需重复登录)。"
                       "确定开始?" % (len(batch["hosts"]), skip_note))
            if not messagebox.askyesno("批量抓取", tip, parent=win):
                return
            batch["active"] = True
            batch["auto"] = auto
            batch["paused"] = False
            batch["stage"] = "serial"
            batch["last_act"] = 0.0
            batch["last_url"] = ""
            batch["tick_t"] = 0.0
            btn_bstart.pack_forget()
            btn_bauto.pack_forget()
            btn_bstop.pack(side="left")
            if auto:
                # 三段式: Phase0 秒过 → Phase1 并行扫 → Phase2 逐站登录。
                # 全程在工作线程, 进度由 poll 轮询 batch["para_done"] 显示。
                batch["stage"] = "para"
                batch["para_done"] = 0
                batch["para_total"] = len(batch["hosts"])
                self.log("自动批量: %d 个站点, 浏览器已有 Cookie 的先秒过,"
                         "其余 %d 线程并行访问…" % (len(batch["hosts"]),
                                                  AUTO_PARA_TABS))
                _stat("正在启动浏览器…")

                def _auto_run():
                    try:
                        first = batch["hosts"][0]
                        auth_sess["handle"] = cdp_cookie.launch_for_auth(
                            sorted(batch["targets"][first])[0],
                            APP_DIR / "auth_profile")
                        # —— Phase 0: 浏览器已有 Cookie 的域, 拆库直存, 零访问 ——
                        allc = cdp_cookie.all_cookies(auth_sess["handle"])
                        instant, remaining = {}, []
                        for h in batch["hosts"]:
                            cks = [c for c in allc
                                   if cdp_cookie._domain_matches(
                                       h, c.get("domain") or "")]
                            if cks:
                                instant[h] = "; ".join(
                                    "%s=%s" % (c["name"], c["value"]) for c in cks)
                            else:
                                remaining.append(h)
                        n_ins_sites = 0
                        for h, ck in instant.items():
                            for u in batch["targets"][h]:
                                cfg = dict(self._auth.get(u) or {})
                                cfg["cookie"] = _merge_cookie_str(
                                    cfg.get("cookie"), ck)
                                self._auth[u] = cfg
                                n_ins_sites += 1
                        if instant:
                            save_auth_state(self._auth)
                            engine.set_auth(self._auth)
                        batch["instant"] = len(instant)
                        batch["instant_sites"] = n_ins_sites
                        # —— Phase 1: 其余站点并行扫 ——
                        results = _para_sweep(auth_sess["handle"], remaining) \
                            if remaining else {}
                        got = sum(1 for ck in results.values() if ck)
                        for h, ck in results.items():
                            if ck:
                                for u in batch["targets"][h]:
                                    cfg = dict(self._auth.get(u) or {})
                                    cfg["cookie"] = _merge_cookie_str(
                                        cfg.get("cookie"), ck)
                                    self._auth[u] = cfg
                        if got:
                            save_auth_state(self._auth)
                            engine.set_auth(self._auth)
                        # —— Phase 2: 没抓到的进入逐站登录模式 ——
                        serial = [h for h in remaining if not results.get(h)]
                        batch["hosts"] = serial
                        batch["idx"] = -1
                        auth_sess["pending"] = {
                            "state": "para_done",
                            "n_instant": len(instant),
                            "n_instant_sites": n_ins_sites,
                            "n_para": got, "n_serial": len(serial)}
                    except Exception as e:
                        auth_sess["pending"] = {"state": "error", "msg": str(e)}

                threading.Thread(target=_auto_run, daemon=True).start()
                return
            btn_skip.pack(side="left", padx=4)
            self.log("批量抓取开始(%s): %d 个站点(来源 %d 个书源)"
                     % ("自动" if auto else "手动", len(batch["hosts"]), len(urls)))
            batch_next()

        def _para_sweep(handle, hosts):
            """Phase 1 并行扫:AUTO_PARA_TABS 个标签同时开, 每站加载+settle 后
            按域取 Cookie 并关标签。返回 {host: cookie 字符串}(空串=没抓到)。"""
            results, lock = {}, threading.Lock()

            def one(h):
                url = sorted(batch["targets"][h])[0]
                ck = ""
                try:
                    tid = cdp_cookie.open_tab(handle, url)
                except Exception:
                    pass
                else:
                    t0 = time.time()
                    committed = False
                    while time.time() - t0 < 8 and batch["active"]:
                        ph = _url_host(cdp_cookie.tab_url(handle, tid))
                        if ph and (ph == h or ph.endswith("." + h)
                                   or h.endswith("." + ph)):
                            committed = True
                            break
                        time.sleep(0.3)
                    time.sleep(AUTO_PARA_SETTLE if committed else 0.5)
                    try:
                        allc = cdp_cookie.all_cookies(handle)
                        ck = "; ".join("%s=%s" % (c["name"], c["value"])
                                       for c in allc
                                       if cdp_cookie._domain_matches(
                                           h, c.get("domain") or ""))
                    except Exception:
                        ck = ""
                    cdp_cookie.close_tab(handle, tid)
                with lock:
                    results[h] = ck
                    batch["para_done"] += 1

            with ThreadPoolExecutor(max_workers=AUTO_PARA_TABS) as ex:
                list(ex.map(one, hosts))
            return results

        def toggle_pause():
            batch["paused"] = not batch["paused"]
            btn_bpause.config(text="继续自动" if batch["paused"] else "暂停自动")
            if batch["paused"]:
                _stat("已暂停自动跳转:点「抓取本站」手动抓,或再点「继续自动」。")

        def batch_grab():
            host = batch["hosts"][batch["idx"]]
            auth_sess["phase"] = "capturing"
            btn_fetch.config(state="disabled")
            _stat("正在抓取 %s 的 Cookie…" % host)
            handle = auth_sess["handle"]

            def _grab():
                try:
                    if handle is None or not cdp_cookie.is_alive(handle):
                        # 浏览器被手动关闭等 → 不中止批量, 重新打开当前站点
                        auth_sess["handle"] = None
                        self.log("批量: 检测到抓取浏览器已关闭,正在重新打开 %s" % host)
                        _open_current()
                        return
                    auth_sess["pending"] = {
                        "state": "captured",
                        "cookie": cdp_cookie.fetch_cookies(handle, host)}
                except Exception as e:
                    auth_sess["pending"] = {"state": "error",
                                            "msg": "抓取失败: %s" % e}

            threading.Thread(target=_grab, daemon=True).start()

        def _auto_tick_checked():
            """兜底包装:_auto_tick 任何异常都不能杀死 poll 循环 —— 否则
            pending 无人处理, 整个抓取流程界面假死, 用户看到的就是"自动化没了"。"""
            try:
                _auto_tick()
            except Exception as e:
                try:
                    _stat("自动模式异常(已忽略): %s" % e, "#cc0000")
                except Exception:
                    pass

        def _auto_tick():
            """自动模式:页面无操作满 AUTO_IDLE_SECS 秒 → 抓取本站并跳下一站。"""
            if not (batch["active"] and batch["auto"] and not batch["paused"]
                    and batch.get("stage") == "serial"):
                return
            if auth_sess["phase"] != "launched":
                return
            now = time.time()
            if now - batch["tick_t"] < 0.5:          # 节流 2Hz
                return
            batch["tick_t"] = now
            handle = auth_sess["handle"]
            pages = cdp_cookie.page_urls(handle)
            url = next((p for p in pages if p.startswith("http")), "")
            if url and url != batch["last_url"]:
                batch["last_url"] = url
                batch["last_act"] = now              # 页面跳转(含登录提交)= 活动
            if not batch.get("url_ok"):
                host = batch["hosts"][batch["idx"]]
                for p in pages:
                    ph = _url_host(p)
                    if ph and (ph == host or ph.endswith("." + host)
                               or host.endswith("." + ph)):
                        batch["url_ok"] = True
                        break
            try:                                     # 活动检测失败不阻断推进
                act_ms = cdp_cookie.page_activity(handle)
            except Exception:
                act_ms = 0
            if act_ms and act_ms / 1000.0 > batch["last_act"]:
                batch["last_act"] = act_ms / 1000.0  # 页面内点击/输入 = 活动
            left = batch["idle"] - (now - batch["last_act"])
            # 页面必须真的到达目标主机才抓(导航提交后 Set-Cookie 才落地);
            # 死站/打不开的站等硬时限(max(10s, 3×静默))后放行跳过
            hard = now - batch.get("launched_t", now) >= max(10.0, batch["idle"] * 3)
            if left <= 0 and (batch.get("url_ok") or hard):
                batch_grab()                         # 与手动抓取同一保存/跳转路径
            elif now - batch["stat_t"] >= 1.0:
                batch["stat_t"] = now
                _stat("自动 %d/%d: %s —— %ds 后无操作自动跳下一站(%ss 档,"
                      "在页面里点击/输入会重置倒计时)"
                      % (batch["idx"] + 1, len(batch["hosts"]),
                         batch["hosts"][batch["idx"]], int(left) + 1,
                         batch["idle"]))

        def batch_skip():
            if batch.get("stage") == "para":
                _stat("并行扫进行中, 不支持单站跳过;可点「结束批量」。", "#cc0000")
                return
            batch_next()

        def batch_stop():
            batch_finish("批量抓取已结束(已完成站点的 Cookie 保留)。")

        def on_fetch_click():
            if batch["active"]:
                if auth_sess["phase"] == "launched":
                    batch_grab()
                return
            on_fetch()

        def refresh_list():
            kw = var_filter.get().strip().lower()
            keep = {tbl.item(i, "values")[1] for i in tbl.selection()}  # 刷新后保住选中
            tbl.delete(*tbl.get_children())
            new_sel = []
            for s in self.sources:
                nm = (s.get("bookSourceName") or "").strip()
                u = (s.get("bookSourceUrl") or "").strip()
                if kw and kw not in nm.lower() and kw not in u.lower():
                    continue
                mark = "● " if u in self._auth else ""
                iid = tbl.insert("", "end", values=(mark + nm, u))
                if u in keep:
                    new_sel.append(iid)
            if new_sel:
                tbl.selection_set(new_sel)

        def on_sel_change(sel):
            """kit 回调:单选回填该源配置;多选进批量模式(清空输入待填);空选复位。"""
            lbl_cnt.config(text="已选 %d" % len(sel))
            if len(sel) == 1:
                name, u = tbl.item(list(sel)[0], "values")
                cur_url[0] = u
                cfg = self._auth.get(u) or {}
                txt_ck.delete("1.0", "end")
                txt_ck.insert("1.0", cfg.get("cookie") or "")
                hd = cfg.get("header")
                var_hd.set(json.dumps(hd, ensure_ascii=False)
                           if isinstance(hd, dict) and hd else "")
                lbl_cur.config(text="%s · %s" % (name, u), foreground="#222")
            elif sel:
                cur_url[0] = ""
                txt_ck.delete("1.0", "end")
                var_hd.set("")
                lbl_cur.config(text="已选 %d 个源(批量应用:填好后点「保存到所选」)"
                               % len(sel), foreground="#0066cc")
            else:
                cur_url[0] = ""
                lbl_cur.config(text="未选中 · 在上方点选书源(可批量)",
                               foreground="#888")

        def _parse_header(raw):
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

        def save_sel():
            sel = sorted(self._auth_kit.get())
            if not sel:
                messagebox.showinfo(
                    "登录头",
                    "先在上方列表选中源(可框选/Ctrl 多选),再点「保存到所选」。",
                    parent=win)
                return
            cookie = txt_ck.get("1.0", "end").strip()
            header = {}
            raw = var_hd.get().strip()
            if raw:
                header = _parse_header(raw)
                if header is None:
                    messagebox.showerror(
                        "登录头", "header 不是合法的 {\"键\": \"值\"} JSON,未保存。",
                        parent=win)
                    return
            urls = {tbl.item(i, "values")[1] for i in sel}
            if cookie or header:
                item = {}
                if cookie:
                    item["cookie"] = cookie
                if header:
                    item["header"] = header
                for u in urls:
                    self._auth[u] = dict(item)
            else:
                for u in urls:                   # 两项都空 = 移除所选源的配置
                    self._auth.pop(u, None)
            save_auth_state(self._auth)
            engine.set_auth(self._auth)
            refresh_list()
            self.log("登录头已保存: %d 个源" % len(urls))

        def del_sel():
            sel = self._auth_kit.get()
            urls = {tbl.item(i, "values")[1] for i in sel}
            removed = [u for u in urls if u in self._auth]
            for u in removed:
                self._auth.pop(u, None)
            if removed:
                save_auth_state(self._auth)
                engine.set_auth(self._auth)
                refresh_list()
                self.log("已删除 %d 个源的登录头" % len(removed))

        def clear_all():
            if not self._auth:
                return
            if messagebox.askyesno("登录头", "确定清空全部 %d 个源的登录头?"
                                   % len(self._auth), parent=win):
                self._auth = {}
                save_auth_state(self._auth)
                engine.set_auth(self._auth)
                refresh_list()
                self.log("已清空全部登录头。")

        ttk.Button(btns, text="保存到所选", command=save_sel).pack(side="left")
        ttk.Button(btns, text="删除所选", command=del_sel).pack(side="left", padx=4)
        ttk.Button(btns, text="清空全部", command=clear_all).pack(side="left", padx=4)
        btn_bstart = ttk.Button(btns, text="批量抓取",
                                command=lambda: batch_start(False))
        btn_bstart.pack(side="left", padx=4)
        var_skip_cfg = tk.BooleanVar(value=True)     # 重跑批量跳过已配站点
        ttk.Checkbutton(btns, text="跳过已配站",
                        variable=var_skip_cfg).pack(side="left", padx=(2, 0))
        btn_bauto = ttk.Button(btns, text="自动抓取",
                               command=lambda: batch_start(True))
        btn_bauto.pack(side="left")
        btn_bpause = ttk.Button(btns, text="暂停自动", command=toggle_pause)
        btn_skip = ttk.Button(btns, text="跳过该站", command=batch_skip)
        btn_bstop = ttk.Button(btns, text="结束批量", command=batch_stop)
        btn_fetch.config(command=on_fetch_click)
        ttk.Button(btns, text="关闭", command=close_win).pack(side="right")
        self._auth_kit = TreeMultiSelect(tbl, win, on_change=on_sel_change)
        var_filter.trace_add("write", lambda *_: refresh_list())
        win.protocol("WM_DELETE_WINDOW", close_win)
        refresh_list()
        poll_fetch()

    def _try_restore_selection(self):
        """搜索结果就绪后,按记忆恢复选中(容错:已不存在的条目自动跳过)。"""
        mem = self._mem_last
        if not mem or not mem.get("selected"):
            return
        avail = {self._hit_key(h) for h in self.hits}
        keep = restore_filter(mem.get("selected"), avail)
        if not keep:
            return
        by_key = {}
        for idx, iid in self.idx2iid.items():
            if 0 <= idx < len(self.hits):
                by_key[self._hit_key(self.hits[idx])] = iid
        want = [by_key[k] for k in keep if k in by_key]
        if want:
            self._apply_selection(want, notify=False)
            self.log("已按上次记忆恢复 %d 条选中" % len(want))

    # -------------------------------------------------- 框选/单击控制器 -----
    def _row_at(self, y):
        iid = self.tree.identify_row(y)
        return iid or None

    # —— 半透明橡皮筋框:拖动开始时创建一次,期间只改 geometry(性能) ——
    def _rubber_create(self):
        self._rubber_clear()
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
            top.attributes("-alpha", 0.25)
        except Exception:
            pass
        top.withdraw()                                  # 先隐藏,有面积再显示
        c = tk.Canvas(top, highlightthickness=0, bg="#1e80ff", bd=0)
        c.pack(fill="both", expand=True)
        # 松开/移动若落在遮罩上,同样走这里;坐标统一按"框左上角+局部偏移"换算
        c.bind("<ButtonRelease-1>", self._ms_release)
        c.bind("<B1-Motion>", self._ms_motion)
        self._rubber_top = (top, c)

    def _rubber_move(self, l, t, r, b):
        """把遮罩挪到 tree 局部坐标 (l,t)-(r,b);零面积时隐藏。"""
        if not self._rubber_top:
            return
        top = self._rubber_top[0]
        w, h = r - l, b - t
        if w < 1 or h < 1:
            try:
                top.withdraw()
            except Exception:
                pass
            return
        try:
            top.deiconify()
            top.geometry("%dx%d+%d+%d" % (w, h,
                                          self.tree.winfo_rootx() + l,
                                          self.tree.winfo_rooty() + t))
        except Exception:
            pass

    def _rubber_clear(self):
        if self._rubber_top:
            try:
                self._rubber_top[0].destroy()
            except Exception:
                pass
            self._rubber_top = None

    def _mods(self, e):
        """修饰键标记(按位读 state)。"""
        return {"ctrl": bool(e.state & 0x0004), "shift": bool(e.state & 0x0001)}

    def _ev_xy(self, e):
        """事件坐标统一为 tree 局部坐标:遮罩层事件按当前框左上角换算。"""
        try:
            if self._rubber_top and e.widget is self._rubber_top[1] and self._drag:
                l, t = self._drag["cur"][0], self._drag["cur"][1]
                return (l + e.x, t + e.y)
        except Exception:
            pass
        return (e.x, e.y)

    def _cache_rects(self):
        """拖动开始时缓存全部可见行的纵向区间,拖动中命中检测零 Tcl 调用。"""
        out = []
        for iid in self.tree.get_children():
            b = self.tree.bbox(iid)
            if b and b[3] > 0:
                out.append((iid, b[1], b[1] + b[3]))
        return out

    def _hit_rows(self, rect):
        """rect=(l,t,r,b) 与缓存行区间求交(行全宽,忽略横向)。"""
        _, t, _, b = rect
        out = set()
        for iid, y1, y2 in self._drag["rects"]:
            if y2 >= t and y1 <= b:
                out.add(iid)
        return out

    def _ms_press(self, e):
        self.tree.focus_set()
        try:
            if self.tree.identify_region(e.x, e.y) == "heading":
                return                     # 点表头:不参与选择
        except Exception:
            pass
        if self._drag:                     # 上一把没收尾的框选,先清理
            self._rubber_clear()
            self._drag = None
        row = self._row_at(e.y)
        if row:
            self.tree.focus(row)
        m = self._mods(e)
        self._press = {"x": e.x, "y": e.y, "row": row, "t": time.time(),
                       "ctrl": m["ctrl"], "shift": m["shift"]}

    def _ms_motion(self, e):
        """按住左键拖动:超过阈值进入橡皮筋框选(资源管理器式)。

        - 普通拖动:松开=只留框内(替换当前选中)。
        - Shift/Ctrl+拖动:松开=追加进现有选中。
        性能:行区间走拖动开始时的缓存,预览 ~30ms 节流,集合未变不刷 Treeview。
        """
        if not self._press:
            return
        p = self._press
        x, y = self._ev_xy(e)
        dx, dy = abs(x - p["x"]), abs(y - p["y"])
        if self._drag is None:
            if max(dx, dy) < MIN_DRAG:          # 低于阈值 → 仍是"单击",不框选
                return
            self._drag = {"x1": p["x"], "y1": p["y"],
                          "sel_before": set(self.tree.selection()),
                          "append": bool(p["shift"] or p["ctrl"]),
                          "rects": self._cache_rects(),
                          "cur": (p["x"], p["y"], p["x"], p["y"]),
                          "last_ts": 0.0, "last_sel": None}
            self._rubber_create()
            self.log("框选开始(松开确认 · %s · Esc 取消)" %
                     ("Shift/Ctrl 追加" if self._drag["append"] else "替换"))
        now = time.time()
        if now - self._drag["last_ts"] < 0.03:  # 节流:预览最多 ~33fps
            return
        self._drag["last_ts"] = now
        wv = max(self.tree.winfo_width(), 1)
        hv = max(self.tree.winfo_height(), 1)
        x2 = min(max(x, 0), wv - 1)
        y2 = min(max(y, 0), hv - 1)
        x1, y1 = self._drag["x1"], self._drag["y1"]
        rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self._drag["cur"] = rect
        self._rubber_move(*rect)
        sel = self._hit_rows(rect)
        if p["row"]:
            sel.add(p["row"])
        final = apply_rubber(self._drag["sel_before"], sel, self._drag["append"])
        if final != self._drag["last_sel"]:     # 集合没变就不刷 Treeview
            self._drag["last_sel"] = final
            self._apply_selection(final, notify=False)

    def _ms_release(self, e):
        p = self._press
        self._press = None
        if not p:
            return
        x, y = self._ev_xy(e)
        if self._drag is not None:
            # 松开 → 框选结束:命中 = 起点行 ∪ 框内/相交行
            d = self._drag
            x1, y1 = d["x1"], d["y1"]
            x2, y2 = (x if x is not None else x1), (y if y is not None else y1)
            rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            added = self._hit_rows(rect)
            if p["row"]:
                added.add(p["row"])
            final = apply_rubber(d["sel_before"], added, d["append"])
            self._rubber_clear()
            self._drag = None
            if p["row"]:
                self._anchor = p["row"]
            self._apply_selection(final, notify=True)
            self.log("框选结束:选中 %d 本%s" % (len(final), " (追加)" if d["append"] else ""))
            return
        # —— 单击(含双击)/修饰键点击 ——
        row = p.get("row") or self._row_at(y if y is not None else -1)   # 按下位置为准
        now = time.time()
        plain = not (p["ctrl"] or p["shift"])
        lc = self._last_click
        if (plain and lc and lc["row"] == row and row and
                (now - lc["t"]) * 1000 <= DOUBLE_MS and
                max(abs(x - lc["x"]), abs(y - lc["y"])) < 10):
            # 双击(无修饰键):维持原双击语义 = 下载该行
            self._last_click = None
            self._apply_selection({row}, notify=True)
            self._anchor = row
            self.start_download()
            return
        cur = set(self.tree.selection())
        if plain:
            new = {row} if row else set()
            if row:
                self._anchor = row
        elif p["ctrl"]:
            new = apply_click(cur, True, row)      # Ctrl:逐个增减,锚点不动
        else:                                      # Shift:从锚点连续选(替换)
            if row and self._anchor:
                new = apply_range(self.tree.get_children(), self._anchor, row)
            else:
                new = {row} if row else cur
        if new != cur:
            self._apply_selection(new, notify=True)
        if plain and row:
            self._last_click = {"row": row, "x": x, "y": y, "t": now}

    def _ms_escape(self):
        """框选过程中按 Esc:取消框选,恢复拖动前的选中状态。"""
        if self._drag is None:
            return
        d = self._drag
        self._rubber_clear()
        self._drag = None
        self._press = None
        self._apply_selection(d["sel_before"], notify=True)
        self.log("已取消框选(恢复拖动前选中 %d 本)" % len(d["sel_before"]))

    def _ms_right(self, e):
        """右键:保持 tree 默认语义(上层如需右键菜单,在此扩展,不与多选冲突)。"""

    def _ms_wheel(self, e):
        try:
            self.tree.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def _ms_wheel_scroll(self, e, step):
        try:
            self.tree.yview_scroll(step, "units")
        except Exception:
            pass

    def _on_key_nav(self, e):
        """接管 ↑/↓/Home/End/空格:单/双选模式下与鼠标策略一致。"""
        ks = e.keysym
        if ks not in ("Up", "Down", "Home", "End", "space"):
            return
        rows = self.tree.get_children()
        if not rows:
            return
        try:
            i = rows.index(self.tree.focus())
        except Exception:
            i = -1
        if ks == "Down":
            j = min(len(rows) - 1, i + 1)
        elif ks == "Up":
            j = max(0, i - 1)
        elif ks == "Home":
            j = 0
        elif ks == "End":
            j = len(rows) - 1
        else:                            # space
            row = rows[i] if 0 <= i < len(rows) else rows[0]
            cur = set(self.tree.selection())
            new = apply_click(cur, True, row)      # 空格:切换焦点行
            self.tree.focus(row)
            self.tree.see(row)
            if new != cur:
                self._apply_selection(new, notify=True)
            return "break"
        self.tree.focus(rows[j])
        self.tree.see(rows[j])
        if e.state & 0x0001:              # Shift+方向键:从锚点扩展
            base = self._anchor if self._anchor in rows else rows[j]
            new = apply_range(rows, base, rows[j])
            self._apply_selection(new, notify=True)
        elif not (e.state & 0x0004):      # 普通方向键:单选该行;Ctrl 只移焦点
            self._apply_selection({rows[j]}, notify=True)
            self._anchor = rows[j]
        return "break"

    # -------------------------------------------------- 多选辅助 -------------
    def _sel_indices(self):
        """当前选中行的 hits 下标(按表格顺序)。"""
        out = []
        for iid in self.tree.selection():
            tags = self.tree.item(iid, "tags")
            if tags:
                try:
                    out.append(int(tags[0]))
                except Exception:
                    pass
        return sorted(set(out))

    def _sync_sel_label(self):
        n = len(self._sel_indices())
        self.lbl_sel.config(text="已选 %d 本" % n)
        self.btn_dl.config(text="⬇ 下载选中(%d)" % n if n else "⬇ 下载选中")

    def sel_all(self):
        self._apply_selection(self.tree.get_children(), notify=True)

    def sel_none(self):
        self._apply_selection([], notify=True)

    def sel_invert(self):
        cur = set(self.tree.selection())
        self._apply_selection([i for i in self.tree.get_children() if i not in cur],
                              notify=True)

    # ---------------------------------------------------- 下载 ---------------
    def start_download(self):
        if self.busy_dl or self.busy_verify:
            return
        idxs = self._sel_indices()
        if not idxs:
            messagebox.showinfo("提示", "先在结果里选中一本书(拖拽/Ctrl/Shift 可多选)")
            return
        self.sel_idx = idxs
        hits = [self.hits[i] for i in idxs]
        dlg = DownloadDialog(self.root, len(hits),
                             default_fmt=self.var_fmt.get(),
                             default_mode=self.var_mode.get())
        if not dlg.result:
            return
        mode, fmt = dlg.result
        self.var_mode.set(mode)
        self.var_fmt.set(fmt)

        out = self.var_out.get()
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("目录错误", str(e))
            return
        self.stop_dl.clear()
        self.busy_dl = True
        self._dl_t0 = time.time()
        self._tick_last = -1
        self.lbl_tick.config(text="已用时 0s")
        self.btn_dl.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.pbar.config(value=0)
        self.lbl_dl.config(text="准备下载… %d 本 · 格式 %s" % (len(hits), fmt.upper()))
        self.log("开始下载 %d 本 · 模式[%s] · 格式[%s]" %
                 (len(hits), "下载单一" if mode == "single" else "合并下载", fmt.upper()))
        threading.Thread(target=self._do_download,
                         args=(hits, out, mode, fmt, idxs), daemon=True).start()

    def _mark_blocked(self, fail_list):
        """把下载失败(被封/需登录/空目录)的行标红,书源列加 ✖ 前缀。"""
        for idx, _nm, srcname, _reason in fail_list:
            iid = self.idx2iid.get(idx)
            if not iid or iid not in self.tree.get_children():
                continue
            vals = list(self.tree.item(iid, "values"))
            if len(vals) == 5 and not vals[4].startswith("✖"):
                vals[4] = "✖ " + vals[4]
            self.tree.item(iid, values=vals, tags=(str(idx), "blocked"))

    def _export_one(self, book, out, fmt, batch):
        """按选定格式导出单一格式。batch=True 时同名不同源自动加书源后缀,避免覆盖。"""
        ext = "txt" if fmt == "txt" else "epub"
        suffix = ""
        if batch and (Path(out) / ("%s.%s" % (export.safe_name(book["title"]), ext))).exists():
            suffix = "_" + export.safe_name(book.get("source", ""))
        if fmt == "txt":
            return export.export_txt(book, out, suffix)
        return export.export_epub(book, out, suffix)

    def _try_one(self, h, prog, out, fmt, batch):
        """探测并下载一本书。返回 (book, path) 或抛异常。被封源直接抛 RuntimeError。

        探测(目录)阶段限时:单请求 6s、总 15s——半死源快速失败,
        "下载单一"模式能尽快换下一个候选,不会长时间停在探测上。
        """
        srcname = h["source"].get("bookSourceName", "?")
        # P1 元数据补全:详情页按语义标签抽取,比搜索列表干净;失败回退搜索值。
        # 只影响导出文件的书名/作者/分类/最新章节,不影响该行选中(键=源+URL)。
        try:
            info = engine.fetch_book_info(h["source"], h["book_url"], timeout=6)
        except Exception:
            info = {}
        if info:
            for k in ("name", "author", "kind", "last_chapter"):
                if info.get(k):
                    h[k] = info[k]
            self.log("↻ 元数据已按详情页修正: [%s]《%s》" % (srcname, h["name"]))
        toc = engine.fetch_toc(h["source"], h["book_url"], stop=self.stop_dl,
                               timeout=6, deadline=time.time() + 15)
        if not toc:
            raise RuntimeError("目录为空(书源被封或需登录)")
        book = engine.load_book(h, toc=toc, on_progress=prog, stop=self.stop_dl,
                                workers=DOWNLOAD_WORKERS)
        if self.stop_dl.is_set():
            raise RuntimeError("已取消")
        if book["ok"] == 0:
            raise RuntimeError("正文 0/%d 章成功(书源被封)" % book["total"])
        path = self._export_one(book, out, fmt, batch)
        self.log("✔ 《%s》 %d/%d 章 · 源[%s] → %s" %
                 (book["title"], book["ok"], book["total"], srcname, path))
        return book, path

    def _do_download(self, hits, out, mode, fmt, hit_idx=None):
        """hit_idx:hits 各元素在 self.hits 中的真实下标(用于标红失败行)。"""
        try:
            self._do_download_inner(hits, out, mode, fmt, hit_idx)
        except Exception:
            # 下载线程的任何异常都必须可见(pythonw 下 stderr 不可见,
            # 否则表现为"永远停在准备下载")
            import traceback
            self.q.put(("log", "✘ 下载线程异常: %s" % traceback.format_exc()[-500:]))
            self.q.put(("dlerr", "下载线程异常,已终止: %s" % traceback.format_exc()[-200:]))

    def _do_download_inner(self, hits, out, mode, fmt, hit_idx=None):
        """hit_idx:hits 各元素在 self.hits 中的真实下标(用于标红失败行)。"""
        n = len(hits)
        hit_idx = hit_idx or list(range(n))

        def prog(done, total, msg):
            self.q.put(("dlprog", (done, total, msg)))

        ok_list, fail_list = [], []
        for bi, h in enumerate(hits):
            if self.stop_dl.is_set():
                break
            srcname = h["source"].get("bookSourceName", "?")
            self.q.put(("dlbook", (bi, n, h["name"], srcname,
                                   "探测目录(超时 6s,失败自动换源)…")))
            try:
                book, path = self._try_one(h, prog, out, fmt, batch=(mode == "batch"))
            except Exception as e:
                self.q.put(("log", "✘ 跳过[%s]《%s》: %s" % (srcname, h["name"], e)))
                fail_list.append((hit_idx[bi], h["name"], srcname, str(e)))
                continue              # 被封/失败 → 单一模式换下一个,合并模式继续下一本
            ok_list.append((book, path))
            self.q.put(("dlone", (book, path, bi, n)))
            if mode == "single":
                break                 # 单一模式:只保留第一本成功的,其余丢弃
        if self.stop_dl.is_set():
            self.q.put(("dlcancel", (ok_list, fail_list)))
        else:
            self.q.put(("dldone", (mode, fmt, ok_list, fail_list)))

    # ------------------------------------------------------- 事件泵 ----------
    def _drain(self):
        """UI 事件泵:任何单条事件的处理异常都不允许杀死循环——
        一旦 after 链断了,所有状态标签会永久冻结(表现为"停在准备下载")。"""
        import traceback as _tb
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    self._handle_event(kind, payload)
                except Exception:
                    self._append("✘ 事件处理异常[%s]: %s" %
                                 (kind, _tb.format_exc()[-400:]))
        except queue.Empty:
            pass
        # 下载进行中的用时跳动(让"探测/正文抓取中" visibly 活着)
        if self.busy_dl and getattr(self, "_dl_t0", None):
            s = int(time.time() - self._dl_t0)
            if s != getattr(self, "_tick_last", -1):
                self._tick_last = s
                self.lbl_tick.config(text="已用时 %ds" % s)
        self.root.after(120, self._drain)

    def _handle_event(self, kind, payload):
        if kind == "log":
            self._append(payload)
        elif kind == "sprog":
            self.lbl_progress.config(text=payload)
        elif kind == "hit":
            h = payload
            # 增量上屏(保持引擎去重语义:同一URL只入一次)
            if any(x["book_url"] == h["book_url"] and
                   x["source"]["bookSourceName"] == h["source"]["bookSourceName"]
                   for x in self.hits):
                return
            if self.var_rel.get() and not self._relevant(h):
                return                                    # 无关结果不上屏
            self.hits.append(h)
            self._insert_hit_row()
            self.lbl_hits.config(text="搜索中… 已返回 %d 条" % len(self.hits))
        elif kind == "sres":
            n, fuzzy = payload
            self.hits = merge_hits(dedupe_hits(self.hits))
            self.hits = self._ordered_hits()   # 同书组相邻,组内按完整度/相关度
            self._fill_results()
            self.lbl_progress.config(text="完成")
            self.lbl_hits.config(text="共找到 %d 条结果" % len(self.hits))
            self.log("搜索完成,共 %d 条(同书已分组相邻)%s" % (len(self.hits),
                    " · 组间按相关度排序" if fuzzy else ""))
            try:                     # 恢复选中即使出错,也必须解开搜索按钮
                self._try_restore_selection()
            finally:
                self._set_busy(False)
        elif kind == "vfile":
            fi, n_files, fn = payload
            self.lbl_verify.config(text="校验中 · 文件 %d/%d · %s" % (fi, n_files, fn))
            self.log("开始校验文件 %d/%d: %s" % (fi, n_files, fn))
        elif kind == "vprog":
            fi, n_files, fn, done, total, ng, nb = payload
            self.lbl_verify.config(text="校验中 文件 %d/%d · %s · %d/%d · 有效 %d · 失效 %d"
                                   % (fi, n_files, fn, done, total, ng, nb))
        elif kind == "vfile_done":
            fn, origin, n_ok, n_bad, elapsed = payload
            now = time.time()
            self.verify_dones[fn] = {"origin": origin, "time": now}
            self._mem_save_core()
            self.log("✔ 文件 %s 校验完成:有效 %d · 失效 %d · 耗时 %.0fs"
                     % (fn, n_ok, n_bad, elapsed))
        elif kind == "vdeep":
            fi, n_files, fn, phase, done, total = payload
            self.lbl_verify.config(text="深度校验 文件 %d/%d · %s · %s %d/%d"
                                   % (fi, n_files, fn, phase, done, total))
        elif kind == "vdone":
            n_files, tot_ok, tot_bad, elapsed, aborted = payload
            self.busy_verify = False
            self._set_busy(False)
            self.btn_verify.config(state="normal")
            self.btn_deep.config(state="normal")
            self._reload_all()                  # good 表更新后重载合并源
            if aborted:
                self.lbl_verify.config(text="校验已中止 · 有效 %d · 失效 %d" % (tot_ok, tot_bad))
                self.log("校验已中止 · 总有效 %d · 总失效 %d · 耗时 %.0fs"
                         % (tot_ok, tot_bad, elapsed))
            else:
                self.lbl_verify.config(text="已校验 %d 文件 · 有效 %d · 失效 %d · 耗时 %.0fs"
                                       % (n_files, tot_ok, tot_bad, elapsed))
                self.log("全部校验完成: %d 文件 · 有效 %d · 失效 %d · 耗时 %.0fs"
                         % (n_files, tot_ok, tot_bad, elapsed))
        elif kind == "dlprog":
            done, total, msg = payload
            self.pbar.config(maximum=max(total, 1), value=done)
            self.lbl_dl.config(text="正文 %d/%d · %s" % (done, total, msg))
        elif kind == "dlbook":
            bi, n, name, srcname, msg = payload
            self.pbar.config(value=0)
            self.lbl_dl.config(text="第 %d/%d 本《%s》 · 源[%s] · %s" %
                               (bi + 1, n, name, srcname, msg))
        elif kind == "dlone":
            book, path, bi, n = payload
            self.log("已保存: %s" % path)
        elif kind == "dldone":
            mode, fmt, ok_list, fail_list = payload
            self.busy_dl = False
            self._set_busy(False)
            self.btn_dl.config(state="normal")
            self._mark_blocked(fail_list)
            self.lbl_tick.config(text="")
            self.pbar.config(value=self.pbar["maximum"] if ok_list else 0)
            if ok_list:
                total_ok = sum(b["ok"] for b, _ in ok_list)
                total_ch = sum(b["total"] for b, _ in ok_list)
                self.lbl_dl.config(text="完成:%d 本 · %d/%d 章" %
                                   (len(ok_list), total_ok, total_ch))
                lines = ["《%s》 %d/%d 章" % (b["title"], b["ok"], b["total"])
                         for b, _ in ok_list]
                detail = "\n".join(lines)
                if fail_list:
                    detail += "\n\n已跳过被封/失败 %d 个:\n" % len(fail_list)
                    detail += "\n".join("· [%s] %s" % (s, r)
                                        for _, _, s, r in fail_list[:8])
                self.log("下载结束:成功 %d 本,跳过 %d 个" % (len(ok_list), len(fail_list)))
                messagebox.showinfo(
                    "下载完成",
                    "成功 %d 本 · 格式 %s\n\n%s\n\n保存目录:\n%s" %
                    (len(ok_list), fmt.upper(), detail, self.var_out.get()))
                self.open_out()
            else:
                self.lbl_dl.config(text="全部失败")
                detail = "\n".join("· [%s]《%s》: %s" % (s, nm, r)
                                   for _, nm, s, r in fail_list[:10])
                self.log("✘ 全部失败:%d 个候选" % len(fail_list))
                messagebox.showerror(
                    "下载失败",
                    "选中的 %d 个候选全部不可用(被封/需登录/无目录):\n\n%s" %
                    (len(fail_list) or 1, detail or "未知原因"))
        elif kind == "dlcancel":
            ok_list, fail_list = payload
            self.busy_dl = False
            self._set_busy(False)
            self.btn_dl.config(state="normal")
            self.pbar.config(value=0)
            self.lbl_tick.config(text="")
            self.lbl_dl.config(text="已取消")
            self.log("已取消(成功 %d / 跳过 %d)" % (len(ok_list), len(fail_list)))
        elif kind == "dlerr":
            self.busy_dl = False
            self._set_busy(False)
            self.btn_dl.config(state="normal")
            self.pbar.config(value=0)
            self.lbl_tick.config(text="")
            self.lbl_dl.config(text="失败")
            self.log("✘ %s" % payload)
            messagebox.showerror("下载失败", payload)
    def _fill_results(self):
        for it in self.tree.get_children():
            self.tree.delete(it)
        self.idx2iid = {}
        self._gtag = {}
        for _h in self.hits:
            self._insert_hit_row()
        self._sync_sel_label()
        self.lbl_hits.config(text="共找到 %d 条结果" % len(self.hits))

    def _append(self, s):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", s + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

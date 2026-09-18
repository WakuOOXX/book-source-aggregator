# -*- coding: utf-8 -*-
"""core.config —— 业务常量与运行目录(自 app.py 剥离,零 tkinter)。

并发数口径的实测依据(2026-09-10,全量 3393 源、12 逻辑核,关键词「剑来」):
  并发  40 → 90.2s      并发 256 → 28.2 / 28.3s
  并发 384 → 18.3 / 20.4s   并发 512 → 18.7 / 42.1s(开始不稳)
CPU 全程只占 1~1.4/12 核 —— 瓶颈是"最慢单源最多等 12s"的超时尾巴,
不是算力。384 是收益/稳定性的拐点,再加只会放大尾部波动。
注意:小批量搜索(十几个到几百个源)提并发没有意义,因为总耗时会撞上
12s 超时下界:实测 240 个源在并发 40~600 之间都是 12s 左右。
"""
import os
import sys
from pathlib import Path

# ---- 运行目录 / 数据目录 ---------------------------------------------------
# APP_DIR: 程序资源定位(exe/仓库根),只读用途;
# DATA_DIR: 全部用户数据的根(书源目录/下载/记忆/登录抓取 profile),
#   优先级 BOOKDL_DATA_DIR 环境变量 > 打包形态 %LOCALAPPDATA%\BookSourceAggregator
#   > 开发形态仓库根。打包安装到 Program Files 后 exe 目录不可写,
#   所以 frozen 默认必须落 %LOCALAPPDATA%。
if getattr(sys, "frozen", False):          # PyInstaller 打包后
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / "BookSourceAggregator"
    return APP_DIR


def _resolve_data_dir() -> Path:
    env = (os.environ.get("BOOKDL_DATA_DIR") or "").strip()
    return Path(env).resolve() if env else _default_data_dir()


DATA_DIR = _resolve_data_dir()
SEED_SOURCE = APP_DIR / "seed" / "bookSource.json"   # 出厂种子书源(安装包内置)

SOURCE_DIR = DATA_DIR / "shuyuan"          # 书源 JSON 统一放这里
DEFAULT_SOURCE = SOURCE_DIR / "bookSource.json"
DEFAULT_OUT = DATA_DIR / "downloads"
STATE_FILE = DATA_DIR / "sel_state.json"   # 多选/选中项记忆 + 校验原始表路径
AUTH_PROFILE_DIR = DATA_DIR / "auth_profile"   # 登录抓取浏览器 profile


def set_data_dir(path) -> Path:
    """把全部用户数据路径整体改指新目录(server.data.setdir 迁移后调用)。

    各路径都是本模块级变量,消费方(core.artifacts / server)经
    `_config.XXX` 动态读取,改完即生效;cookie 库路径同样动态解析。
    """
    global DATA_DIR, SOURCE_DIR, DEFAULT_SOURCE, DEFAULT_OUT, STATE_FILE
    global AUTH_PROFILE_DIR
    DATA_DIR = Path(path).resolve()
    SOURCE_DIR = DATA_DIR / "shuyuan"
    DEFAULT_SOURCE = SOURCE_DIR / "bookSource.json"
    DEFAULT_OUT = DATA_DIR / "downloads"
    STATE_FILE = DATA_DIR / "sel_state.json"
    AUTH_PROFILE_DIR = DATA_DIR / "auth_profile"
    return DATA_DIR

# ---- 并发度 ---------------------------------------------------------------
# 搜索与校验:每个书源只发 1 个请求,3393 个源分布在 2107 个域名上,
# 对单个站的压力不随总并发上升,所以可以开大。
SEARCH_WORKERS = 384
VERIFY_WORKERS = 384
# 校验搜索兜底:这些失效原因的源值得用真实搜索规则复测(首页被 WAF 拦/超时
# ≠ 源不可用,起点 202、69书吧 403 这类首页拦爬虫但搜索接口正常的源很多)。
SEARCH_FALLBACK_REASONS = {"timeout", "http_403", "http_429", "http_503"}
# 批量自动抓取:站点内无鼠标/键盘操作满该秒数 → 自动抓取并跳下一站;
# 用户在页面里点击/输入(CDP 注入监听)会重置倒计时,给登录留时间。
AUTO_IDLE_SECS = 5
AUTO_IDLE_FAST = 1.5    # 已配过 Cookie 的站刷新会话用快档
AUTO_PARA_TABS = 10     # 并行扫/并行登录扫的标签数默认值(「登录头」窗口「并行标签」可调 2~20)
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

# 校验产物后缀 = 可再生缓存(*.good.json / *.error.json / *.deep.json)
VERIFY_ARTIFACT_SUFFIXES = (".good.json", ".error.json", ".deep.json")

# -*- coding: utf-8 -*-
"""core.artifacts —— 纯逻辑函数集(自 app.py 剥离,零 tkinter)。

包含:校验产物路径与读写(good/deep/error 表)、深度校验判定与摘要格式化、
登录头(auth_state)存储、并行登录扫纯函数、校验产物缓存清理。
"""
import json
import os
import re
import time
from pathlib import Path

from core import config as _config
from core.config import (AUTO_IDLE_SECS, AUTO_IDLE_FAST, AUTO_PARA_TABS,
                         VERIFY_ARTIFACT_SUFFIXES)

# ------------------------------------------------------------- 产物路径 -----


def good_table_path(origin: Path) -> Path:
    """原始全量表对应的有效书源表:<原名>.good.json。"""
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


# --------------------------------------------------------- 登录头抓取辅助 ----


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


def _host_match(ph, host):
    """页面主机名是否命中目标域(相等或互为子域)—— 到站判定,与 cdp_cookie 口径一致。"""
    return bool(ph) and (ph == host or ph.endswith("." + host)
                         or host.endswith("." + ph))


def _para_idle(configured):
    """并行登录扫分级静默:该站已配过 Cookie 用快档(刷新会话),全新站慢档(留登录时间)。"""
    return AUTO_IDLE_FAST if configured else AUTO_IDLE_SECS


def _para_clamp(v, default=AUTO_PARA_TABS):
    """「并行标签」取值清洗:非法输入回退 default,越界收敛到 2~20。"""
    try:
        n = int(str(v).strip())
    except Exception:
        return default
    return 2 if n < 2 else (20 if n > 20 else n)


def _tab_due(arrived, idle, since_act, opened_for):
    """并行登录扫单标签判定(纯函数):返回 "grab" / "timeout" / "wait"。

    - 已到站且静默满 idle 秒 → grab(抓完即关,没抓到也关);
    - 未到站且打开超过硬时限 max(10, 3×idle) → timeout(死站不再干等);
    - 其余(含已到站但用户仍在操作,静默未满)→ wait,保护登录中的标签。
    """
    if arrived and since_act >= idle:
        return "grab"
    if not arrived and opened_for >= max(10.0, idle * 3):
        return "timeout"
    return "wait"


# --------------------------------------------------------------- 书源分组 ----


def split_groups(source):
    """书源 bookSourceGroup → 分组名列表(逗号/换行分隔,去空白与空项)。"""
    raw = (source.get("bookSourceGroup") or "")
    return [g.strip() for g in re.split(r"[,\n]", str(raw)) if g.strip()]


def collect_groups(srcs):
    """全部书源的分组名合集(排序去重);未标分组的源不归入任何分组。"""
    groups = set()
    for s in srcs:
        groups.update(split_groups(s))
    return sorted(groups)


def filter_by_group(srcs, group):
    """按分组过滤书源(纯函数):group 为空或「全部」原样返回。"""
    g = (group or "").strip()
    if not g or g == "全部":
        return srcs
    return [s for s in srcs if g in split_groups(s)]


# ------------------------------------------------------------- 登录头存储 ----


def auth_state_path() -> Path:
    """每源登录头存储:shuyuan/auth_state.json。

    属用户凭据,**不属于缓存** —— 「清除缓存」不清理,在登录头页查看/修改/清空,
    「清除数据」(data.clear)会一并带走。写入与 good 表同款原子写。
    """
    return _config.SOURCE_DIR / "auth_state.json"


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


# --------------------------------------------------------- 校验产物缓存 -----


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
        if _config.SOURCE_DIR.exists():
            return [p for p in sorted(_config.SOURCE_DIR.iterdir())
                    if p.is_file() and is_verify_artifact(p.name)]
    except Exception:
        pass
    return []


def cleanup_verify_artifacts(fn, origin, source_dir=None, log=None):
    """删除指定书源文件的校验产物(.good.json / .error.json / .deep.json)。

    source_dir 缺省用 core.config.SOURCE_DIR(origin 为文件名字符串时需要);
    log 为消息回调(可选)。UI 层负责再清除自己的验证记录(verify_dones)。
    """
    sd = source_dir if source_dir is not None else _config.SOURCE_DIR
    for suffix in VERIFY_ARTIFACT_SUFFIXES:
        p = sd / fn if isinstance(origin, str) else origin
        target = p.with_name(p.stem + suffix)
        if target.exists():
            try:
                target.unlink()
                if log:
                    log("已清理: %s" % target.name)
            except Exception as e:
                if log:
                    log("⚠ 清理 %s 失败: %s" % (target.name, e))

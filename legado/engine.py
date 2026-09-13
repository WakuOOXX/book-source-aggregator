# -*- coding: utf-8 -*-
"""下载编排:搜索 → 目录 → 并发抓正文。GUI/CLI 共用。"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

from . import rules
from . import jsengine
from .fetcher import fetch, DEFAULT_UA
from .normalize import (clean_author, clean_kind, clean_last_chapter,
                        clean_name, dedupe_hits, merge_hits)

BOOK_SOURCE_FILE = "bookSource.json"

# ---------------------------------------------------------------- 登录头 ----
# 每源用户登录态(Cookie/自定义 header),key = bookSourceUrl。
# GUI 从 shuyuan/auth_state.json 加载并经 set_auth() 注入;没有 GUI 时为空表,
# 行为与旧版一致(只用书源自带 header)。注入优先级:书源 header 字段 <
# 内嵌请求头(searchUrl/bookUrl 的 ,{json})< 用户登录头(最高,凭据优先)。
_AUTH = {}


def set_auth(auth):
    """GUI 启动/保存登录头时刷新模块级注入表。auth = {bookSourceUrl: {...}}"""
    global _AUTH
    _AUTH = dict(auth or {})


def source_headers(source, extra=None) -> dict:
    """构造该源请求基础头:书源 header 字段 → extra(内嵌请求头)→ 用户登录头。

    用户 Cookie 整体覆盖低层同名字段。UA 缺省回退 DEFAULT_UA,让校验等
    不依赖 session 的裸请求也带现代 UA。
    """
    headers = dict(rules.parse_header(source.get("header")) or {})
    if extra:
        headers.update(extra)
    user = _AUTH.get((source.get("bookSourceUrl") or "").strip()) or {}
    uh = user.get("header")
    if isinstance(uh, dict):
        headers.update(uh)
    if user.get("cookie"):
        headers["Cookie"] = str(user["cookie"])
    if not headers.get("User-Agent"):
        headers["User-Agent"] = DEFAULT_UA
    return headers


def load_sources(path=BOOK_SOURCE_FILE):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [d for d in data if isinstance(d, dict)]


def source_groups(sources):
    from collections import Counter
    c = Counter((d.get("bookSourceGroup") or "").strip() or "(未分组)" for d in sources)
    return c.most_common()


def critical_js(src) -> str:
    """返回用到的关键规则中是否含 @js/<js>。有则返回说明串,否则 ''。

    v1.7.0:JS 引擎(mini-racer)可用时不再整源跳过 —— 含 JS 的源照常参与
    搜索/下载,脚本失败由规则层归约为空结果;无引擎时行为不变。
    """
    if jsengine.HAS_JS:
        return ""
    fields = [
        ("search", src.get("searchUrl", "")),
        ("header", src.get("header", "")),
        ("ruleSearch", json.dumps(src.get("ruleSearch") or {}, ensure_ascii=False)),
        ("ruleToc", json.dumps(src.get("ruleToc") or {}, ensure_ascii=False)),
        ("ruleContent", json.dumps(src.get("ruleContent") or {}, ensure_ascii=False)),
        ("ruleBookInfo", json.dumps(src.get("ruleBookInfo") or {}, ensure_ascii=False)),
    ]
    for name, text in fields:
        if rules.uses_js_text(str(text)):
            return name
    return ""


def _clamp_timeout(src):
    rt = src.get("respondTime") or 8000
    try:
        rt = int(rt) / 1000.0
    except Exception:
        rt = 8
    return min(max(rt, 4), 12)


def make_key_variants(key: str):
    """模糊搜索关键词变体:按分隔符取首段、去尾字、截前段。不含原词、至少 2 字。"""
    key = (key or "").strip()
    if not key:
        return []
    first = re.split(r"[\s,，。、;；/|·]+|[《》\"'“”()（）]", key, maxsplit=1)[0].strip() or key
    cands = []
    if first != key:
        cands.append(first)
    src = first if len(first) >= 2 else key
    if len(src) >= 4:
        cands.append(src[:-1])          # 去尾字(如 斗破苍 → 斗破苍穹)
    if len(first) >= 6:
        cands.append(first[:4])         # 超长时截前4字
    out, seen = [], {key}
    for s in cands:
        s = s.strip()
        if len(s) >= 2 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _searchable(s) -> bool:
    rs = s.get("ruleSearch") or {}
    if not (s.get("enabled", True)):
        return False
    if not (s.get("searchUrl") and isinstance(rs, dict) and rs.get("bookList")):
        return False
    return not critical_js(s)


def search_sources(sources, key, on_progress=None, stop=None, workers=24,
                   on_hit=None, fuzzy=False):
    """并发搜索。

    - 返回候选列表(同源同URL去重 + 跨源同书合并后)。每个候选为
      dict(source/name/author/book_url/...),并注记 _group_key/_src_count/
      _group_first(同书分组信息,同组行在返回列表中相邻)。
    - on_progress(done, total, msg):每完成一个源回调一次(整批源只预筛一次)。
    - on_hit(hit):每命中一条即时回调(供 GUI 增量上屏)。
      【取舍(2026-09-09 定案)】即时回调的条目**不带**同书分组注记
      (跨源合并需等整轮结果才能算),GUI 过程态用 fallback key 自成一组,
      无害;搜索结束事件里 GUI 必做 merge_hits(dedupe_hits(hits)) 整表
      重建,分组/相邻/排序以最终态为准。换来的是结果逐条即时可见,
      而非整轮结束才一次性上屏。
    - fuzzy=True:直搜 0 命中时,自动用 make_key_variants 生成的变体,仅对
      "本轮网络存活的源"重试,直到有命中或变体耗尽。
    """
    if stop is None:
        stop = threading.Event()
    usable = [s for s in sources if _searchable(s)]
    all_hits = []

    def run_round(cands, k, label):
        total = len(cands)
        done = 0
        round_hits, round_alive = [], []

        def one(s):
            rs = s.get("ruleSearch") or {}
            try:
                base = (s.get("bookSourceUrl") or "").strip()
                # 页面上下文:searchUrl JS(签名/时间戳等)需要 key/baseUrl
                rules.set_page_context(key=k, page=1, src_key=s.get("bookSourceName", "?"),
                                       base_url=base)
                req = rules.parse_request(s["searchUrl"], {"key": k, "page": 1, "baseUrl": base})
                if base and not req["url"].lower().startswith(("http://", "https://")):
                    req["url"] = urljoin(base, req["url"])
                headers = source_headers(s, req.get("headers"))
                url, text = fetch(s.get("bookSourceName", "?"), req["url"], req["method"],
                                  req.get("body", ""), headers, _clamp_timeout(s),
                                  retry=req.get("retry") or 0,
                                  charset=req.get("charset") or "")
                # 搜索页上下文:ruleSearch 内 JS(java.getString 等)可用
                rules.set_page_context(base_url=url, page_text=text)
            except Exception:
                return s, [], False
            if not text:
                return s, [], False
            try:
                dom = rules.parse_dom(text)
            except Exception:
                return s, [], False
            out = []
            try:
                items = rules.extract_list(dom, rs.get("bookList") or "")
                for it in items:
                    try:
                        name = clean_name(rules.extract_value(it, rs.get("name") or ""))
                        if not name:
                            continue
                        bu = rules.extract_value(it, rs.get("bookUrl") or "")
                        if not bu:
                            continue
                        out.append({
                            "source": s,
                            "name": name,
                            "author": clean_author(name, rules.extract_value(it, rs.get("author") or "")),
                            "kind": clean_kind(name, rules.extract_value(it, rs.get("kind") or "")),
                            "book_url": urljoin(url, bu),
                            "last_chapter": clean_last_chapter(name, rules.extract_value(it, rs.get("lastChapter") or "")),
                            "intro": rules.extract_value(it, rs.get("intro") or ""),
                        })
                    except Exception:
                        continue
            except Exception:
                pass
            return s, out, True

        with ThreadPoolExecutor(max_workers=max(2, workers)) as ex:
            futs = {ex.submit(one, s): s for s in cands}
            for fut in as_completed(futs):
                done += 1
                s = futs[fut]
                try:
                    _, hs, ok = fut.result()
                except Exception:
                    _, hs, ok = s, [], False
                if ok:
                    round_alive.append(s)
                round_hits.extend(hs)
                if on_hit:
                    for h in hs:
                        try:
                            on_hit(h)
                        except Exception:
                            pass
                if on_progress:
                    try:
                        on_progress(done, total, "%s%s ×%d" % (label, s.get("bookSourceName", "?"), len(hs)))
                    except Exception:
                        pass
                if stop.is_set():
                    for f2 in futs:
                        f2.cancel()
                    break
        return round_hits, round_alive

    alive = list(usable)
    all_hits, alive = run_round(alive, key, "")
    # 模糊兜底:直搜无果 → 变体重试(仅对本轮存活源,每变体至多一轮)
    if fuzzy and not all_hits and alive and not stop.is_set():
        for v in make_key_variants(key):
            if stop.is_set():
                break
            if on_progress:
                try:
                    on_progress(0, 0, "直搜无果 → 模糊变体「%s」重试 %d 源" % (v, len(alive)))
                except Exception:
                    pass
            vhits, alive = run_round(alive, v, "[模糊]")
            all_hits = vhits
            if all_hits:
                break
    # 去重(同源同 URL) + 跨源同书合并(注记 _group_key/_src_count/_group_first)
    return merge_hits(dedupe_hits(all_hits))


def fetch_book_info(source, book_url, timeout=None):
    """按 ruleBookInfo 抓书籍详情页,返回清洗后的元数据 dict(name/author/
    kind/last_chapter/intro,仅含非空项);失败或规则含 JS 时返回 {}。

    用途(P1 元数据补全):搜索列表的字段清洗只能"救"错列文本,
    详情页按语义标签抽取的值更干净,下载前用它覆盖搜索列表脏值。
    """
    rbi = source.get("ruleBookInfo") or {}
    if not isinstance(rbi, dict) or not rbi:
        return {}
    # 规则含 JS:无引擎时跳过(旧行为);有引擎时交给 JS 引擎跑
    if rules.uses_js_text(json.dumps(rbi, ensure_ascii=False)) and not jsengine.HAS_JS:
        return {}
    try:
        req = rules.parse_request(book_url, {})
        headers = source_headers(source, req.get("headers"))
        url, text = fetch(source.get("bookSourceName", "?"), req["url"],
                          req["method"], req.get("body", ""), headers,
                          timeout if timeout else _clamp_timeout(source),
                          retry=req.get("retry") or 0,
                          charset=req.get("charset") or "")
        if not text:
            return {}
        dom = rules.parse_dom(text)
        rules.set_page_context(base_url=url, page_text=text)
        out = {}
        for field, key in (("name", "name"), ("author", "author"),
                           ("kind", "kind"), ("lastChapter", "last_chapter"),
                           ("intro", "intro")):
            rule = rbi.get(field)
            if not rule:
                continue
            try:
                raw = (rules.extract_value(dom, rule) or "").strip()
            except Exception:
                raw = ""
            if not raw:
                continue
            if key == "name":
                v = clean_name(raw)
            elif key == "author":
                v = clean_author(out.get("name", ""), raw)
            elif key == "kind":
                v = clean_kind(out.get("name", ""), raw)
            elif key == "last_chapter":
                v = clean_last_chapter(out.get("name", ""), raw)
            else:
                v = raw
            if v:
                out[key] = v
        return out
    except Exception:
        return {}


def _resolve_toc_url(source, book_url, timeout=None):
    """部分源需在书籍页找目录链接。默认书页即目录页。"""
    toc_rule = (source.get("ruleToc") or {}).get("tocUrl")
    if not toc_rule:
        toc_rule = (source.get("ruleBookInfo") or {}).get("tocUrl")
    if not toc_rule:
        return book_url
    try:
        req = rules.parse_request(book_url, {})
        headers = source_headers(source, req.get("headers"))
        url, text = fetch(source.get("bookSourceName", "?"), req["url"], req["method"],
                          req.get("body", ""), headers,
                          timeout if timeout else _clamp_timeout(source),
                          retry=req.get("retry") or 0,
                          charset=req.get("charset") or "")
        dom = rules.parse_dom(text)
        rules.set_page_context(base_url=url, page_text=text)
        href = rules.extract_value(dom, toc_rule)
        if href:
            return urljoin(url, href)
    except Exception:
        pass
    return book_url


def fetch_toc(source, book_url, on_progress=None, stop=None, timeout=None, deadline=None):
    """抓目录,返回章节列表 [(name, url), ...]。分页(option 类)一并并入。

    timeout: 单次请求超时秒数(缺省按书源 respondTime 夹取 [4,12])。
    deadline: time.time() 时间戳,超过后停止翻页(探测阶段用来限制总耗时)。
    """
    rc = source.get("ruleContent") or {}
    if (isinstance(rc, dict)
            and rules.uses_js_text(json.dumps(rc, ensure_ascii=False))
            and not jsengine.HAS_JS):
        raise RuntimeError("该源正文规则依赖 JS,且 JS 引擎不可用")
    toc_url = _resolve_toc_url(source, book_url, timeout)
    rt = source.get("ruleToc") or {}
    if not isinstance(rt, dict) or not rt.get("chapterList"):
        raise RuntimeError("缺少 ruleToc.chapterList")
    chapters = []
    seen = set()
    cur = toc_url
    guard = 0
    headers = source_headers(source)
    rules.set_page_context(src_key=source.get("bookSourceName", "?"))
    while cur and guard < 8:
        guard += 1
        if deadline is not None and time.time() > deadline and guard > 1:
            break                     # 探测限时:已有首页结果,不再翻页
        try:
            req = rules.parse_request(cur, {})
            hd = dict(headers or {})
            if req.get("headers"):
                hd.update(req["headers"])
            url, text = fetch(source.get("bookSourceName", "?"), req["url"], req["method"],
                              req.get("body", ""), hd,
                              timeout if timeout else _clamp_timeout(source),
                              retry=req.get("retry") or 0,
                              charset=req.get("charset") or "")
        except Exception as e:
            if guard == 1:
                raise RuntimeError("目录页抓取失败: %s" % e)
            break
        dom = rules.parse_dom(text)
        rules.set_page_context(base_url=url, page_text=text)
        nodes = rules.extract_list(dom, rt.get("chapterList") or "")
        added = 0
        for n in nodes:
            try:
                nm = rules.extract_value(n, rt.get("chapterName") or "text")
                cu = rules.extract_value(n, rt.get("chapterUrl") or "href")
                if not nm or not cu:
                    continue
                cu = urljoin(url, cu)
                if cu in seen:
                    continue
                seen.add(cu)
                chapters.append((nm, cu))
                added += 1
            except Exception:
                continue
        if on_progress:
            on_progress(len(chapters), 0, "")
        nxt = (rt.get("nextTocUrl") or "").strip()
        if not nxt:
            break
        try:
            nxt_url = rules.extract_value(dom, nxt)
        except Exception:
            nxt_url = ""
        if not nxt_url or nxt_url == cur or urljoin(url, nxt_url) in (toc_url, cur):
            break
        cur = urljoin(url, nxt_url)
        if stop is not None and stop.is_set():
            break
    return chapters


def _fetch_chapter(source, url, base, headers):
    req = rules.parse_request(url, {})
    hd = dict(headers or {})
    if req.get("headers"):
        hd.update(req["headers"])
    page_url, text = fetch(source.get("bookSourceName", "?"), req["url"], req["method"],
                           req.get("body", ""), hd, _clamp_timeout(source),
                           retry=req.get("retry") or 0,
                           charset=req.get("charset") or "")
    dom = rules.parse_dom(text)
    rules.set_page_context(base_url=page_url, page_text=text,
                           src_key=source.get("bookSourceName", "?"))
    parts = []
    # 多页正文
    rc = source.get("ruleContent") or {}
    if isinstance(rc, str):
        content_rule = rc
        next_rule, replace_rules = "", []
    else:
        content_rule = (rc.get("content") or "")
        next_rule = (rc.get("nextContentUrl") or "")
        replace_rules = rc.get("replaceRegex") or ""
    if not content_rule:
        raise RuntimeError("无正文规则")
    guard = 0
    cur_dom = dom
    cur_page = page_url
    while guard < 20:
        guard += 1
        raw = rules.extract_value(cur_dom, content_rule)
        if not raw:
            if guard == 1:
                raise RuntimeError("正文规则未匹配到内容")
            break
        if "<" in raw and ">" in raw:
            raw = rules.fragment_to_text(raw)
        parts.append(raw.strip())
        if not next_rule or guard >= 20:
            break
        try:
            nx = rules.extract_value(cur_dom, next_rule)
        except Exception:
            nx = ""
        if not nx:
            break
        nurl = urljoin(cur_page, nx)
        if nurl == cur_page:
            break
        cur_page, text = fetch(source.get("bookSourceName", "?"), nurl, "GET",
                               "", hd, _clamp_timeout(source))
        cur_dom = rules.parse_dom(text)
        rules.set_page_context(base_url=cur_page, page_text=text)
    body = "\n".join(p for p in parts if p)
    # replaceRegex(可能多行多条)
    if replace_rules:
        if isinstance(replace_rules, list):
            rules_list = replace_rules
        else:
            rules_list = [ln for ln in str(replace_rules).splitlines() if ln.strip()]
        for rr in rules_list:
            body = rules.apply_replaces(body, str(rr).strip())
    return body


def load_book(hit, on_progress=None, stop=None, workers=8, toc=None):
    """下载整本书。on_progress(done, total, chapter_name)。返回 dict(title/author/chapters)。

    toc:已抓好的目录 [(name, url)]。传入则可跳过目录抓取(探测书源时用)。
    """
    if stop is None:
        stop = threading.Event()
    source = hit["source"]
    title = hit["name"]
    if toc is None:
        toc = fetch_toc(source, hit["book_url"], on_progress=None, stop=stop)
    total = len(toc)
    if total == 0:
        raise RuntimeError("目录为空(可能需登录或被封)")
    chapters = [None] * total
    headers = source_headers(source)
    base = hit["book_url"]

    def one(i):
        nm, cu = toc[i]
        try:
            body = _fetch_chapter(source, cu, base, headers)
            return i, nm, body, None
        except Exception as e:
            return i, nm, "", str(e)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(one, i) for i in range(total)]
        for fut in as_completed(futs):
            i, nm, body, err = fut.result()
            chapters[i] = {"name": nm, "text": body, "error": err}
            done += 1
            if on_progress:
                try:
                    on_progress(done, total, nm)
                except Exception:
                    pass
            if stop.is_set():
                for f2 in futs:
                    f2.cancel()
                break
    if stop.is_set():
        raise RuntimeError("已取消")
    ok = sum(1 for c in chapters if c and c["text"] and not c["error"])
    return {"title": title, "author": hit.get("author", ""),
            "kind": hit.get("kind", ""), "source": source.get("bookSourceName", "?"),
            "book_url": hit["book_url"], "chapters": [c for c in chapters if c],
            "total": total, "ok": ok}

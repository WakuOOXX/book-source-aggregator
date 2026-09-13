# -*- coding: utf-8 -*-
"""core.search —— 搜索编排与相关性判定(自 app.py 剥离,零 tkinter)。

emit 接收**单个 (kind, payload) 元组**;keep 谓词(「只看相关结果」过滤)
原属 UI 的 hit 事件分支,现上移至此 —— UI 只渲染,不再做业务过滤。
"""
import difflib

from legado import engine

from core.config import SEARCH_WORKERS


# --------------------------------------------------------- 相关性判定 -------
def rel_terms(key):
    """相关性判定词:关键词 + 其模糊变体(变体重试产生的结果也算相关)。"""
    k = (key or "").strip()
    if not k:
        return []
    terms = [k] + engine.make_key_variants(k)
    return [t.lower() for t in terms if len(t) >= 2]


def relevant(h, key, domain="自动"):
    """只看相关结果:按域(自动/书名/作者/分类)判定是否保留。

    自动:书名/作者/分类/简介至少一处命中(现有行为)。
    书名/作者/分类:仅该域命中关键词或其变体时保留。
    很多小站无视搜索词、返回热门书充数,本地不过滤就会混进无关结果。
    """
    terms = rel_terms(key)
    if not terms:
        return True
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


def score_hit(h, key):
    """相关度:书名同名/含词 > 作者含词 > 分类含词 > 简介含词 > 字符相似度。"""
    k = (key or "").strip()
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
        for v in rel_terms(key):             # 变体命中书名也给分
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


# ------------------------------------------------------------- 搜索编排 ----
def search_run(srcs, key, *, emit, stop, fuzzy=True, workers=None, keep=None):
    """并发搜索并按事件上报。返回引擎全量结果列表。

    - 进度 → ("sprog", "done/total 源 · msg");
    - 每条命中 → ("hit", h):先按 (book_url, 书源名) 去重(保持引擎去重
      语义:同一URL只入一次),再过 keep 谓词(「只看相关结果」,UI 传入;
      keep=None 不过滤)—— 过滤从 UI 的 hit 事件分支上移至此,UI 只渲染;
    - 结束 → ("sres", (len(hits), fuzzy)),hits 为引擎返回的全量列表
      (含被 keep 过滤掉的;UI 以其收到的 hit 集合为准做最终重排)。
    """
    if workers is None:
        workers = SEARCH_WORKERS
    seen = set()

    def prog(done, total, msg):
        emit(("sprog", "%d/%d 源 · %s" % (done, total, msg)))

    def onhit(h):
        k = (h.get("book_url"), (h.get("source") or {}).get("bookSourceName"))
        if k in seen:
            return
        seen.add(k)
        if keep is not None and not keep(h):
            return
        emit(("hit", h))

    try:
        hits = engine.search_sources(srcs, key, on_progress=prog, stop=stop,
                                     workers=workers, on_hit=onhit, fuzzy=fuzzy)
    except Exception as e:
        emit(("log", "搜索异常: %s" % e))
        hits = []
    emit(("sres", (len(hits), fuzzy)))
    return hits

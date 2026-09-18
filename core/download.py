# -*- coding: utf-8 -*-
"""core.download —— 正文下载/导出编排(自 app.py 剥离,零 tkinter)。

dldone/dlcancel 的结构化汇总(成功/失败清单)由本模块 emit,UI 只负责
弹窗展示与标红;messagebox 等交互留在 UI 层。
emit 接收**单个 (kind, payload) 元组**。
"""
import time
import traceback
from pathlib import Path

from legado import engine, export

from core.config import DOWNLOAD_WORKERS


# ------------------------------------------------------------- 单本导出 ----
def export_one(book, out, fmt, batch, emit=None):
    """按选定格式导出。batch=True 时同名不同源自动加书源后缀,避免覆盖。

    fmt="auto"(书源能力检测):先按 EPUB 导出,失败降级 TXT ——
    load_book 产物是纯内存 book,重试零网络成本;返回 path 的扩展名
    即本书实际落盘的格式。
    """
    def _do(ext):
        suffix = ""
        if batch and (Path(out) / ("%s.%s" % (export.safe_name(book["title"]),
                                              ext))).exists():
            suffix = "_" + export.safe_name(book.get("source", ""))
        fn = export.export_txt if ext == "txt" else export.export_epub
        return fn(book, out, suffix)

    if fmt == "txt":
        return _do("txt")
    if fmt != "auto":
        return _do("epub")
    try:
        return _do("epub")
    except Exception as e:
        if emit:
            emit(("log", "↻ 《%s》EPUB 导出失败(%s),降级 TXT"
                  % (book["title"], e)))
        return _do("txt")


def try_one(h, prog, out, fmt, batch, *, emit, stop):
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
        emit(("log", "↻ 元数据已按详情页修正: [%s]《%s》" % (srcname, h["name"])))
    toc = engine.fetch_toc(h["source"], h["book_url"], stop=stop,
                           timeout=6, deadline=time.time() + 15)
    if not toc:
        raise RuntimeError("目录为空(书源被封或需登录)")
    book = engine.load_book(h, toc=toc, on_progress=prog, stop=stop,
                            workers=DOWNLOAD_WORKERS)
    if stop.is_set():
        raise RuntimeError("已取消")
    if book["ok"] == 0:
        raise RuntimeError("正文 0/%d 章成功(书源被封)" % book["total"])
    path = export_one(book, out, fmt, batch, emit=emit)
    emit(("log", "《%s》 %d/%d 章 · 源[%s] → %s" %
          (book["title"], book["ok"], book["total"], srcname, path)))
    return book, path


# ------------------------------------------------------------- 下载主循环 ---
def download_run(hits, out, mode, fmt, *, emit, stop, hit_idx=None):
    """逐本下载循环(前台编排入口,工作线程中调用)。

    hit_idx:hits 各元素在 UI self.hits 中的真实下标(用于标红失败行)。
    任何异常都必须可见(pythonw 下 stderr 不可见,否则表现为"永远停在
    准备下载"),经 dlerr 事件上报。
    """
    try:
        _download_inner(hits, out, mode, fmt, emit=emit, stop=stop,
                        hit_idx=hit_idx)
    except Exception:
        emit(("log", "✘ 下载线程异常: %s" % traceback.format_exc()[-500:]))
        emit(("dlerr", "下载线程异常,已终止: %s" % traceback.format_exc()[-200:]))


def _download_inner(hits, out, mode, fmt, *, emit, stop, hit_idx=None):
    n = len(hits)
    hit_idx = hit_idx or list(range(n))

    def prog(done, total, msg):
        emit(("dlprog", (done, total, msg)))

    ok_list, fail_list = [], []
    for bi, h in enumerate(hits):
        if stop.is_set():
            break
        srcname = h["source"].get("bookSourceName", "?")
        emit(("dlbook", (bi, n, h["name"], srcname,
                         "探测目录(超时 6s,失败自动换源)…")))
        try:
            book, path = try_one(h, prog, out, fmt, batch=(mode == "batch"),
                                 emit=emit, stop=stop)
        except Exception as e:
            emit(("log", "✘ 跳过[%s]《%s》: %s" % (srcname, h["name"], e)))
            fail_list.append((hit_idx[bi], h["name"], srcname, str(e)))
            continue              # 被封/失败 → 单一模式换下一个,合并模式继续下一本
        ok_list.append((book, path))
        emit(("dlone", (book, path, bi, n)))
        if mode == "single":
            break                 # 单一模式:只保留第一本成功的,其余丢弃
    if stop.is_set():
        emit(("dlcancel", (ok_list, fail_list)))
    else:
        emit(("dldone", (mode, fmt, ok_list, fail_list)))

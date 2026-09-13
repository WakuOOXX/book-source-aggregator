# -*- coding: utf-8 -*-
"""cli.py —— 命令行入口(逻辑/UI 剥离红利落地,零 tkinter)。

全部业务走 core 包(core.verify / core.search / core.download),本文件只做:
  argparse 参数解析 → 组装注入(emit=print 版 reporter、stop=threading.Event)
  → 调 core 的 *_run 编排 → 打印结果。
与 GUI 同一套引擎注入:set_auth(每源登录头) + set_deep(深度校验判定表),
启动时初始化一次,口径与 App.__init__/_reload_all 一致。

用法:
  python cli.py verify [--deep] [--files FILE ...] [--dir DIR] [--workers N]
  python cli.py search KEYWORD [--no-fuzzy] [--domain 自动|书名|作者|分类]
  python cli.py download KEYWORD [--fmt epub|txt] [--out DIR]
                  [--mode single|merge] [--all]
"""
import argparse
import sys
import threading
from pathlib import Path

from legado.normalize import dedupe_sources
from legado import engine

import core
from core import config as _config
from core.artifacts import is_verify_artifact, load_auth_state
from core.download import download_run
from core.search import relevant, search_run
from core.verify import load_deep_tables, verify_run


# ------------------------------------------------------------ 引擎注入 ------
def _discover_files(source_dir):
    """书源目录下的原始表清单(剔除校验产物与 auth_state.json)。"""
    if not source_dir.exists():
        return []
    return sorted(p for p in source_dir.iterdir()
                  if p.is_file() and p.suffix.lower() == ".json"
                  and not is_verify_artifact(p.name)
                  and p.name != "auth_state.json")


def _init_engine(source_dir):
    """启动注入:登录头 + 深度判定表(对应 App.__init__/_load_deep_tables 路径逻辑)。"""
    _config.SOURCE_DIR = Path(source_dir)          # CLI 可用 --dir 指向其他书源目录
    auth = load_auth_state()
    core.set_auth(auth)
    fns = [p.name for p in _discover_files(_config.SOURCE_DIR)]
    deep = load_deep_tables(fns) if fns else {}
    print("书源目录: %s" % _config.SOURCE_DIR)
    print("已注入登录头 %d 条 · 深度判定 %d 条。" % (len(auth), len(deep)))


def _merge_sources():
    """合并工作源:有 .good.json 的文件用有效表,否则用全量表(与 GUI 口径一致),
    跨文件按 (书源名, 站点URL) 去重、有效表来源优先。"""
    merged = []
    for p in _discover_files(_config.SOURCE_DIR):
        try:
            srcs = engine.load_sources(str(p))
        except Exception as e:
            print("✘ 书源文件读取失败,已跳过: %s(%s)" % (p.name, e))
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
        print("已加载 %s:%s %d 源" % (p.name, "有效表" if use_good else "全量", len(use)))
        merged.extend(use)
    srcs, n_dup = dedupe_sources(merged)
    print("合并工作源 %d 个(去重 %d)。" % (len(srcs), n_dup))
    return srcs


# ------------------------------------------------------------ print reporter -
def _cut(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def report(ev, _state={"line": False}):
    """print 版 reporter:core emit((kind, payload)) → 终端输出。

    进度类事件用 \\r 原位刷新;其余整行打印。与 app._handle_event 同一套
    事件契约(15 种),这里只挑 CLI 关心的打印,未知事件忽略。
    """
    kind, p = ev
    if kind == "log":
        print("\r" + " " * 79 + "\r" + str(p))
        _state["line"] = False
    elif kind == "vfile":
        fi, n, fn = p
        print("\r── 文件 %d/%d: %s" % (fi, n, fn))
    elif kind == "vprog":
        fi, n, fn, done, total, ok, bad = p
        print("\r  探测 %d/%d · 有效 %d · 失效 %d   " % (done, total, ok, bad),
              end="", flush=True)
    elif kind == "vfile_done":
        fn, origin, ok, bad, elapsed = p
        print("\r  ✔ %s:有效 %d · 失效 %d(耗时 %.0fs)" % (fn, ok, bad, elapsed))
    elif kind == "vdeep":
        fi, n, fn, phase, done, total = p
        print("\r  [深度·%s] %d/%d   " % (phase, done, total), end="", flush=True)
    elif kind == "vdone":
        n_files, tot_ok, tot_bad, elapsed, aborted = p
        print("\n校验完成:%d 个文件 · 有效 %d · 失效 %d · 耗时 %.0fs%s"
              % (n_files, tot_ok, tot_bad, elapsed,
                 " · 已中止" if aborted else ""))
    elif kind == "sprog":
        print("\r  搜索 %s   " % p, end="", flush=True)
    elif kind == "hit":
        pass                                    # 结束后统一打命中表(sres)
    elif kind == "sres":
        n_hits, fuzzy = p
        print("\r搜索结束:命中 %d 本%s" % (n_hits, "" if fuzzy else "(精确模式)"))
    elif kind == "dlbook":
        bi, n, name, src, msg = p
        print("\n▶ (%d/%d)《%s》[src] %s" % (bi + 1, n, name, msg))
    elif kind == "dlprog":
        done, total, msg = p
        print("\r  正文 %d/%d 章 · %s   " % (done, total, msg), end="", flush=True)
    elif kind == "dlone":
        book, path, bi, n = p
        print("\r  ✔ 《%s》 %d/%d 章 → %s" % (book["title"], book["ok"],
                                              book["total"], path))
    elif kind == "dldone":
        mode, fmt, ok_list, fail_list = p
        print("\n下载完成:成功 %d · 失败 %d" % (len(ok_list), len(fail_list)))
        for idx, name, src, err in fail_list:
            print("  ✖ 《%s》[src] %s" % (name, err))
    elif kind == "dlcancel":
        ok_list, fail_list = p
        print("\n已取消:完成前成功 %d · 失败 %d" % (len(ok_list), len(fail_list)))
    elif kind == "dlerr":
        print("✘ %s" % p)


def _hit_blocked(h):
    """命中行是否标 [BLOCKED]:深度判定表里试搜未通过(空转/失败)的源。"""
    return engine.deep_search_ok(h.get("source") or {}) is False


def print_hits(hits):
    """命中表:序号/书名/作者/分类/最新章节/书源,被封源加 [BLOCKED] 前缀。"""
    if not hits:
        print("没有命中结果。")
        return
    print("\n%-4s %-28s %-14s %-16s %-24s %s"
          % ("#", "书名", "作者", "分类", "最新章节", "书源"))
    for i, h in enumerate(hits, 1):
        tag = " [BLOCKED]" if _hit_blocked(h) else ""
        print("%-4d %-28s %-14s %-16s %-24s %s%s"
              % (i, _cut(h.get("name"), 26), _cut(h.get("author"), 12),
                 _cut(h.get("kind"), 14), _cut(h.get("last_chapter"), 22),
                 _cut((h.get("source") or {}).get("bookSourceName"), 18), tag))
    print("共 %d 条。" % len(hits))


# ------------------------------------------------------------- 后台执行 ------
def run_with_cancel(fn):
    """fn(stop) 在后台线程执行;主线程 Ctrl+C → 置 stop,等线程收尾。"""
    stop = threading.Event()
    t = threading.Thread(target=fn, args=(stop,), daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
    except KeyboardInterrupt:
        stop.set()
        print("\n⌨ 已请求取消,等待线程收尾…")
        t.join()
    return stop


# ------------------------------------------------------------- 子命令 --------
def cmd_verify(args):
    _init_engine(args.dir)
    if args.files:                              # --files:相对 shuyuan 目录或绝对路径
        files = []
        for f in args.files:
            p = Path(f)
            if not p.is_absolute():
                p = _config.SOURCE_DIR / f
            if p.exists():
                files.append((p.name, p))
            else:
                print("⚠ 校验跳过缺失文件: %s" % f)
    else:
        files = [(p.name, p) for p in _discover_files(_config.SOURCE_DIR)]
    if not files:
        print("没有可校验的书源文件(书源目录为空或文件缺失)。")
        return 0
    print("开始%s校验 %d 个书源文件(并发 %d · 超时 12s)…"
          % ("深度" if args.deep else "", len(files), args.workers))
    run_with_cancel(lambda stop: verify_run(files, emit=report, stop=stop,
                                            deep=args.deep,
                                            workers=args.workers))
    return 0


def cmd_search(args):
    _init_engine(args.dir)
    srcs = _merge_sources()
    if not srcs:
        print("没有可用书源,无法搜索。")
        return 0
    hits_box = []

    def go(stop):
        hits_box.append(search_run(srcs, args.keyword, emit=report, stop=stop,
                                   fuzzy=args.fuzzy, workers=args.workers,
                                   keep=(lambda h: relevant(h, args.keyword,
                                                            args.domain))
                                   if args.rel else None))

    run_with_cancel(go)
    hits = merge_and_dedupe(hits_box[0]) if hits_box else []
    print_hits(hits)
    return 0


def merge_and_dedupe(hits):
    """CLI 收尾重排:与 GUI 同口径(merge_hits + dedupe_hits)。"""
    from legado.normalize import merge_hits, dedupe_hits
    return merge_hits(dedupe_hits(list(hits)))


def cmd_download(args):
    _init_engine(args.dir)
    srcs = _merge_sources()
    if not srcs:
        print("没有可用书源,无法下载。")
        return 0
    hits_box = []

    def go_search(stop):
        hits_box.append(search_run(srcs, args.keyword, emit=report, stop=stop,
                                   fuzzy=True, workers=args.workers,
                                   keep=lambda h: relevant(h, args.keyword,
                                                           "自动")))

    run_with_cancel(go_search)
    hits = merge_and_dedupe(hits_box[0]) if hits_box else []
    print_hits(hits)
    if not hits:
        return 0
    if args.all:
        sel = hits
    else:
        sel = _pick_hits(hits)
        if not sel:
            print("未选择任何命中,退出。")
            return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mode = "batch" if args.mode == "merge" else "single"   # core 内部口径
    print("开始下载 %d 本 → %s(格式 %s · 方式 %s · 正文并发 %d)…"
          % (len(sel), out, args.fmt, args.mode, _config.DOWNLOAD_WORKERS))
    box = []

    def go_dl(stop):
        download_run(sel, str(out), mode, args.fmt, emit=report, stop=stop)
        box.append(1)

    run_with_cancel(go_dl)
    return 0 if box else 1


def _pick_hits(hits):
    """交互式选择:序号(逗号/空格分隔)、a=全部、q=取消、回车=1。"""
    try:
        raw = input("选择要下载的序号(逗号/空格分隔; a=全部; q=取消; 回车=1): "
                    ).strip()
    except (EOFError, KeyboardInterrupt):
        return []
    if raw.lower() == "q":
        return []
    if raw.lower() == "a" or not raw:
        return hits
    sel = []
    for tok in raw.replace(",", " ").split():
        try:
            i = int(tok)
            if 1 <= i <= len(hits):
                sel.append(hits[i - 1])
        except ValueError:
            pass
    return sel


# ------------------------------------------------------------- argparse ------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="cli.py", description="Legado 书源小说下载器 —— 命令行入口"
                                   "(业务逻辑全部走 core 包,与 GUI 同口径)。")
    ap.add_argument("--dir", default=None, metavar="DIR",
                    help="书源目录(缺省 shuyuan/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="书源校验/深度校验(写 .good/.error/.deep.json)")
    v.add_argument("--deep", action="store_true",
                   help="深度校验:活源追加试搜+分类探测")
    v.add_argument("--files", nargs="*", default=[], metavar="FILE",
                   help="只校验指定文件(缺省=目录下全部原始表)")
    v.add_argument("--workers", type=int, default=_config.VERIFY_WORKERS,
                   help="并发数(缺省 %d)" % _config.VERIFY_WORKERS)
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("search", help="并发搜索全部工作源")
    s.add_argument("keyword")
    sf = s.add_mutually_exclusive_group()
    sf.add_argument("--fuzzy", dest="fuzzy", action="store_true", default=True,
                    help="模糊搜索(缺省)")
    sf.add_argument("--no-fuzzy", dest="fuzzy", action="store_false",
                    help="精确搜索")
    s.add_argument("--domain", choices=("自动", "书名", "作者", "分类"),
                   default="自动", help="「只看相关结果」判定域(缺省 自动)")
    s.add_argument("--no-rel", dest="rel", action="store_false", default=True,
                   help="关闭相关结果过滤")
    s.add_argument("--workers", type=int, default=_config.SEARCH_WORKERS,
                   help="并发数(缺省 %d)" % _config.SEARCH_WORKERS)
    s.set_defaults(fn=cmd_search)

    d = sub.add_parser("download", help="搜索并下载(交互式选择或 --all)")
    d.add_argument("keyword")
    d.add_argument("--fmt", choices=("epub", "txt"), default="epub",
                   help="导出格式(缺省 epub)")
    d.add_argument("--out", default=str(_config.DEFAULT_OUT), metavar="DIR",
                   help="导出目录(缺省 downloads/)")
    d.add_argument("--mode", choices=("single", "merge"), default="single",
                   help="single=第一本成功即止;merge=每本都下(缺省 single)")
    d.add_argument("--all", action="store_true",
                   help="跳过交互选择,下载全部命中")
    d.add_argument("--workers", type=int, default=_config.SEARCH_WORKERS,
                   help="搜索并发数(缺省 %d)" % _config.SEARCH_WORKERS)
    d.set_defaults(fn=cmd_download)

    # --dir 双处定义:顶层(子命令前)与子命令后均可;SUPPRESS 缺省避免
    # 子解析器把顶层已解析的值覆盖回 None。
    for p in (v, s, d):
        p.add_argument("--dir", default=argparse.SUPPRESS, metavar="DIR",
                       help="书源目录(缺省 shuyuan/)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

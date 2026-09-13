# -*- coding: utf-8 -*-
"""core.verify —— 书源校验/深度校验编排(自 app.py 剥离,零 tkinter)。

线程纪律:后台线程一律 emit((kind, payload)) 上报(UI 侧 queue 消费),
绝不碰 tk 控件;取消用注入的 threading.Event(stop)。
emit 接收**单个 (kind, payload) 元组**。
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from legado import engine, fetcher

from core import config as _config
from core.config import (VERIFY_WORKERS, SEARCH_FALLBACK_REASONS, DEEP_KEYWORD)
from core.artifacts import (good_table_path, deep_table_path, deep_classify,
                            write_deep_table, _fmt_deep_summary,
                            _fmt_reason_summary, cleanup_verify_artifacts)


# ------------------------------------------------------------ 单源探测 ------
def check_one(s, *, stop, timeout=12):
    """探测单源,返回 (reason, status, elapsed_ms)。

    reason ∈ ok / timeout / connect(连不上、DNS 死)/ http_<code> / invalid_url;
    status 为最后一次 HTTP 状态码(非 HTTP 失败为 None);elapsed_ms 含重试的
    总耗时(回写 respondTime 供搜索排序);中止返回 None。
    """
    if stop.is_set():
        return None                                          # None = 中止未检测
    url = (s.get("bookSourceUrl") or "").strip()
    if not url:
        return ("invalid_url", None, 0)
    t0 = time.time()
    reason, status = ("connect", None)
    for _attempt in (0, 1):                                  # 失败重试一次
        if stop.is_set():
            return None
        try:
            hd = engine.source_headers(s)                    # 含用户登录头/Cookie
            r = fetcher.request("GET", url, headers=hd, timeout=timeout,
                                verify=False, allow_redirects=True)
            if r.status_code < 400:
                return ("ok", r.status_code, int((time.time() - t0) * 1000))
            reason, status = ("http_%d" % r.status_code, r.status_code)
        except fetcher.TIMEOUT_EXCS:
            reason, status = ("timeout", None)
        except Exception:
            reason, status = ("connect", None)
    return (reason, status, int((time.time() - t0) * 1000))


def write_error_table(origin, fn, srcs, results, rescued=0):
    """写机器可读失效记录 <原名>.error.json,返回原因分布 dict。

    面向 agent/脚本解析:_meta.reason_summary 一眼拿到失效原因分布,
    failures 逐源记录 name/url/reason/status。后缀已在校验产物后缀中,
    「清除缓存」与重校验前的清理会把它当可再生缓存带走。
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


# ---------------------------------------------------------- 深度校验阶段 ----


def deep_search_one(s, *, stop, emit, keyword=DEEP_KEYWORD):
    """试搜单源 → (reason, hits, ms);中止返回 None。reason 空串=网络存活。"""
    if stop.is_set():
        return None
    t0 = time.time()
    try:
        hits, err = engine.search_one(s, keyword)
    except Exception as e:                     # 引擎层不该抛,兜底归因
        hits, err = [], "connect"
        emit(("log", "⚠ 试搜异常(%s): %s"
              % (s.get("bookSourceName", "?"), e)))
    return (err, len(hits), int((time.time() - t0) * 1000))


def deep_explore_one(s, *, stop, emit):
    """分类探测单源 → (reason, items, ms);中止返回 None。"""
    if stop.is_set():
        return None
    t0 = time.time()
    try:
        items, err = engine.explore_first_page(s)
    except Exception as e:
        items, err = 0, "connect"
        emit(("log", "⚠ 分类探测异常(%s): %s"
              % (s.get("bookSourceName", "?"), e)))
    return (err, items, int((time.time() - t0) * 1000))


def deep_stage(fi, n_files, fn, origin, srcs, results, *, emit, stop,
               workers=None, reload_files=None):
    """对活性通过的源跑深度阶段并写 <原名>.deep.json + 日志分布摘要。

    中途停止:已测源结果照常写 deep.json(_meta.interrupted),与 good 表
    "停止不写"不同 —— 深度结果是分析产物,部分数据也有价值。
    reload_files:完成后重灌深度判定表的文件名清单(缺省=本文件)。
    """
    if workers is None:
        workers = VERIFY_WORKERS
    if reload_files is None:
        reload_files = [fn]
    alive = [s for s, r in zip(srcs, results) if r and r[0] == "ok"]
    if not alive:
        return
    pool = ThreadPoolExecutor(max_workers=workers)

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
                emit(("vdeep", (fi + 1, n_files, fn, phase, done, total)))
        return out

    try:
        emit(("vdeep", (fi + 1, n_files, fn, "试搜", 0, len(alive))))
        sres = _run_phase(lambda s: deep_search_one(s, stop=stop, emit=emit),
                          "试搜", [None] * len(alive))
        stopped = any(r is None for r in sres)
        eres = [None] * len(alive)
        if not stopped and not stop.is_set():
            emit(("vdeep", (fi + 1, n_files, fn, "分类", 0, len(alive))))
            eres = _run_phase(lambda s: deep_explore_one(s, stop=stop, emit=emit),
                              "分类", eres)
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
    interrupted = stopped or stop.is_set()
    counts = write_deep_table(origin, fn, rows, DEEP_KEYWORD,
                              interrupted=interrupted)
    emit(("vdeep", (fi + 1, n_files, fn, "完成", len(rows), len(alive))))
    emit(("log", "深度结果(%s): %s%s"
          % (fn, _fmt_deep_summary(counts),
             " · 已中止,部分源未测" if interrupted else "")))
    # 深度判定灌入引擎(搜索过滤联动):全部文件合并后统一 set_deep
    load_deep_tables(reload_files)


def load_deep_tables(fns, source_dir=None):
    """扫描清单内各文件的 .deep.json,合并灌入 engine.set_deep(搜索过滤用)。

    键 = (书源名, bookSourceUrl),与跨文件去重身份同口径;
    多文件合并去重后同一身份只留一条判定,重复源覆盖次序 = 清单顺序。
    返回判定表 dict(供 UI 缓存展示)。
    """
    table = {}
    for fn in fns:
        dp = deep_table_path((_config.SOURCE_DIR if source_dir is None
                              else source_dir) / fn)
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
    engine.set_deep(table)
    return table


# ---------------------------------------------------------- 校验主循环 ------
def verify_run(files, *, emit, stop, deep=False, workers=None,
               reload_files=None):
    """逐文件校验循环。files = [(filename, Path), ...]

    deep=True(v1.8.0 深度校验):活性校验照旧跑完全套(good/error 表
    语义不变),之后对每个活性通过的源追加试搜 + 分类探测,写
    <原名>.deep.json。全部失效的文件不进入深度阶段。
    reload_files:深度阶段结束后重灌深度判定表的文件名清单(UI 传入完整
    勾选清单;缺省 = files 里的文件名)。
    """
    if workers is None:
        workers = VERIFY_WORKERS
    if reload_files is None:
        reload_files = [fn for fn, _ in files]
    t_total = time.time()
    n_files = len(files)
    tot_ok, tot_bad = 0, 0
    aborted = False
    for fi, (fn, origin) in enumerate(files):
        if stop.is_set():
            aborted = True
            break
        emit(("vfile", (fi + 1, n_files, fn)))
        try:
            srcs = engine.load_sources(str(origin))
        except Exception as e:
            emit(("log", "✘ 文件 %s 读取失败,跳过: %s" % (fn, e)))
            continue
        if not srcs:
            emit(("log", "⚠ 文件 %s 无书源,跳过。" % fn))
            continue
        t0 = time.time()
        results, done = [], 0
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            for r in pool.map(lambda s: check_one(s, stop=stop), srcs):
                results.append(r)
                done += 1
                if done % 25 == 0 or done == len(srcs):
                    n_ok = sum(1 for x in results if x and x[0] == "ok")
                    n_bad = sum(1 for x in results if x and x[0] != "ok")
                    emit(("vprog", (fi + 1, n_files, fn,
                                    done, len(srcs), n_ok, n_bad)))
        except Exception as e:
            emit(("log", "校验异常(%s): %s" % (fn, e)))
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
            emit(("log", "文件 %s 校验中止(未检测 %d 个)" % (fn, n_untested)))
            aborted = True
            break
        # 搜索兜底救援:403/超时类源用真实搜索规则复测,搜到书即改判有效。
        n_rescued = 0
        if not stop.is_set():
            cands = [s for s, r in zip(srcs, results)
                     if r and r[0] in SEARCH_FALLBACK_REASONS]
            if cands:
                emit(("log", "搜索兜底: 对 %d 个 403/超时类源用真实搜索规则复测…"
                      % len(cands)))
                rescued = set()

                def _on_hit(h, _rescued=rescued):
                    _rescued.add(((h.get("source") or {}).get("bookSourceUrl")
                                  or "").strip())

                try:
                    engine.search_sources(cands, DEEP_KEYWORD, workers=workers,
                                          stop=stop, on_hit=_on_hit)
                except Exception as e:
                    emit(("log", "搜索兜底异常: %s" % e))
                for i, (s, r) in enumerate(zip(srcs, results)):
                    if (r and r[0] in SEARCH_FALLBACK_REASONS
                            and (s.get("bookSourceUrl") or "").strip() in rescued):
                        results[i] = ("ok", None)
                        n_rescued += 1
                if n_rescued:
                    n_ok = sum(1 for x in results if x and x[0] == "ok")
                    n_bad = sum(1 for x in results if x and x[0] != "ok")
                    emit(("log", "搜索兜底救回 %d 个(文件 %s)"
                          % (n_rescued, fn)))
        if n_ok == 0:
            # 断网保护:全部失效时清掉旧产物,避免 offline 全灭被缓存成"没有可用源"。
            cleanup_verify_artifacts(fn, origin, log=lambda m: emit(("log", m)))
            emit(("log", "⚠ 文件 %s 全部 %d 个源失效(耗时 %.0fs),已清理旧有效表。"
                  % (fn, n_bad, elapsed)))
        # 机器可读的失效记录(含原因分布)落盘,供 agent/脚本读取分析;
        # 全失效分支上面刚清过旧产物,这里写的是本次的新记录。
        try:
            summary = write_error_table(origin, fn, srcs, results,
                                        rescued=n_rescued)
            if summary:
                emit(("log", "失效原因(%s): %s"
                      % (fn, _fmt_reason_summary(summary))))
        except Exception as e:
            emit(("log", "✘ %s 失效记录写入失败: %s" % (fn, e)))
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
            emit(("log", "✘ %s 有效表写入失败: %s" % (fn, e)))
            continue
        emit(("vfile_done", (fn, str(origin), n_ok, n_bad, elapsed)))
        # 深度校验阶段:活源试搜 + 分类探测 → deep.json(全失效文件不进入)
        if deep and n_ok > 0:
            if stop.is_set():
                aborted = True
                break
            try:
                deep_stage(fi, n_files, fn, origin, srcs, results,
                           emit=emit, stop=stop, workers=workers,
                           reload_files=reload_files)
            except Exception as e:
                emit(("log", "✘ %s 深度校验异常: %s" % (fn, e)))
            if stop.is_set():      # 深度阶段中途停止:后续文件不再开始
                aborted = True
                break
    elapsed_total = time.time() - t_total
    emit(("vdone", (n_files, tot_ok, tot_bad, elapsed_total, aborted)))

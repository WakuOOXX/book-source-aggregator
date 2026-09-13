# -*- coding: utf-8 -*-
"""core.mem —— 运行记忆(sel_state.json)纯 JSON 读写(自 app.py 剥离,零 tkinter)。

tk var 的 trace 写入(_mem_save_opts)留在 UI 层,由 UI 组装 data 后调用
save_state() 落盘;键值语义见 app._mem_flush。
"""
import json


def load_state(path):
    """读运行记忆文件;无记忆/损坏 → 出厂默认 dict(与 app._mem_load 一致)。

    返回结构:
      {"multi","selected"(list[tuple]),"verify_origin","verify_done",
       "sources"(list[str] 或 None),"verify_dones",
       "fuzzy","rel","deep_only","para_tabs","fmt","mode","domain"}
    """
    from pathlib import Path
    from core.artifacts import _para_clamp
    from core.config import AUTO_PARA_TABS
    try:
        with open(path, encoding="utf-8") as f:
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
                "para_tabs": _para_clamp(d.get("para_tabs")),
                "fmt": d.get("fmt") or "epub",
                "mode": d.get("mode") or "single",
                "domain": d.get("domain") or "自动"}
    except Exception:
        # 无记忆/文件损坏:空选中 + 出厂默认选项。多选交互常开(单击仍是单选,无害)。
        return {"multi": True, "selected": [], "verify_origin": "",
                "verify_done": {}, "sources": None, "verify_dones": {},
                "fuzzy": True, "rel": True, "deep_only": False,
                "para_tabs": AUTO_PARA_TABS,
                "fmt": "epub", "mode": "single", "domain": "自动"}


def save_state(path, data):
    """全量写盘(data = UI 组装的完整 dict);异常静默(与旧 _mem_flush 一致)。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return True
    except Exception:
        return False

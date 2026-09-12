# -*- coding: utf-8 -*-
"""Treeview 资源管理器式多选控制器。

交互与主窗口搜索结果表一致(selpolicy 纯函数为判定核心):
  单击=单选 · Ctrl+单击=逐个增减 · Shift+单击=从锚点连续选 ·
  按住拖动=橡皮筋框选(普通=替换,Shift/Ctrl=追加) · Esc=取消框选 ·
  滚轮=滚动 · ↑/↓/Home/End/空格(Shift=从锚点扩展)。

用法:kit = TreeMultiSelect(tree, master, on_change=cb);on_change(set_iids)
在选中集合变化后回调。主窗口 App 自带一套同语义实现(含双击下载等业务耦合),
未迁移到本类 —— 两处共用 selpolicy 纯函数保证判定一致。
"""
import time
import tkinter as tk

from selpolicy import MIN_DRAG, apply_click, apply_range, apply_rubber


class TreeMultiSelect:

    def __init__(self, tree, master, on_change=None):
        self.tree = tree
        self.master = master
        self.on_change = on_change
        self._press = None
        self._drag = None
        self._anchor = None
        self._last_click = None
        self._rubber_top = None
        # 禁用 Treeview 类默认选择绑定,选择全部由本类维护(与主窗口同款)
        tree.bindtags((str(tree), str(master), ".", "all"))
        tree.bind("<ButtonPress-1>", self._ms_press)
        tree.bind("<B1-Motion>", self._ms_motion)
        tree.bind("<ButtonRelease-1>", self._ms_release)
        tree.bind("<MouseWheel>", self._ms_wheel)
        tree.bind("<Button-4>", lambda e: self._wheel_scroll(e, 1))
        tree.bind("<Button-5>", lambda e: self._wheel_scroll(e, -1))
        tree.bind("<KeyRelease>", self._on_key_nav)
        master.bind("<Escape>", lambda e: self._ms_escape())

    # ------------------------------------------------------------ 对外 API -
    def get(self):
        return set(self.tree.selection())

    def set(self, iids):
        """外部直接设置选中集合(全选/清空走这里),触发 on_change。"""
        sel = tuple(iids)
        self.tree.selection_set(sel)
        if sel:
            self._anchor = sel[-1]
            self.tree.see(sel[-1])
        self._notify()

    def select_all(self):
        self.set(self.tree.get_children())

    def clear_selection(self):
        self.set(())

    def _sel_apply(self, iids):
        """selection_set 只收 tuple/list(set 会被 str 化成错误项);空集=清空。"""
        self.tree.selection_set(tuple(iids))

    def _notify(self):
        if self.on_change:
            try:
                self.on_change(self.get())
            except Exception:
                pass

    # ------------------------------------------------------------- 内部机制 -
    def _row_at(self, y):
        return self.tree.identify_row(y) or None

    def _mods(self, e):
        return {"ctrl": bool(e.state & 0x0004), "shift": bool(e.state & 0x0001)}

    def _ev_xy(self, e):
        """遮罩层事件按当前框左上角换算回 tree 局部坐标。"""
        try:
            if self._rubber_top and e.widget is self._rubber_top[1] and self._drag:
                l, t = self._drag["cur"][0], self._drag["cur"][1]
                return (l + e.x, t + e.y)
        except Exception:
            pass
        return (e.x, e.y)

    def _cache_rects(self):
        """拖动开始缓存全部可见行纵向区间,拖动中命中检测零 Tcl 调用。"""
        out = []
        for iid in self.tree.get_children():
            b = self.tree.bbox(iid)
            if b and b[3] > 0:
                out.append((iid, b[1], b[1] + b[3]))
        return out

    def _hit_rows(self, rect):
        _, t, _, b = rect
        out = set()
        for iid, y1, y2 in self._drag["rects"]:
            if y2 >= t and y1 <= b:
                out.add(iid)
        return out

    # —— 半透明橡皮筋层:拖动开始创建一次,期间只改 geometry ——
    def _rubber_create(self):
        self._rubber_clear()
        top = tk.Toplevel(self.master)
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
            top.attributes("-alpha", 0.25)
        except Exception:
            pass
        top.withdraw()
        c = tk.Canvas(top, highlightthickness=0, bg="#1e80ff", bd=0)
        c.pack(fill="both", expand=True)
        c.bind("<ButtonRelease-1>", self._ms_release)
        c.bind("<B1-Motion>", self._ms_motion)
        self._rubber_top = (top, c)

    def _rubber_move(self, l, t, r, b):
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

    # ------------------------------------------------------------- 事件处理 -
    def _ms_press(self, e):
        self.tree.focus_set()
        try:
            if self.tree.identify_region(e.x, e.y) == "heading":
                return
        except Exception:
            pass
        if self._drag:
            self._rubber_clear()
            self._drag = None
        row = self._row_at(e.y)
        if row:
            self.tree.focus(row)
        m = self._mods(e)
        self._press = {"x": e.x, "y": e.y, "row": row, "t": time.time(),
                       "ctrl": m["ctrl"], "shift": m["shift"]}

    def _ms_motion(self, e):
        if not self._press:
            return
        p = self._press
        x, y = self._ev_xy(e)
        dx, dy = abs(x - p["x"]), abs(y - p["y"])
        if self._drag is None:
            if max(dx, dy) < MIN_DRAG:
                return
            self._drag = {"x1": p["x"], "y1": p["y"],
                          "sel_before": set(self.tree.selection()),
                          "append": bool(p["shift"] or p["ctrl"]),
                          "rects": self._cache_rects(),
                          "cur": (p["x"], p["y"], p["x"], p["y"]),
                          "last_ts": 0.0, "last_sel": None}
            self._rubber_create()
        now = time.time()
        if now - self._drag["last_ts"] < 0.03:      # 预览节流 ~33fps
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
        if final != self._drag["last_sel"]:
            self._drag["last_sel"] = final
            self._sel_apply(final)
            self._notify()

    def _ms_release(self, e):
        p = self._press
        self._press = None
        if not p:
            return
        x, y = self._ev_xy(e)
        if self._drag is not None:
            d = self._drag
            x1, y1 = d["x1"], d["y1"]
            x2 = (x if x is not None else x1)
            y2 = (y if y is not None else y1)
            rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            added = self._hit_rows(rect)
            if p["row"]:
                added.add(p["row"])
            final = apply_rubber(d["sel_before"], added, d["append"])
            self._rubber_clear()
            self._drag = None
            if p["row"]:
                self._anchor = p["row"]
            self._sel_apply(final)
            self._notify()
            return
        row = p.get("row") or self._row_at(y if y is not None else -1)
        now = time.time()
        plain = not (p["ctrl"] or p["shift"])
        cur = set(self.tree.selection())
        if plain:
            new = {row} if row else set()
            if row:
                self._anchor = row
        elif p["ctrl"]:
            new = apply_click(cur, True, row)
        else:
            if row and self._anchor:
                new = apply_range(self.tree.get_children(), self._anchor, row)
            else:
                new = {row} if row else cur
        if new != cur:
            self._sel_apply(new)
            self._notify()
        if plain and row:
            self._last_click = {"row": row, "x": x, "y": y, "t": now}

    def _ms_escape(self):
        if self._drag is None:
            return
        d = self._drag
        self._rubber_clear()
        self._drag = None
        self._press = None
        self._sel_apply(d["sel_before"])
        self._notify()

    def _ms_wheel(self, e):
        try:
            self.tree.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def _wheel_scroll(self, e, step):
        try:
            self.tree.yview_scroll(step, "units")
        except Exception:
            pass

    def _on_key_nav(self, e):
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
        else:
            row = rows[i] if 0 <= i < len(rows) else rows[0]
            new = apply_click(set(self.tree.selection()), True, row)
            self.tree.focus(row)
            self.tree.see(row)
            self._sel_apply(new)
            self._notify()
            return "break"
        self.tree.focus(rows[j])
        self.tree.see(rows[j])
        if e.state & 0x0001:                      # Shift:从锚点扩展
            base = self._anchor if self._anchor in rows else rows[j]
            new = apply_range(rows, base, rows[j])
            self._sel_apply(new)
            self._notify()
        elif not (e.state & 0x0004):              # 普通:单选该行
            self._sel_apply((rows[j],))
            self._anchor = rows[j]
            self._notify()
        return "break"

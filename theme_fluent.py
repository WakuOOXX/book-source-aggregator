# -*- coding: utf-8 -*-
"""Win11 Fluent 主题(UI 层专用,参照 WinUI 3 规范 + 样本/07-fluent-win11.html)。

只被 app.py 引用 —— core/ 零 tkinter 守卫不受影响。

tkinter 能力边界(与 HTML 参照的差异,均为已知限制):
- 无法真渐变/毛玻璃(Mica):主窗口用接近的纯色 BG(#f3f6fa);
- 无法圆角/阴影:全部 relief=flat + 1px 细边框(bordercolor/lightcolor/darkcolor
  三色拉平)模拟 Fluent 扁平质感,不引入任何图片/PIL;
- 半透明卡片 rgba(255,255,255,.72):tkinter 无 alpha,用纯白 #ffffff 替代。

对外接口:
- 调色板常量(RED/ACCENT/ALT_ROW/TEXT2/TEXT3/…):app.py 原硬编码颜色
  全部改为引用这里,语义映射见 docs/技术文档.md v1.9.1 修订;
- ST_ACCENT("Accent.TButton"):主操作按钮样式(校验/深度校验/搜索/下载/自动抓取);
- "Subtle.TButton":次要按钮样式(纯文字无描边,hover 显浅底,用于「打开目录」「停止」);
- TEXT_FLAT:tk.Text 扁平化参数包(无边框、白底);
- FONT_UI / FONT_UI_BOLD / FONT_LOG:全局字体(Microsoft YaHei UI 10pt,
  Win11 中文 UI 标准;日志探测 Cascadia Mono,无则回退 Consolas);
- apply(root):基于 ttk.Style("clam") 定制全套控件样式并刷主窗背景;
  所有间距对齐 4px 网格(WinUI 3 规范)。
"""
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

# ------------------------------------------------------------ 调色板 ------
# 主色板严格取自 样本/07-fluent-win11.html 的 :root 变量
BG = "#f3f6fa"            # Mica 浅底(主窗口;HTML 为渐变,tkinter 用纯色近似)
BG_DEEP = "#eef1f6"       # Mica 深一档(禁用输入底等)
CARD = "#ffffff"          # 卡片/输入面(HTML rgba(255,255,255,.72) 的 tkinter 近似)
CARD_SOFT = "#f9fbfd"     # 次级按钮底(HTML --card-solid)
BORDER = "#e4e8ee"        # 细边框/分隔线(HTML --divider)
BORDER_STRONG = "#d5d9e0" # 加重边框(HTML --stroke-strong 近似)
TEXT = "#1b1b1b"          # 正文
TEXT2 = "#616161"         # 次要文字(HTML --text-2)
TEXT3 = "#8b929e"         # 辅助文字(HTML --text-3)
DISABLED = "#a7adb8"      # 禁用态文字
ACCENT = "#0067c0"        # Fluent 强调蓝
ACCENT_HOVER = "#1975c5"  # 强调蓝 hover
ACCENT_PRESS = "#005ba1"  # 强调蓝按下
ACCENT_LIGHT = "#e6f1fa"  # 强调蓝浅底(选中行)
ACCENT_TEXT = "#0b4d8c"   # 选中行深蓝字(HTML .scan-note 文字色)
RED = "#c42b1c"           # 被封/失败标红(HTML --red,替代旧 #b00020/#cc0000)
RED_LIGHT = "#fdf3f2"     # 红浅底
GREEN = "#0f7b3d"         # 成功色(HTML --lv-ok,预留)
ALT_ROW = "#f2f6fb"       # 同书分组交替底色·浅蓝灰(替代旧 #eef4fb)
ROW_BG = "#ffffff"        # 分组交替另一底(纯白)
HEAD_BG = "#f3f6fa"       # Treeview 表头浅灰底
SEL_ROW = "#e6f1fa"       # 选中行(HTML --accent-light)
RUBBER = "#0067c0"        # 橡皮筋框选遮罩(替代旧 #1e80ff)
SCROLL = "#a9b0ba"        # 滚动条滑块(加深确保在浅底上可见;HTML --webkit-scrollbar-thumb)
SCROLL_HOVER = "#7b828d"  # 滚动条滑块 hover
SELECT_BG = "#cfe3f5"     # 文本选区(HTML ::selection)

# ------------------------------------------------------------ 字体 --------
FONT_UI = ("Microsoft YaHei UI", 10)   # 全局 UI 字体(Win11 中文 UI 标准,比 Segoe UI 9pt 中文更清晰)
FONT_UI_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_LOG_FAMILY = "Consolas"          # 日志等宽(apply 时探测 Cascadia Mono)
FONT_LOG = ("Consolas", 10)

# ------------------------------------------------------------ 样式名 ------
ST_ACCENT = "Accent.TButton"          # 主操作按钮(强调色填充白字)
ST_SUBTLE = "Subtle.TButton"          # 次要按钮(纯文字无描边,hover 浅底)

# tk.Text 扁平化参数包(运行日志 / Cookie 输入框:无边框、白底、扁平)
TEXT_FLAT = {"bg": CARD, "fg": TEXT, "relief": "flat", "bd": 0,
             "highlightthickness": 0, "insertbackground": TEXT,
             "selectbackground": SELECT_BG, "selectforeground": TEXT}


def apply(root):
    """把 Win11 Fluent 皮肤套到 root 及其全部 ttk 控件上(clam 基座)。"""
    global FONT_LOG_FAMILY, FONT_LOG
    style = ttk.Style(root)
    style.theme_use("clam")

    # 日志字体探测:Cascadia Mono(Win11 终端同款)优先,回退 Consolas
    try:
        fams = set(tkfont.families(root))
        if "Cascadia Mono" in fams:
            FONT_LOG_FAMILY, FONT_LOG = "Cascadia Mono", ("Cascadia Mono", 10)
        elif "Cascadia Code" in fams:
            FONT_LOG_FAMILY, FONT_LOG = "Cascadia Code", ("Cascadia Code", 10)
    except Exception:
        pass

    # —— 主窗口:Mica 浅底(tkinter 无法渐变,用纯色近似) ——
    root.configure(background=BG)

    # —— 基座与容器 ——
    style.configure(".", background=BG, foreground=TEXT, font=FONT_UI,
                    bordercolor=BORDER, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    # LabelFrame(「运行日志」/登录头详情区):白卡质感 → 细边框扁平
    style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, relief="flat",
                    borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=TEXT,
                    font=FONT_UI_BOLD)
    style.configure("TSeparator", background=BORDER)

    # —— 按钮:次操作 = 白底细边框,hover 变色(HTML .btn) ——
    style.configure("TButton", background=CARD_SOFT, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, relief="flat", borderwidth=1,
                    focusthickness=0, focuscolor=ACCENT, padding=(12, 4),
                    font=FONT_UI)
    style.map("TButton",
              background=[("disabled", "#eef1f5"), ("pressed", "#e8ecf1"),
                          ("active", "#f2f5f9")],
              foreground=[("disabled", DISABLED), ("active", ACCENT),
                          ("pressed", TEXT2)],
              bordercolor=[("active", BORDER_STRONG), ("disabled", BORDER)],
              lightcolor=[("active", BORDER_STRONG)],
              darkcolor=[("active", BORDER_STRONG)])
    # 主操作按钮:强调色填充白字(HTML .btn.primary)
    style.configure(ST_ACCENT, background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    padding=(12, 4), font=FONT_UI)
    # 次要按钮:纯文字无描边,hover 显浅底(「打开目录」「停止」等)
    style.configure("Subtle.TButton", background=BG, foreground=TEXT,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG,
                    relief="flat", borderwidth=0, padding=(8, 4),
                    font=FONT_UI)
    style.map("Subtle.TButton",
              background=[("active", ACCENT_LIGHT), ("pressed", "#dce8f4")],
              foreground=[("active", ACCENT), ("pressed", ACCENT_PRESS)],
              bordercolor=[("active", ACCENT_LIGHT)])
    style.map(ST_ACCENT,
              background=[("disabled", "#bfd9f2"), ("pressed", ACCENT_PRESS),
                          ("active", ACCENT_HOVER)],
              foreground=[("disabled", "#f0f6fc")],
              bordercolor=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESS)],
              lightcolor=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESS)],
              darkcolor=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESS)])

    # —— 输入框:白底 + #e4e8ee 细边框,聚焦 #0067c0(HTML input:focus) ——
    style.configure("TEntry", fieldbackground=CARD, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    insertcolor=TEXT, padding=(8, 4),
                    selectbackground=SELECT_BG, selectforeground=TEXT)
    style.map("TEntry",
              fieldbackground=[("disabled", BG_DEEP)],
              foreground=[("disabled", DISABLED)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])
    style.configure("TSpinbox", fieldbackground=CARD, background=CARD,
                    foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, arrowcolor=TEXT2, insertcolor=TEXT,
                    padding=(8, 4))
    style.map("TSpinbox",
              fieldbackground=[("disabled", BG_DEEP)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)],
              arrowcolor=[("active", ACCENT), ("disabled", DISABLED)])
    style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                    foreground=TEXT, arrowcolor=TEXT2, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(8, 4))
    style.map("TCombobox",
              fieldbackground=[("readonly", CARD), ("disabled", BG_DEEP)],
              foreground=[("disabled", DISABLED)],
              background=[("active", ACCENT_LIGHT), ("pressed", ACCENT_LIGHT)],
              arrowcolor=[("pressed", ACCENT), ("active", ACCENT),
                          ("disabled", DISABLED)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])
    # Combobox 弹出列表(popdown 是经典 tk 控件,走 option 数据库)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SEL_ROW)
    root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TEXT)
    root.option_add("*TCombobox*Listbox.relief", "flat")
    root.option_add("*TCombobox*Listbox.borderWidth", 1)

    # —— Checkbutton/Radiobutton:Fluent 蓝选中色(accent-color) ——
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=BG, foreground=TEXT, focuscolor=BG,
                        indicatorbackground=CARD, indicatorforeground=ACCENT,
                        indicatormargin=(0, 0, 0, 0), padding=1)
        style.map(name,
                  background=[("active", BG)],
                  indicatorbackground=[("pressed", ACCENT_LIGHT),
                                       ("selected", ACCENT),
                                       ("alternate", ACCENT)],
                  indicatorforeground=[("selected", "#ffffff"),
                                       ("alternate", "#ffffff"),
                                       ("active", "#ffffff")])

    # —— 结果表 Treeview:白底、浅灰表头、放宽行高、选中 #e6f1fa+深蓝字 ——
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, bordercolor=BORDER, borderwidth=1,
                    relief="flat", rowheight=28, font=FONT_UI)
    style.configure("Treeview.Heading", background=HEAD_BG, foreground=TEXT2,
                    relief="flat", borderwidth=0, bordercolor=HEAD_BG,
                    lightcolor=HEAD_BG, darkcolor=HEAD_BG, padding=(8, 6),
                    font=FONT_UI_BOLD)
    style.map("Treeview",
              background=[("selected", SEL_ROW)],
              foreground=[("selected", ACCENT_TEXT)])
    style.map("Treeview.Heading",
              background=[("active", "#eaf0f7")],
              foreground=[("active", ACCENT)])

    # —— 滚动条:Win11 细型但保证可见 ——
    #   clam 的竖向滚动条厚度由 arrowsize 决定(14→约15px);滑块用中灰、
    #   槽改纯白,确保在近白窗口上不"隐形"。
    style.configure("TScrollbar", background=SCROLL, troughcolor=CARD,
                    bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
                    relief="flat", borderwidth=0, arrowsize=14, arm=0)
    style.map("TScrollbar",
              background=[("pressed", SCROLL_HOVER), ("active", SCROLL_HOVER),
                          ("disabled", "#d6dae0")],
              troughcolor=[("disabled", BG)],
              arrowcolor=[("active", TEXT), ("pressed", TEXT),
                          ("disabled", DISABLED)])

    # —— 进度条:强调蓝填充 ——
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=BORDER, bordercolor=BG, lightcolor=ACCENT,
                    darkcolor=ACCENT, borderwidth=0)

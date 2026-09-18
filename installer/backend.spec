# -*- coding: utf-8 -*-
# 把 server.py 冻结为单文件 console 后端 bookdl-backend.exe。
# BackendClient 优先探测 exe 同目录的 bookdl-backend.exe (安装包形态), 找到即用, 无需 Python。
#
# 构建 (仓库根, Python 3.12 venv — 3.14 与 py_mini_racer/PyInstaller 兼容风险):
#   .build-venv\Scripts\pyinstaller.exe installer\backend.spec --noconfirm ^
#       --distpath installer\dist --workpath installer\build
# 产物: installer\dist\bookdl-backend.exe
#
# py_mini_racer 必须 collect_all: 它携带 V8 平台 DLL 资源, 靠静态分析会漏,
# 漏了不报错、只在运行时静默降级 (JS 规则引擎不可用, hello 的 js=false)。
# 注意 pip 包名是 mini-racer (bpcreech/PyMiniRacer 0.9+, 模块名 py_mini_racer);
# 老的 py-mini-racer 0.6.0 缺 JSEvalException/timeout_sec, 探测不通过。
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

mr_datas, mr_binaries, mr_hidden = collect_all("py_mini_racer")

hiddenimports = [
    # 仓库内模块: server.py 静态 import 大多能被分析到, 列全是为了防 try/except 守卫式导入被漏。
    "cdp_cookie",
    "core.artifacts",
    "core.auth_manager",
    "core.config",
    "core.download",
    "core.mem",
    "core.search",
    "core.verify",
    "legado.cookie_store",
    "legado.engine",
    "legado.export",
    "legado.fetcher",
    "legado.jsengine",
    "legado.normalize",
    "legado.rules",
    # 第三方守卫式依赖 (缺了会静默降级, 打包环境必须带全):
    "requests",
    "curl_cffi",
    "websocket",
    "bs4",
] + mr_hidden

a = Analysis(
    [os.path.join(ROOT, "server.py")],
    pathex=[ROOT],
    binaries=mr_binaries,
    datas=mr_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bookdl-backend",
    debug=False,
    strip=False,
    upx=False,  # UPX 压缩过的 onefile 是杀软误报重灾区, 宁可大一点。
    console=True,  # 前端用 CreateNoWindow 拉起, 用户看不到窗口。
)

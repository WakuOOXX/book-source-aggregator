# -*- coding: utf-8 -*-
"""core —— 业务逻辑层(自 app.py 剥离,零 tkinter)。

模块:
  config    业务常量与运行目录
  artifacts 纯逻辑函数(产物路径/深度分级/格式化/auth_state/缓存清理)
  verify    书源校验/深度校验编排(verify_run)
  search    搜索编排与相关性判定(search_run)
  download  正文下载/导出编排(download_run)
  mem       运行记忆 sel_state.json 读写

事件契约:core → UI 一律 emit((kind, payload)) 单元组,kind/payload 结构
与旧 app 自queue版本逐字一致(见 tests/test_event_contract.py)。
引擎注入:engine 的 auth/deep 状态经 set_auth/set_deep 显式注入,
App 启动时调用一次,core 不依赖任何 App 属性。
"""
from legado import engine


def set_auth(auth):
    """注入每源登录头(校验/搜索/详情/目录/正文全链路生效)。"""
    engine.set_auth(auth)


def set_deep(table):
    """注入深度校验判定表(搜索预筛过滤用)。"""
    engine.set_deep(table)

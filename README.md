<div align="center">

# 📚 书源聚合下载器

Windows 桌面应用 · WinUI 3 界面 + Python 后端 · 自研 Legado 规则引擎

[![release](https://img.shields.io/github/v/release/WakuOOXX/book-source-aggregator)](https://github.com/WakuOOXX/book-source-aggregator/releases/latest)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](#)
[![platform](https://img.shields.io/badge/platform-Windows%2011-lightgrey)](#)

[🌐 项目官网](https://WakuOOXX.github.io/book-source-aggregator/) ·
[⬇️ 下载 Windows 版](https://github.com/WakuOOXX/book-source-aggregator/releases/latest) ·
[📖 技术文档](docs/技术文档.md)

</div>

---

## 目录

- [功能特性](#-功能特性)
- [快速开始](#-快速开始)
- [使用指南](#-使用指南)
- [配置与数据文件](#-配置与数据文件)
- [常见问题](#-常见问题)
- [二次开发](#-二次开发)
- [免责声明](#-免责声明)

## ✨ 功能特性

搜索：

- 一次并发扫全部有效书源，结果边搜边上屏，不用等跑完。
- 直搜无果自动生成关键词变体重试；「只看相关」过滤掉无视搜索词返回热门书充数的小站。
- 含 JS 的书源已解锁（内置 V8 引擎执行 Legado 的 `java.*` 脚本），登录态书源自动跳过不中断。

选书与下载：

- 点选 / Ctrl 加选 / Shift 连选 / 空白处拖动框选；另有全选 / 反选 / 一键清空；重开程序自动恢复上次选中。
- 两种模式：**万里挑一**（选中的书依次换源试下，第一本成功即停）/ **全部下载**（每本各存一个文件）。
- 导出格式 **自动**（优先 EPUB，书源不支持自动降级 TXT）/ EPUB / TXT。
- 正文逐章抓取，目录分页拼接；重名书自动加书源后缀防覆盖；「下载」页统一管理已下载的书。

书源管理：

- 多书源文件合并去重；384 并发连通性体检生成有效表；深度校验（真实试搜 + 分类探测）给每个源定质量等级。
- 登录头抓取：CDP 驱动 Chrome 逐站登录，Cookie / 请求头自动注入对应书源。

## 🚀 快速开始

### 安装使用（推荐）

1. 到 [Releases](https://github.com/WakuOOXX/book-source-aggregator/releases/latest) 下载 `book-source-aggregator-v1.31-windows-x64.zip`；
2. 解压后运行里面的 `书源聚合下载器-Setup-1.31.exe`，安装时可自选目录；
3. 首次启动自动播种一份内置书源清单，直接搜书即可。

安装包自带全部运行环境（WinUI 前端 + 冻结的 Python 后端），目标机器不需要装 Python。

### 源码运行

```bash
git clone https://github.com/WakuOOXX/book-source-aggregator.git
cd book-source-aggregator
pip install requests beautifulsoup4 lxml websocket-client curl_cffi
pip install mini-racer        # 可选: JS 引擎, 解锁含 JS 的书源
python server.py              # JSONL 后端 (供 WinUI 前端拉起)
python cli.py                 # 或直接用命令行版
```

WinUI 3 前端在 `frontends/winui/`，需要 .NET 8 SDK + Windows 11：

```bash
dotnet build frontends/winui/NovelDownloader.sln -c Debug -p:Platform=x64
dotnet test  frontends/winui/NovelDownloader.sln -c Debug
```

## 📖 使用指南

点标题栏左上角 ☰，从左侧滑出毛玻璃导航面板：搜索 / 书源 / 下载 / 登录头 / 日志与设置；再点面板顶部的 ☰ 收起。

### 搜索（搜索页）

输入书名或作者回车。书源分组下拉可限定搜索范围（先用书源页校验出的「可用」分组）；域下拉默认「自动」，书名/作者/分类/简介任一命中即保留。结果表五列：书名 / 作者 / 分类 / 最新章节 / 书源。

### 书源（书源页）

管理书源 JSON 文件：导入的文件会复制进程序数据目录，加入清单即参与搜索（多文件自动合并去重）。「校验书源」对每个文件做连通性体检并生成 `<文件名>.good.json` 有效表；「深度校验」再对活源真实试搜 + 探测分类页，产出质量等级（完整可用 > 可搜 > 搜索空转 > 试搜失败），搜索页可勾选「只搜试搜通过源」。

### 下载（下载页）

搜索页勾选书后选格式与模式点「下载选中」。完成后到「下载」页统一管理：双击打开文件，或从列表删除；「打开下载目录」直达输出文件夹。

### 登录头（登录头页）

需要登录的书源在这里抓 Cookie：程序驱动 Chrome 打开各站登录页，你正常登录，Cookie 与请求头自动抓回并按站点存档。

### 日志与设置（设置页）

全局日志在这里（所有页面的操作日志统一汇总）。设置卡片：主题切换、数据存储目录（改目录会自动迁移书源/下载/登录头并清理旧目录残留）、「清除缓存」与「清除数据」两个按钮职责不同：

| | 清除缓存 | 清除数据 |
|---|---|---|
| 校验产物 `*.good/error/deep.json` | ✔ 删 | ✔ 删 |
| 校验记录 / 选项记忆 | ✔ 清 | ✔ 清 |
| 登录头（Cookie / 请求头 / cookies.db） | 保留 | ✔ 删 |
| 书源文件（内置 + 导入） | 保留 | ✔ 删（清完恢复出厂内置书源） |
| 已下载的书 | 保留 | 保留 |

## ⚙️ 配置与数据文件

| 位置 | 说明 |
|---|---|
| `%LOCALAPPDATA%\BookSourceAggregator\` | 数据根目录（安装版默认；设置页可改，改时自动迁移） |
| `…\shuyuan\` | 书源 JSON 及校验产物；首次启动从安装包 `seed\bookSource.json` 播种 |
| `…\downloads\` | 下载输出目录，「下载」页展示的就是这里 |
| `…\sel_state.json` | 运行状态：选中书目 / 文件清单 / 校验记录 / 选项记忆 |
| `…\auth_profile\`、`cookies.db`、`auth_state.json` | 登录头抓取档案 |
| `%LOCALAPPDATA%\NovelDownloader\settings.json` | 界面设置（主题、首启引导、数据目录覆盖） |

## ❓ 常见问题

**搜出来 0 条？** 先到书源页校验/深度校验，勾「只搜试搜通过源」再搜。书源死亡率高是所有聚合下载器的共同处境，多校验、留活源。

**下载时大量 `[抓取失败]`？** 站点限流或章节页反爬。等几分钟重试，或换「全部下载」让每本多源各试一次。

**提示「目录为空」？** 该源需要登录或已改版，程序自动换下一候选。含 JS 的源需装 mini-racer（安装包版已内置）；需登录的源去登录头页抓 Cookie。

**杀毒软件报毒？** PyInstaller 单文件 exe 偶发误报，装到别的目录或加白名单即可；介意可源码运行。

## 🧩 二次开发

- 架构：WinUI 3 前端 spawn `server.py`（JSONL 协议，stdin 命令 / stdout 事件流），业务逻辑在 `core/`（搜索/校验/下载/登录头/清理）与 `legado/`（规则引擎），细节见[技术文档](docs/技术文档.md)。
- 规则 DSL 兼容矩阵、JSONL 全命令表、事件契约（15 种 kind）都在技术文档；事件契约由 C# 侧用例双向把守。
- 测试：`dotnet test frontends/winui/NovelDownloader.sln`。

打安装包（需 .NET 8 SDK、Python 3.12 venv、Inno Setup 6）：

```bash
py -3.12 -m venv .build-venv
.build-venv\Scripts\pip install requests curl_cffi websocket-client beautifulsoup4 mini-racer pyinstaller
build-installer.bat        # 产物: dist\书源聚合下载器-Setup-<版本>.exe
```

## ⚠️ 免责声明

本项目仅用于学习与研究网络请求与规则解析技术。安装包附带的书源清单来自公开网络共享，不保证可用性，作者不对任何源的内容负责。请尊重作品版权：仅供个人试读，下载内容请于 24 小时内删除；长期阅读请支持正版（起点、晋江等官方平台）。

## 📄 License

[MIT](LICENSE) © 2026 WakuOOXX

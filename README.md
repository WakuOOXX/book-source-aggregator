<div align="center">

# 书源聚合下载器

Windows 桌面小说下载器：WinUI 3 界面 + Python 后端，自带一套 Legado 规则引擎。

[![release](https://img.shields.io/github/v/release/WakuOOXX/book-source-aggregator)](https://github.com/WakuOOXX/book-source-aggregator/releases/latest)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12-blue)](#)
[![platform](https://img.shields.io/badge/platform-Windows%2011-lightgrey)](#)

[项目官网](https://WakuOOXX.github.io/book-source-aggregator/) · [下载](https://github.com/WakuOOXX/book-source-aggregator/releases/latest) · [技术文档](docs/技术文档.md)

</div>

拿一堆 Legado 书源 JSON，一次并发搜全部源，挑中意的书逐章抓成正经的 EPUB / TXT 存本地。界面是全原生 WinUI 3，后端是 Python，装好即用不需要自己配环境。

---

## 能做什么

搜索时一次并发扫遍所有有效书源，结果边出边上屏，不用干等。直搜没结果会自动换关键词变体重试；「只看相关」这个开关能挡住那些无视搜索词、甩一堆热门书凑数的小站。带 JS 的书源也能搜（内置 V8 跑 Legado 的 `java.*` 脚本），要登录的源会自动跳过，不会卡住整轮。

选书的体验和资源管理器一致：点选、Ctrl 加选、Shift 连选，在空白处按住拖还能出绿框框选，也有全选 / 反选 / 清空，重开程序记得你上次勾了哪些。下载分两种模式，「万里挑一」是勾的书依次换源试下、第一本成功就停，「全部下载」则每本各存一份。格式选「自动」的话优先 EPUB、源不支持就降 TXT，也可以手动指定。正文一章章抓，目录分页拼全，重名的书自动带书源后缀，不会互相覆盖。

书源这边，多个 JSON 文件会自动合并去重，384 并发做连通性体检生成有效表；深度校验会真发一次搜索、再探一遍分类页，给每个源打质量等级。源按 Legado 顶层分组名自动归组，搜索时可以只搜某个分组。碰到要登录的源，程序会驱动 Chrome 逐站打开登录页，你正常登录，Cookie 和请求头自动抓回、注入对应书源。

## 装来用

到 [Releases](https://github.com/WakuOOXX/book-source-aggregator/releases/latest) 下 `BookSourceAggregator-Setup-1.31.exe`，双击装，目录随便选。

首次启动会播种一份内置书源清单，直接搜书就行；也可以去「书源」页放自己的 Legado JSON。安装包把 WinUI 前端、冻结的 Python 后端和 JS 引擎全打包了，目标机器不用装 Python 或 .NET。卸载只删程序本身，不动书源、下载的书和登录头。

想从源码跑：

```bash
git clone https://github.com/WakuOOXX/book-source-aggregator.git
cd book-source-aggregator
pip install requests beautifulsoup4 lxml websocket-client curl_cffi
pip install mini-racer        # 可选, 装了就解锁带 JS 的书源
python server.py              # JSONL 后端, 正常由 WinUI 前端自动拉起
python cli.py                 # 或直接用命令行版
```

WinUI 3 前端在 `frontends/winui/`，要 .NET 8 SDK 和 Windows 11：

```bash
dotnet build frontends/winui/NovelDownloader.sln -c Debug -p:Platform=x64
dotnet test  frontends/winui/NovelDownloader.sln -c Debug
```

## 怎么用

界面导航收在标题栏左上角那个 ☰ 里，点了从左边滑出毛玻璃面板：搜索 / 书源 / 下载 / 登录头 / 日志与设置，再点一次收起。运行日志统一在窗口底部，可折叠。

输入书名或作者回车就是搜索。书源分组下拉能限定只搜某组（先用书源页校验出「可用」组），域下拉默认「自动」，书名/作者/分类/简介任一项命中就留。结果表五列：书名 / 作者 / 分类 / 最新章节 / 书源。

「书源」页管书源 JSON 文件。导入的文件会复制进程序数据目录，进清单就参与搜索。校验书源对每个文件做连通性体检，生成 `<文件名>.good.json` 有效表；深度校验再对活源试搜一次、探分类页，打出完整可用 > 可搜 > 搜索空转 > 试搜失败的等级，搜索页可勾「只搜试搜通过源」。

搜完勾好书，选格式和模式点下载就完了，成品在「下载」页统一管理，按下载时间倒序，双击打开、列表删除、直达目录。要登录的源去「登录头」页抓 Cookie，其余上面讲过。

最后一页是「日志与设置」：主题切换、数据存储目录（换目录会自动把书源/下载/登录头搬过去并清理旧残留），还有两个别搞混的按钮：

| | 清除缓存 | 清除数据 |
|---|---|---|
| 校验产物 `*.good/error/deep.json` | 删 | 删 |
| 校验记录 / 选项记忆 | 清 | 清 |
| 登录头（Cookie / 请求头 / cookies.db） | 不动 | 删 |
| 书源文件（内置 + 导入） | 不动 | 删（清完恢复出厂内置书源） |
| 已下载的书 | 不动 | 不动 |

## 数据都在哪

| 位置 | 说明 |
|---|---|
| `%LOCALAPPDATA%\BookSourceAggregator\` | 数据根目录（安装版默认，设置页可改） |
| `…\shuyuan\` | 书源 JSON 和校验产物，首次启动从 `seed\bookSource.json` 播种 |
| `…\downloads\` | 下载输出，「下载」页看的就是这里 |
| `…\sel_state.json` | 运行状态：选中书目 / 文件清单 / 校验记录 / 选项 |
| `…\auth_profile\`、`cookies.db`、`auth_state.json` | 登录头抓取的存档 |
| `%LOCALAPPDATA%\NovelDownloader\settings.json` | 界面设置：主题、首启引导、数据目录覆盖 |

## 遇到问题

**搜出来 0 条？** 先去书源页做校验/深度校验，勾上「只搜试搜通过源」再搜。书源死亡率高是所有聚合下载器的通病，多校验、只留活源。

**下载时一堆 `[抓取失败]`？** 站点限流或章节页反爬。等几分钟重试，或换「全部下载」让每本多源各试一次。

**提示「目录为空」？** 这个源要么要登录要么改版了，程序自动换下一个。带 JS 的源得装 mini-racer（安装包已内置），要登录的去登录头页抓 Cookie。

**杀软报毒？** PyInstaller 单文件 exe 偶发误报，换个目录装或加白名单，实在介意就源码跑。

## 免责声明

本项目只用于学习和研究网络请求与规则解析技术。随包附的书源清单来自公开网络共享，不保证能用，作者不对任何源的内容负责。请尊重版权：仅供个人试读，下载内容请于 24 小时内删除，长期阅读请去起点、晋江等官方平台支持正版。

## 许可

[Apache License 2.0](LICENSE)。

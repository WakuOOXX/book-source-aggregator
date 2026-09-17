using System.Text.Json;
using System.Text.Json.Serialization;

namespace NovelDownloader.Services.Backend;

// =====================================================================================
//  后端事件契约 (M0: 只做类型定义与 JSON 反序列化, 不接进程)
//
//  线协议 (见 server.py 头部与计划 §8):
//    后端 → stdout, 每行一个 JSON 对象:
//      {"type":"event","kind":<kind>,"payload":<payload>}   ← core 的 15 种事件原样平移
//      {"type":"hello", ...} / {"type":"ack", ...} / {"type":"busy", ...} / {"type":"error", ...}
//    payload 为元组(JSON 数组)或对象; 字段名与 core 结构逐字同名 (snake_case)。
//
//  15 种 kind 与 payload 形状 (源自 cli.py report() 与 core/{search,verify,download}.py 的 emit):
//    log        string
//    sprog      string
//    hit        object {source,name,author,kind,book_url,last_chapter,intro}
//    sres       [n_hits, fuzzy]
//    vfile      [file_index, file_count, file_name]                        (1-based)
//    vprog      [file_index, file_count, file_name, done, total, ok, bad]  (1-based)
//    vfile_done [file_name, origin, ok, bad, elapsed_seconds]
//    vdeep      [file_index, file_count, file_name, phase, done, total]    (1-based)
//    vdone      [file_count, total_ok, total_bad, elapsed_seconds, aborted]
//    dlbook     [book_index, book_count, name, source, message]            (0-based)
//    dlprog     [done, total, message]
//    dlone      [book, path, book_index, book_count]                       (0-based)
//    dldone     [mode, fmt, ok_list, fail_list]
//    dlcancel   [ok_list, fail_list]
//    dlerr      string
// =====================================================================================

/// <summary>所有后端 core 事件的基类。Kind 为 §4.1 事件映射表里的 15 种之一。</summary>
public abstract record BackendEvent(string Kind);

/// <summary>
/// 搜索结果命中项 (hit 事件的 payload)。字段名与 legado.engine.search_sources 产出逐字一致。
/// </summary>
public sealed record HitDto
{
    [JsonPropertyName("name")] public string Name { get; init; } = "";
    [JsonPropertyName("author")] public string Author { get; init; } = "";
    [JsonPropertyName("kind")] public string Kind { get; init; } = "";
    [JsonPropertyName("book_url")] public string BookUrl { get; init; } = "";
    [JsonPropertyName("last_chapter")] public string LastChapter { get; init; } = "";
    [JsonPropertyName("intro")] public string Intro { get; init; } = "";

    /// <summary>
    /// 是否被封源 (深度判定表里试搜未通过)。
    /// 注意: 截至 M1, server.py 的 hit payload **不含** blocked 字段
    /// (blocked 由 Python 侧 engine.deep_search_ok 才能判定, 见 cli._hit_blocked),
    /// 因此该字段缺省 false —— 卡片右侧 InfoBadge 在 M1 恒不显示。
    /// 待后端在 hit payload 里补 "blocked" (或前端自带深度表) 后自动生效, 无需改 XAML。
    /// </summary>
    [JsonPropertyName("blocked")] public bool Blocked { get; init; }

    /// <summary>完整书源对象 (原样透传)。</summary>
    [JsonPropertyName("source")] public JsonElement Source { get; init; }

    /// <summary>书源名便捷读取 (source.bookSourceName)。</summary>
    public string SourceName
        => Source.ValueKind == JsonValueKind.Object
           && Source.TryGetProperty("bookSourceName", out var v)
           && v.ValueKind == JsonValueKind.String
            ? (v.GetString() ?? "")
            : "";
}

/// <summary>dlone / dldone 的 book 摘要 (server 已剔除 chapters 大字段)。</summary>
public sealed record BookSummaryDto
{
    [JsonPropertyName("title")] public string Title { get; init; } = "";
    [JsonPropertyName("author")] public string Author { get; init; } = "";
    [JsonPropertyName("kind")] public string Kind { get; init; } = "";
    [JsonPropertyName("source")] public string Source { get; init; } = "";
    [JsonPropertyName("book_url")] public string BookUrl { get; init; } = "";
    [JsonPropertyName("total")] public int Total { get; init; }
    [JsonPropertyName("ok")] public int Ok { get; init; }

    /// <summary>其余未显式声明的字段原样保留, 便于 M2 扩展 (不含 chapters)。</summary>
    [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; init; }
}

/// <summary>dldone/dlcancel 的 ok_list 单项: [book, path]。</summary>
public sealed record DownloadOkItemDto(BookSummaryDto Book, string Path);

/// <summary>dldone/dlcancel 的 fail_list 单项: [hit_index, name, source, error]。</summary>
public sealed record DownloadFailItemDto(int Index, string Name, string Source, string Error);

// ---------------------------------- 15 种事件 ----------------------------------

/// <summary>log —— 一行日志 (任何级别, 前端按前缀着色)。</summary>
public sealed record LogEvent(string Message) : BackendEvent("log");

/// <summary>sprog —— 搜索进度文本, 如 "3/10 源 · 抓取中"。</summary>
public sealed record SearchProgressEvent(string Text) : BackendEvent("sprog");

/// <summary>hit —— 单条命中 (增量上屏, 边搜边显示)。</summary>
public sealed record HitEvent(HitDto Hit) : BackendEvent("hit");

/// <summary>sres —— 搜索结束: 命中数以引擎返回为准, fuzzy 表示是否模糊模式。</summary>
public sealed record SearchResultEvent(int HitCount, bool Fuzzy) : BackendEvent("sres");

/// <summary>vfile —— 开始校验某文件。file_index 为 1-based。</summary>
public sealed record VerifyFileEvent(int FileIndex, int FileCount, string FileName) : BackendEvent("vfile");

/// <summary>vprog —— 文件内校验进度 (每 25 个源上报一次)。</summary>
public sealed record VerifyProgressEvent(
    int FileIndex, int FileCount, string FileName,
    int Done, int Total, int OkCount, int BadCount) : BackendEvent("vprog");

/// <summary>vfile_done —— 单个文件校验完成。</summary>
public sealed record VerifyFileDoneEvent(
    string FileName, string Origin, int OkCount, int BadCount, double ElapsedSeconds)
    : BackendEvent("vfile_done");

/// <summary>vdeep —— 深度校验阶段 (试搜 → 分类)。</summary>
public sealed record VerifyDeepEvent(
    int FileIndex, int FileCount, string FileName, string Phase, int Done, int Total)
    : BackendEvent("vdeep");

/// <summary>vdone —— 全部校验完成汇总。</summary>
public sealed record VerifyDoneEvent(
    int FileCount, int TotalOk, int TotalBad, double ElapsedSeconds, bool Aborted)
    : BackendEvent("vdone");

/// <summary>dlbook —— 开始下载某本书 (book_index 为 0-based)。</summary>
public sealed record DownloadBookEvent(
    int BookIndex, int BookCount, string Name, string Source, string Message)
    : BackendEvent("dlbook");

/// <summary>dlprog —— 当前书章节进度。</summary>
public sealed record DownloadProgressEvent(int Done, int Total, string Message)
    : BackendEvent("dlprog");

/// <summary>dlone —— 单本下载完成。</summary>
public sealed record DownloadOneEvent(
    BookSummaryDto Book, string Path, int BookIndex, int BookCount)
    : BackendEvent("dlone");

/// <summary>dldone —— 下载任务完成 (mode: single/batch; fmt: txt/epub)。</summary>
public sealed record DownloadDoneEvent(
    string Mode, string Format,
    IReadOnlyList<DownloadOkItemDto> OkList,
    IReadOnlyList<DownloadFailItemDto> FailList) : BackendEvent("dldone");

/// <summary>dlcancel —— 下载被取消。</summary>
public sealed record DownloadCancelEvent(
    IReadOnlyList<DownloadOkItemDto> OkList,
    IReadOnlyList<DownloadFailItemDto> FailList) : BackendEvent("dlcancel");

/// <summary>dlerr —— 业务级错误 (区别于协议级 error)。</summary>
public sealed record DownloadErrorEvent(string Message) : BackendEvent("dlerr");

// ---------------------------------- 线协议解析 ----------------------------------

/// <summary>
/// init 的应答 (hello 消息)。字段名与 server.py `_cmd_init` 的 reply 逐字同名。
/// </summary>
public sealed record BackendHello(
    string Version,
    bool Js,
    int Sources,
    int Files,
    int Checked,
    int Auth,
    string SourceDir)
{
    /// <summary>一行可读的后端状态摘要 (日志/状态栏用)。</summary>
    public string Summary =>
        $"后端已连接 · server v{Version} · JS 引擎{(Js ? "✓" : "✗")} · " +
        $"工作源 {Sources} · 书源文件 {Files} (清单 {Checked}) · 登录头 {Auth}";
}

/// <summary>
/// 后端 → 前端的一行 JSON 消息。type 决定语义:
/// event / hello / ack / busy / error。M0 只解析, 不消费。
/// </summary>
public sealed class BackendEnvelope
{
    [JsonPropertyName("type")] public string Type { get; init; } = "";
    [JsonPropertyName("kind")] public string? Kind { get; init; }
    [JsonPropertyName("payload")] public JsonElement Payload { get; init; }
    [JsonPropertyName("cmd")] public string? Cmd { get; init; }
    [JsonPropertyName("message")] public string? Message { get; init; }
    [JsonPropertyName("running")] public string? Running { get; init; }

    // hello 专属字段 (其余消息缺省, 无害)。
    // files/checked 用 JsonElement 承载: hello 时是数字 (文件数/清单数),
    // sources.list ack 时是数组 (SourceFileDto 行 / 清单文件名)。用数值访问器
    // (FilesCount / CheckedCount) 只取数字形态, 数组形态留给 ParseSourcesListAck。
    [JsonPropertyName("version")] public string? Version { get; init; }
    [JsonPropertyName("js")] public bool Js { get; init; }
    [JsonPropertyName("sources")] public int Sources { get; init; }
    [JsonPropertyName("files")] public JsonElement Files { get; init; }
    [JsonPropertyName("checked")] public JsonElement Checked { get; init; }
    [JsonPropertyName("auth")] public int Auth { get; init; }
    [JsonPropertyName("source_dir")] public string? SourceDir { get; init; }

    // auth 命令族 ack 专属字段 (auth.list / auth.save / auth.fetch; 其余消息缺省, 无害)。
    [JsonPropertyName("count")] public int Count { get; init; }
    [JsonPropertyName("entries")] public JsonElement Entries { get; init; }
    [JsonPropertyName("phase")] public string? Phase { get; init; }
    [JsonPropertyName("url")] public string? Url { get; init; }
    [JsonPropertyName("host")] public string? Host { get; init; }
    [JsonPropertyName("grab_host")] public string? GrabHost { get; init; }
    [JsonPropertyName("cookie")] public string? Cookie { get; init; }

    /// <summary>hello 的文件数 (files 为数字形态时; 数组形态回 0)。</summary>
    public int FilesCount => Files.ValueKind == JsonValueKind.Number ? Files.GetInt32() : 0;

    /// <summary>hello 的清单数 (checked 为数字形态时; 数组形态回 0)。</summary>
    public int CheckedCount => Checked.ValueKind == JsonValueKind.Number ? Checked.GetInt32() : 0;

    /// <summary>其余字段 (后续命令的 ack 数据等) 原样保留。</summary>
    [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; init; }

    /// <summary>hello → 强类型 BackendHello。</summary>
    public BackendHello ToHello() => new(
        Version ?? "", Js, Sources, FilesCount, CheckedCount, Auth, SourceDir ?? "");
}

// ---------------------------------- sources.list ack 数据 ----------------------------------

/// <summary>
/// sources.list ack 的 files 单项 (server._cmd_sources_list)。
/// checked = 该文件是否在校验/搜索清单内; exists = 磁盘上是否仍存在。
/// </summary>
public sealed record SourceFileDto(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("checked")] bool Checked,
    [property: JsonPropertyName("exists")] bool Exists);

/// <summary>sources.list 命令的完整应答 (files + 生效清单 + 书源目录)。</summary>
public sealed class SourcesListResult
{
    public IReadOnlyList<SourceFileDto> Files { get; init; } = Array.Empty<SourceFileDto>();
    public IReadOnlyList<string> Checked { get; init; } = Array.Empty<string>();
    public string Dir { get; init; } = "";
}

// ---------------------------------- auth 命令族 ack 数据 ----------------------------------

/// <summary>
/// auth.list 的 entries 单项 (server._cmd_auth_list)。
/// header 为原始 dict (JSON 对象); cookie 为字符串 (空串 = 该源仅配了 header)。
/// </summary>
public sealed record AuthEntryDto(
    [property: JsonPropertyName("url")] string Url,
    [property: JsonPropertyName("cookie")] string Cookie,
    [property: JsonPropertyName("header")] JsonElement Header)
{
    /// <summary>header dict → 可编辑的 JSON 文本 (空对象回空串)。</summary>
    public string HeaderJson
        => Header.ValueKind == JsonValueKind.Object && Header.EnumerateObject().Any()
            ? Header.ToString()
            : "";
}

/// <summary>auth.list 命令的完整应答 (已配置源条目)。</summary>
public sealed class AuthListResult
{
    public int Count { get; init; }
    public IReadOnlyList<AuthEntryDto> Entries { get; init; } = Array.Empty<AuthEntryDto>();
}

/// <summary>
/// auth.fetch 两段式 ack (server._run_auth_launch / _run_auth_grab):
///   phase=launched  → 浏览器已打开 url, 抓取主机为 grab_host;
///   phase=captured  → 已按 host 抓回 cookie (空串 = 没抓到)。
/// </summary>
public sealed record AuthFetchAckInfo(
    string Phase, string Url, string Host, string GrabHost, string Cookie);

/// <summary>
/// JSONL 行 → 强类型 BackendEvent 的解析器。
/// 线程纪律: 纯函数, 无共享状态; 调用方负责在 DispatcherQueue 上编组。
/// </summary>
public static class BackendEventParser
{
    private static readonly JsonSerializerOptions Opts = new()
    {
        PropertyNameCaseInsensitive = false,
    };

    /// <summary>
    /// 解析一行 JSONL 的信封 (不区分 type)。空行返回 null 且 error 为 null;
    /// JSON 破损返回 null 且 error 带原因 (不抛异常, 保证读行循环不中断)。
    /// </summary>
    public static BackendEnvelope? ParseEnvelope(string line, out string? error)
    {
        error = null;
        if (string.IsNullOrWhiteSpace(line))
        {
            return null;
        }

        try
        {
            return JsonSerializer.Deserialize<BackendEnvelope>(line, Opts);
        }
        catch (JsonException ex)
        {
            error = $"JSON 解析失败: {ex.Message}";
            return null;
        }
    }

    /// <summary>
    /// 信封 → 强类型事件。非 event 消息或未知 kind 返回 null。
    /// payload 解析失败时 error 带原因 (不抛异常)。
    /// </summary>
    public static BackendEvent? ParseEventFromEnvelope(BackendEnvelope env, out string? error)
    {
        error = null;
        if (env.Type != "event" || string.IsNullOrEmpty(env.Kind))
        {
            return null;
        }

        try
        {
            return ParseEvent(env.Kind!, env.Payload);
        }
        catch (Exception ex)
        {
            error = $"事件 {env.Kind} payload 解析失败: {ex.Message}";
            return null;
        }
    }

    /// <summary>
    /// 解析一行 JSONL。成功时返回强类型事件; 非 event 消息或未知 kind 返回 null。
    /// 反序列化失败时 error 带原因 (不抛异常, 保证读行循环不中断)。
    /// </summary>
    public static BackendEvent? ParseLine(string line, out string? error)
    {
        var env = ParseEnvelope(line, out error);
        if (env is null)
        {
            return null;
        }

        return ParseEventFromEnvelope(env, out error);
    }

    private static BackendEvent ParseEvent(string kind, JsonElement p) => kind switch
    {
        "log" => new LogEvent(p.GetString() ?? ""),
        "sprog" => new SearchProgressEvent(p.GetString() ?? ""),
        "hit" => new HitEvent(Deserialize<HitDto>(p)),
        "sres" => new SearchResultEvent(AtInt(p, 0), AtBool(p, 1)),
        "vfile" => new VerifyFileEvent(AtInt(p, 0), AtInt(p, 1), AtString(p, 2)),
        "vprog" => new VerifyProgressEvent(
            AtInt(p, 0), AtInt(p, 1), AtString(p, 2),
            AtInt(p, 3), AtInt(p, 4), AtInt(p, 5), AtInt(p, 6)),
        "vfile_done" => new VerifyFileDoneEvent(
            AtString(p, 0), AtString(p, 1), AtInt(p, 2), AtInt(p, 3), AtDouble(p, 4)),
        "vdeep" => new VerifyDeepEvent(
            AtInt(p, 0), AtInt(p, 1), AtString(p, 2), AtString(p, 3), AtInt(p, 4), AtInt(p, 5)),
        "vdone" => new VerifyDoneEvent(
            AtInt(p, 0), AtInt(p, 1), AtInt(p, 2), AtDouble(p, 3), AtBool(p, 4)),
        "dlbook" => new DownloadBookEvent(
            AtInt(p, 0), AtInt(p, 1), AtString(p, 2), AtString(p, 3), AtString(p, 4)),
        "dlprog" => new DownloadProgressEvent(AtInt(p, 0), AtInt(p, 1), AtString(p, 2)),
        "dlone" => new DownloadOneEvent(
            Deserialize<BookSummaryDto>(p[0]), AtString(p, 1), AtInt(p, 2), AtInt(p, 3)),
        "dldone" => new DownloadDoneEvent(
            AtString(p, 0), AtString(p, 1), ParseOkList(p[2]), ParseFailList(p[3])),
        "dlcancel" => new DownloadCancelEvent(ParseOkList(p[0]), ParseFailList(p[1])),
        "dlerr" => new DownloadErrorEvent(p.GetString() ?? ""),
        _ => throw new NotSupportedException($"未知事件 kind: {kind}"),
    };

    // -------- ack 数据读取 (sources.list) --------

    /// <summary>
    /// sources.list 的 ack 信封 → SourcesListResult。数据字段在 ack 顶层
    /// (files / checked / dir), 反序列化后落在 <see cref="BackendEnvelope.Extra"/> 里,
    /// 这里取出并强类型化。非该 ack 或形状不符返回 null (不抛)。
    /// </summary>
    public static SourcesListResult? ParseSourcesListAck(BackendEnvelope env)
    {
        if (env.Type != "ack" || env.Cmd != "sources.list")
        {
            return null;
        }

        try
        {
            var files = new List<SourceFileDto>();
            if (env.Files.ValueKind == JsonValueKind.Array)
            {
                foreach (var row in env.Files.EnumerateArray())
                {
                    files.Add(new SourceFileDto(
                        RowString(row, "name"),
                        RowBool(row, "checked"),
                        RowBool(row, "exists")));
                }
            }

            var checkedList = new List<string>();
            if (env.Checked.ValueKind == JsonValueKind.Array)
            {
                foreach (var s in env.Checked.EnumerateArray())
                {
                    if (s.ValueKind == JsonValueKind.String)
                    {
                        checkedList.Add(s.GetString() ?? "");
                    }
                }
            }

            var dir = "";
            if (env.Extra is not null
                && env.Extra.TryGetValue("dir", out var dEl)
                && dEl.ValueKind == JsonValueKind.String)
            {
                dir = dEl.GetString() ?? "";
            }

            return new SourcesListResult { Files = files, Checked = checkedList, Dir = dir };
        }
        catch (Exception)
        {
            // ack 形状意外 (理论不该发生): 视为无数据, 前端保持旧列表。
            return null;
        }
    }

    private static string RowString(JsonElement obj, string prop)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(prop, out var v)
           && v.ValueKind == JsonValueKind.String
            ? (v.GetString() ?? "")
            : "";

    private static bool RowBool(JsonElement obj, string prop)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(prop, out var v)
           && v.ValueKind == JsonValueKind.True;

    // -------- ack 数据读取 (auth) --------

    /// <summary>
    /// auth.list 的 ack 信封 → AuthListResult。数据字段在 ack 顶层
    /// (count / entries), 反序列化后落在 <see cref="BackendEnvelope"/> 对应属性上。
    /// 非该 ack 或形状不符返回 null (不抛)。
    /// </summary>
    public static AuthListResult? ParseAuthListAck(BackendEnvelope env)
    {
        if (env.Type != "ack" || env.Cmd != "auth.list")
        {
            return null;
        }

        try
        {
            var entries = new List<AuthEntryDto>();
            if (env.Entries.ValueKind == JsonValueKind.Array)
            {
                foreach (var row in env.Entries.EnumerateArray())
                {
                    if (row.ValueKind != JsonValueKind.Object)
                    {
                        continue;
                    }

                    entries.Add(new AuthEntryDto(
                        RowString(row, "url"),
                        RowString(row, "cookie"),
                        RowElement(row, "header")));
                }
            }

            return new AuthListResult { Count = env.Count, Entries = entries };
        }
        catch (Exception)
        {
            // ack 形状意外 (理论不该发生): 视为无数据, 前端保持旧列表。
            return null;
        }
    }

    /// <summary>
    /// auth.fetch 的 ack 信封 → AuthFetchAckInfo (phase/url/host/grab_host/cookie)。
    /// 非该 ack 或 phase 缺失返回 null (不抛)。
    /// </summary>
    public static AuthFetchAckInfo? ParseAuthFetchAck(BackendEnvelope env)
    {
        if (env.Type != "ack" || env.Cmd != "auth.fetch" || string.IsNullOrEmpty(env.Phase))
        {
            return null;
        }

        return new AuthFetchAckInfo(
            env.Phase!,
            env.Url ?? "",
            env.Host ?? "",
            env.GrabHost ?? "",
            env.Cookie ?? "");
    }

    private static JsonElement RowElement(JsonElement obj, string prop)
        => obj.ValueKind == JsonValueKind.Object
           && obj.TryGetProperty(prop, out var v)
            ? v
            : default;

    // -------- 数组元素读取助手 (payload 是元组 → JSON 数组) --------

    private static T Deserialize<T>(JsonElement e)
        => e.Deserialize<T>(Opts) ?? throw new JsonException($"无法反序列化为 {typeof(T).Name}");

    private static string AtString(JsonElement arr, int i)
        => arr[i].ValueKind == JsonValueKind.String ? (arr[i].GetString() ?? "") : arr[i].ToString();

    private static int AtInt(JsonElement arr, int i)
        => arr[i].ValueKind == JsonValueKind.Number ? arr[i].GetInt32() : 0;

    private static double AtDouble(JsonElement arr, int i)
        => arr[i].ValueKind == JsonValueKind.Number ? arr[i].GetDouble() : 0d;

    private static bool AtBool(JsonElement arr, int i)
        => arr[i].ValueKind == JsonValueKind.True;

    private static IReadOnlyList<DownloadOkItemDto> ParseOkList(JsonElement arr)
    {
        var list = new List<DownloadOkItemDto>();
        if (arr.ValueKind != JsonValueKind.Array)
        {
            return list;
        }

        foreach (var pair in arr.EnumerateArray())
        {
            list.Add(new DownloadOkItemDto(
                Deserialize<BookSummaryDto>(pair[0]),
                AtString(pair, 1)));
        }

        return list;
    }

    private static IReadOnlyList<DownloadFailItemDto> ParseFailList(JsonElement arr)
    {
        var list = new List<DownloadFailItemDto>();
        if (arr.ValueKind != JsonValueKind.Array)
        {
            return list;
        }

        foreach (var row in arr.EnumerateArray())
        {
            list.Add(new DownloadFailItemDto(
                AtInt(row, 0), AtString(row, 1), AtString(row, 2), AtString(row, 3)));
        }

        return list;
    }
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace NovelDownloader.Services.Backend;

/// <summary>
/// 后端进程客户端 (计划 §3.2 方案 A): spawn <c>python server.py</c> (或打包后的
/// <c>bookdl-backend.exe</c>), stdin 写 {"cmd": ...} 行 / stdout 逐行读 JSONL,
/// stderr 收尾写入诊断事件。
///
/// 线程纪律: 本类所有回调 (LineReceived / StderrLineReceived / DiagnosticMessage /
/// ProcessExited) 都在**后台线程**触发; 订阅方 (EventRouter) 必须自行编组到 UI 线程,
/// 绝不能在回调里直接改 ObservableCollection —— WinUI 3 是单线程单元, 会崩。
///
/// 编码: 三个管道都显式钉 UTF-8 **无 BOM**。
///   - 不显式设 StandardOutputEncoding 时管道按系统 OEM 代码页 (cp936) 解码, 中文全乱码;
///   - 用无 BOM 的 UTF8Encoding(false) 而非 Encoding.UTF8, 避免 .NET 往 stdin 首行
///     写入 BOM —— Python 侧 sys.stdin 是纯 utf-8 (非 utf-8-sig), 带 BOM 会让第一条
///     命令 json.loads 直接失败。
/// </summary>
public sealed class BackendClient : IAsyncDisposable, IDisposable
{
    private static readonly UTF8Encoding Utf8NoBom = new(encoderShouldEmitUTF8Identifier: false);
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private const int MaxAutoRestarts = 3;
    private const int StderrTailLines = 20;

    private readonly object _writeLock = new();
    private readonly object _tailLock = new();
    private readonly Queue<string> _stderrTail = new();

    private Process? _process;
    private CancellationTokenSource? _lifeCts;
    private int _generation;
    private int _restartCount;
    private volatile bool _disposed;

    /// <summary>后端是否已连接 (进程存活)。</summary>
    public bool IsRunning => !_disposed && _process is { HasExited: false };

    /// <summary>实际使用的启动命令行 (诊断用)。</summary>
    public string LaunchCommand { get; private set; } = "";

    /// <summary>后端工作目录 (仓库根)。</summary>
    public string WorkingDirectory { get; private set; } = "";

    /// <summary>收到一行 stdout (已剥离换行)。后台线程触发。</summary>
    public event Action<string>? LineReceived;

    /// <summary>收到一行 stderr。后台线程触发。</summary>
    public event Action<string>? StderrLineReceived;

    /// <summary>进程级诊断 (启动失败 / 退出 / 重启 / stderr 尾巴)。后台线程触发。</summary>
    public event Action<string>? DiagnosticMessage;

    /// <summary>后端进程退出 (退出码)。后台线程触发。</summary>
    public event Action<int>? ProcessExited;

    // ------------------------------------------------------------------ 启动 --

    /// <summary>
    /// 启动后端进程并挂上读写循环。
    /// 优先级: exe 同目录的 bookdl-backend.exe (M5 打包预留) &gt; python &gt; py -3。
    /// 返回是否启动成功。启动后由调用方发 init (App 里发, 便于"启动即握手"流程可见)。
    /// </summary>
    public async Task<bool> StartAsync(CancellationToken ct = default)
    {
        if (_disposed)
        {
            return false;
        }

        var baseDir = AppContext.BaseDirectory;
        var bundled = Path.Combine(baseDir, "bookdl-backend.exe");
        var root = ResolveRepoRoot();

        string exe, args, workDir;
        if (File.Exists(bundled))
        {
            // M5 打包形态: PyInstaller 冻出的 bookdl-backend.exe, 就在 exe 同目录。
            exe = bundled;
            args = "";
            workDir = root ?? baseDir;
        }
        else
        {
            if (root is null)
            {
                RaiseDiagnostic("启动失败: 找不到仓库根 (向上未发现 server.py)。");
                return false;
            }

            var python = ResolvePython();
            if (python is null)
            {
                RaiseDiagnostic("启动失败: 未找到 python / py -3, 请确认 Python 已在 PATH。");
                return false;
            }

            exe = python.Value.Exe;
            args = CombineArgs(python.Value.Args, Quote(Path.Combine(root, "server.py")));
            workDir = root;
        }

        var psi = new ProcessStartInfo(exe, args)
        {
            WorkingDirectory = workDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = Utf8NoBom,
            StandardOutputEncoding = Utf8NoBom,
            StandardErrorEncoding = Utf8NoBom,
        };
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONUNBUFFERED"] = "1";

        Process p;
        try
        {
            p = new Process { StartInfo = psi, EnableRaisingEvents = true };
            if (!p.Start())
            {
                RaiseDiagnostic($"启动失败: Process.Start 返回 false ({exe})。");
                p.Dispose();
                return false;
            }
        }
        catch (Exception ex)
        {
            RaiseDiagnostic($"启动后端进程失败: {ex.Message} ({exe} {args})");
            return false;
        }

        var gen = ++_generation;
        _process = p;
        LaunchCommand = $"{exe} {args}".Trim();
        WorkingDirectory = workDir;
        p.Exited += (s, _) => OnProcessExited((Process)s!, gen);

        var life = new CancellationTokenSource();
        var old = _lifeCts;
        _lifeCts = life;
        old?.Cancel();
        old?.Dispose();

        _stderrTail.Clear();
        RaiseDiagnostic($"后端已启动: {LaunchCommand} (工作目录 {workDir})");

        _ = Task.Run(() => ReadStdoutLoopAsync(p, gen, life.Token));
        _ = Task.Run(() => ReadStderrLoopAsync(p, gen, life.Token));

        await Task.CompletedTask;
        return true;
    }

    // ------------------------------------------------------------------ 发送 --

    /// <summary>向前端 stdin 写一条命令 (加锁 + flush; 进程已退出时静默忽略)。</summary>
    public Task SendAsync(string jsonCommand, CancellationToken ct = default)
    {
        var p = _process;
        if (_disposed || p is null || p.HasExited)
        {
            RaiseDiagnostic("发送命令失败: 后端未运行。");
            return Task.CompletedTask;
        }

        lock (_writeLock)
        {
            try
            {
                p.StandardInput.WriteLine(jsonCommand);
                p.StandardInput.Flush();
            }
            catch (Exception ex)
            {
                RaiseDiagnostic($"写入后端 stdin 失败: {ex.Message}");
            }
        }

        return Task.CompletedTask;
    }

    /// <summary>init: 握手, 后端回 hello (版本/JS/源统计)。</summary>
    public Task InitAsync(CancellationToken ct = default)
        => SendAsync(Json(new Dictionary<string, object?> { ["cmd"] = "init" }), ct);

    /// <summary>search: 并发搜索。domain = 自动|书名|作者|分类。</summary>
    public Task SearchAsync(
        string keyword, bool fuzzy = true, bool rel = true,
        string domain = "自动", bool deepOnly = false, CancellationToken ct = default)
        => SendAsync(Json(new Dictionary<string, object?>
        {
            ["cmd"] = "search",
            ["keyword"] = keyword,
            ["fuzzy"] = fuzzy,
            ["rel"] = rel,
            ["domain"] = domain,
            ["deep_only"] = deepOnly,
        }), ct);

    /// <summary>stop: 等价旧 stop_all, 后端置当前重操作的 threading.Event。</summary>
    public Task StopAsync(CancellationToken ct = default)
        => SendAsync(Json(new Dictionary<string, object?> { ["cmd"] = "stop" }), ct);

    /// <summary>config.get: 只读并发档 / 目录等配置。</summary>
    public Task ConfigGetAsync(CancellationToken ct = default)
        => SendAsync(Json(new Dictionary<string, object?> { ["cmd"] = "config.get" }), ct);

    /// <summary>
    /// download: 下载选中书目。hits 为 HitItem 列表 (转为 DTO 传给后端)。
    /// mode = "单一" | "合并", fmt = "TXT" | "EPUB", outDir 为输出目录。
    /// </summary>
    public Task DownloadAsync(
        IReadOnlyList<Models.HitItem> hits, string mode, string fmt, string outDir,
        CancellationToken ct = default)
    {
        var hitsPayload = hits.Select(h => new Dictionary<string, object?>
        {
            ["name"] = h.Name,
            ["author"] = h.Author,
            ["kind"] = h.Kind,
            ["book_url"] = h.BookUrl,
            ["last_chapter"] = h.LastChapter,
            ["source"] = new Dictionary<string, string> { ["bookSourceName"] = h.SourceName },
        }).ToList();

        // 线协议: server._cmd_download 认 "single"/"merge" (内部再 merge→batch), fmt 认 "txt"/"epub"。
        // UI 侧是中文 "单一"/"合并" 与大写 "TXT"/"EPUB", 这里统一映射, 兼容直接传英文的情况。
        return SendAsync(Json(new Dictionary<string, object?>
        {
            ["cmd"] = "download",
            ["hits"] = hitsPayload,
            ["mode"] = MapMode(mode),
            ["fmt"] = MapFormat(fmt),
            ["out"] = outDir,
        }), ct);
    }

    /// <summary>UI 下载模式 → server 线协议: 合并/merge → "merge", 其余 → "single"。</summary>
    private static string MapMode(string? mode)
        => mode is not null &&
           (mode.Equals("合并", StringComparison.Ordinal) || mode.Equals("merge", StringComparison.OrdinalIgnoreCase))
            ? "merge"
            : "single";

    /// <summary>UI 导出格式 → server 线协议: 含 "epub" (不区分大小写) → "epub", 其余 → "txt"。</summary>
    private static string MapFormat(string? fmt)
        => fmt is not null && fmt.Contains("epub", StringComparison.OrdinalIgnoreCase)
            ? "epub"
            : "txt";

    // -------------------------------------------------------------- 读行循环 --

    private async Task ReadStdoutLoopAsync(Process p, int gen, CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                var line = await p.StandardOutput.ReadLineAsync(ct).ConfigureAwait(false);
                if (line is null)
                {
                    break; // EOF: 进程要退出了, 退出处理走 Exited 事件。
                }

                if (gen == _generation && !_disposed)
                {
                    LineReceived?.Invoke(line);
                }
            }
        }
        catch (OperationCanceledException)
        {
            // 正常收尾。
        }
        catch (Exception ex)
        {
            if (gen == _generation && !_disposed)
            {
                RaiseDiagnostic($"读取后端 stdout 异常: {ex.Message}");
            }
        }
    }

    private async Task ReadStderrLoopAsync(Process p, int gen, CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                var line = await p.StandardError.ReadLineAsync(ct).ConfigureAwait(false);
                if (line is null)
                {
                    break;
                }

                PushStderrTail(line);
                if (gen == _generation && !_disposed)
                {
                    StderrLineReceived?.Invoke(line);
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception)
        {
            // stderr 读取失败不致命 (进程退出时常见)。
        }
    }

    // ------------------------------------------------------------ 退出/重启 --

    private void OnProcessExited(Process p, int gen)
    {
        if (_disposed || gen != _generation)
        {
            return;
        }

        int code;
        try
        {
            code = p.ExitCode;
        }
        catch
        {
            code = -1;
        }

        FlushStderrTail();
        RaiseDiagnostic($"后端进程已退出 (退出码 {code})。");
        ProcessExited?.Invoke(code);

        if (code != 0 && _restartCount < MaxAutoRestarts)
        {
            _ = AutoRestartAsync();
        }
    }

    private async Task AutoRestartAsync()
    {
        _restartCount++;
        RaiseDiagnostic($"后端异常退出, 2 秒后自动重启 (第 {_restartCount}/{MaxAutoRestarts} 次)…");
        try
        {
            await Task.Delay(2000).ConfigureAwait(false);
        }
        catch
        {
            return;
        }

        if (_disposed)
        {
            return;
        }

        if (await StartAsync().ConfigureAwait(false))
        {
            await InitAsync().ConfigureAwait(false);
        }
        else
        {
            RaiseDiagnostic("后端自动重启失败, 请检查 Python 环境。");
        }
    }

    /// <summary>手动重启 (停止旧进程并重新 spawn + init)。</summary>
    public async Task<bool> RestartAsync()
    {
        StopCurrentProcess();
        var ok = await StartAsync().ConfigureAwait(false);
        if (ok)
        {
            await InitAsync().ConfigureAwait(false);
        }

        return ok;
    }

    // ------------------------------------------------------------- 生命周期 --

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        StopCurrentProcess();
    }

    public ValueTask DisposeAsync()
    {
        Dispose();
        return ValueTask.CompletedTask;
    }

    private void StopCurrentProcess()
    {
        try
        {
            _lifeCts?.Cancel();
        }
        catch
        {
        }

        var p = _process;
        _process = null;
        if (p is null)
        {
            return;
        }

        try
        {
            if (!p.HasExited)
            {
                p.Kill(entireProcessTree: true);
                p.WaitForExit(2000);
            }
        }
        catch
        {
            // 进程已退出, 忽略。
        }

        try
        {
            p.Dispose();
        }
        catch
        {
        }
    }

    // ----------------------------------------------------------------- 助手 --

    private static string Json(object payload) => JsonSerializer.Serialize(payload, JsonOpts);

    private void RaiseDiagnostic(string message) => DiagnosticMessage?.Invoke(message);

    private void PushStderrTail(string line)
    {
        lock (_tailLock)
        {
            _stderrTail.Enqueue(line);
            while (_stderrTail.Count > StderrTailLines)
            {
                _stderrTail.Dequeue();
            }
        }
    }

    private void FlushStderrTail()
    {
        string[] lines;
        lock (_tailLock)
        {
            lines = _stderrTail.ToArray();
            _stderrTail.Clear();
        }

        if (lines.Length > 0)
        {
            RaiseDiagnostic("── stderr 尾巴 ──");
            foreach (var l in lines)
            {
                RaiseDiagnostic("  " + l);
            }
        }
    }

    /// <summary>向上查找包含 server.py 的仓库根; 支持 BOOKDL_ROOT 覆盖。</summary>
    private static string? ResolveRepoRoot()
    {
        var env = Environment.GetEnvironmentVariable("BOOKDL_ROOT");
        if (!string.IsNullOrWhiteSpace(env) && File.Exists(Path.Combine(env, "server.py")))
        {
            return Path.GetFullPath(env);
        }

        foreach (var start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            try
            {
                for (var dir = new DirectoryInfo(start); dir is not null; dir = dir.Parent)
                {
                    if (File.Exists(Path.Combine(dir.FullName, "server.py")))
                    {
                        return dir.FullName;
                    }
                }
            }
            catch
            {
                // 路径非法, 试下一个起点。
            }
        }

        return null;
    }

    /// <summary>探测 Python 解释器: 先 python, 失败回退 py -3 (各探一次)。</summary>
    private static (string Exe, string Args)? ResolvePython()
    {
        foreach (var (exe, args) in new[] { ("python", ""), ("py", "-3") })
        {
            if (ProbeInterpreter(exe, args))
            {
                return (exe, args);
            }
        }

        return null;
    }

    private static bool ProbeInterpreter(string exe, string args)
    {
        try
        {
            var psi = new ProcessStartInfo(exe, CombineArgs(args, "--version"))
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var p = Process.Start(psi);
            if (p is null)
            {
                return false;
            }

            if (!p.WaitForExit(5000))
            {
                try
                {
                    p.Kill(entireProcessTree: true);
                }
                catch
                {
                }

                return false;
            }

            return p.ExitCode == 0;
        }
        catch
        {
            return false;
        }
    }

    private static string CombineArgs(string a, string b)
        => string.Join(' ', new[] { a, b }.Where(s => !string.IsNullOrWhiteSpace(s)));

    private static string Quote(string path) => path.Contains(' ') ? $"\"{path}\"" : path;
}

using System;
using System.IO;

namespace NovelDownloader.Services;

/// <summary>临时诊断跟踪 (仅 DEBUG 构建; 诊断完删除)。</summary>
internal static class TraceLog
{
    private static readonly string Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "novel_ui_trace.log");
    private static readonly object Lock = new();

    public static void Write(string msg)
    {
        try
        {
            lock (Lock)
            {
                File.AppendAllText(Path, $"{DateTime.Now:HH:mm:ss.fff} {msg}\n");
            }
        }
        catch
        {
        }
    }
}

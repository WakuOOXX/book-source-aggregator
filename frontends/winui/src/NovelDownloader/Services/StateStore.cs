using System.Collections.Generic;

namespace NovelDownloader.Services;

/// <summary>
/// 状态记忆: 等价于旧版 sel_state.json (multi/selected/sources/verify_dones/
/// verify_origin/verify_done/fuzzy/rel/deep_only/para_tabs)。
/// M0: 只定义键名常量与内存占位; M2 起实现读写与旧格式平滑迁移。
/// 注意: 只读旧文件, 不改写 Python 侧产物。
/// </summary>
public sealed class StateStore
{
    // 与 sel_state.json 逐字同名的键 (见仓库根 sel_state.json)。
    public const string KeyMulti = "multi";
    public const string KeySelected = "selected";
    public const string KeySources = "sources";
    public const string KeyVerifyDones = "verify_dones";
    public const string KeyVerifyOrigin = "verify_origin";
    public const string KeyVerifyDone = "verify_done";
    public const string KeyFuzzy = "fuzzy";
    public const string KeyRel = "rel";
    public const string KeyDeepOnly = "deep_only";
    public const string KeyParaTabs = "para_tabs";

    private readonly Dictionary<string, object?> _memory = new();

    /// <summary>M2: 从 sel_state.json 读取 (只读)。M0 返回空。</summary>
    public object? Get(string key) => _memory.TryGetValue(key, out var v) ? v : null;

    /// <summary>M2: 写回。M0 仅内存。</summary>
    public void Set(string key, object? value) => _memory[key] = value;
}

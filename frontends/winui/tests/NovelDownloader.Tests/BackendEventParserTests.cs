using System.Text.Json;
using NovelDownloader.Services.Backend;

namespace NovelDownloader.Tests;

/// <summary>
/// JSONL 事件解析的往返测试: 用与 server.py 逐字一致的线格式喂给解析器。
/// M1 起扩充为「事件 → 状态机」全量用例。
/// </summary>
public class BackendEventParserTests
{
    [Fact]
    public void ParseLine_Log_ReturnsLogEvent()
    {
        const string line = """{"type":"event","kind":"log","payload":"已加载 shuyuan/x.json:12 源"}""";

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(error);
        var log = Assert.IsType<LogEvent>(ev);
        Assert.Equal("已加载 shuyuan/x.json:12 源", log.Message);
        Assert.Equal("log", log.Kind);
    }

    [Fact]
    public void ParseLine_Hit_ReadsObjectPayloadAndSourceName()
    {
        const string line = """
        {"type":"event","kind":"hit","payload":{"source":{"bookSourceName":"起点"},"name":"某某书","author":"作者","kind":"都市","book_url":"https://a/b","last_chapter":"第10章 尾","intro":"简介"}}
        """;

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(error);
        var hit = Assert.IsType<HitEvent>(ev);
        Assert.Equal("某某书", hit.Hit.Name);
        Assert.Equal("都市", hit.Hit.Kind);
        Assert.Equal("起点", hit.Hit.SourceName);
    }

    [Fact]
    public void ParseLine_VerifyProgress_ReadsTuplePayloadInOrder()
    {
        // vprog: [file_index, file_count, file_name, done, total, ok, bad] —— 1-based
        const string line = """{"type":"event","kind":"vprog","payload":[2,5,"a.json",50,120,40,10]}""";

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(error);
        var p = Assert.IsType<VerifyProgressEvent>(ev);
        Assert.Equal(2, p.FileIndex);
        Assert.Equal(5, p.FileCount);
        Assert.Equal("a.json", p.FileName);
        Assert.Equal(50, p.Done);
        Assert.Equal(120, p.Total);
        Assert.Equal(40, p.OkCount);
        Assert.Equal(10, p.BadCount);
    }

    [Fact]
    public void ParseLine_SearchResult_MapsCountAndFuzzy()
    {
        const string line = """{"type":"event","kind":"sres","payload":[128,true]}""";

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(error);
        var sres = Assert.IsType<SearchResultEvent>(ev);
        Assert.Equal(128, sres.HitCount);
        Assert.True(sres.Fuzzy);
    }

    [Fact]
    public void ParseLine_DownloadDone_ProjectsOkAndFailLists()
    {
        const string line = """
        {"type":"event","kind":"dldone","payload":["batch","epub",[[{"title":"A","ok":9,"total":10},"out/A.epub"]],[[3,"B","源X","403"]]]}
        """;

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(error);
        var done = Assert.IsType<DownloadDoneEvent>(ev);
        Assert.Equal("batch", done.Mode);
        Assert.Equal("epub", done.Format);
        Assert.Single(done.OkList);
        Assert.Equal("A", done.OkList[0].Book.Title);
        Assert.Equal(9, done.OkList[0].Book.Ok);
        Assert.Equal("out/A.epub", done.OkList[0].Path);
        Assert.Single(done.FailList);
        Assert.Equal(3, done.FailList[0].Index);
        Assert.Equal("403", done.FailList[0].Error);
    }

    [Theory]
    [InlineData("""{"type":"event","kind":"sprog","payload":"3/10 源 · 抓取中"}""", "sprog")]
    [InlineData("""{"type":"event","kind":"vfile","payload":[1,2,"a.json"]}""", "vfile")]
    [InlineData("""{"type":"event","kind":"vdeep","payload":[1,2,"a.json","试搜",0,8]}""", "vdeep")]
    [InlineData("""{"type":"event","kind":"vdone","payload":[2,10,1,3.5,false]}""", "vdone")]
    [InlineData("""{"type":"event","kind":"dlbook","payload":[0,3,"A","源X","探测目录…"]}""", "dlbook")]
    [InlineData("""{"type":"event","kind":"dlprog","payload":[5,100,"第5章"]}""", "dlprog")]
    [InlineData("""{"type":"event","kind":"dlcancel","payload":[[],[[0,"A","源X","取消"]]]}""", "dlcancel")]
    [InlineData("""{"type":"event","kind":"dlerr","payload":"导出目录创建失败"}""", "dlerr")]
    public void ParseLine_AllKinds_ReturnMatchingKind(string line, string expectedKind)
    {
        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(error);
        Assert.NotNull(ev);
        Assert.Equal(expectedKind, ev!.Kind);
    }

    [Fact]
    public void ParseLine_NonEventMessage_ReturnsNull()
    {
        const string line = """{"type":"hello","cmd":"init","version":"1.9.1"}""";

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(ev);
        Assert.Null(error);
    }

    [Fact]
    public void ParseSourcesListAck_ReadsFilesCheckedDirFromArrayShape()
    {
        // sources.list ack 的 files/checked 是数组 (hello 的 files/checked 才是数字)。
        // 信封共用同名字段, 必须两种形态都能反序列化 (回归: 曾因 int 绑定数组而崩)。
        const string line = """
        {"type":"ack","cmd":"sources.list","files":[{"name":"a.json","checked":true,"exists":true},{"name":"b.json","checked":false,"exists":false}],"checked":["a.json"],"dir":"E:\\book\\bookdl\\shuyuan"}
        """;

        var env = BackendEventParser.ParseEnvelope(line, out var error);

        Assert.Null(error);
        Assert.NotNull(env);
        var result = BackendEventParser.ParseSourcesListAck(env!);

        Assert.NotNull(result);
        Assert.Equal(2, result!.Files.Count);
        Assert.Equal("a.json", result.Files[0].Name);
        Assert.True(result.Files[0].Checked);
        Assert.True(result.Files[0].Exists);
        Assert.False(result.Files[1].Exists);
        Assert.Single(result.Checked);
        Assert.Equal("a.json", result.Checked[0]);
        Assert.Equal("E:\\book\\bookdl\\shuyuan", result.Dir);
    }

    [Fact]
    public void ParseLine_BrokenJson_ReportsErrorWithoutThrowing()
    {
        const string line = """{"type":"event","kind":"log",""";

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(ev);
        Assert.NotNull(error);
    }

    [Fact]
    public void ParseLine_UnknownKind_ReportsError()
    {
        const string line = """{"type":"event","kind":"does_not_exist","payload":1}""";

        var ev = BackendEventParser.ParseLine(line, out var error);

        Assert.Null(ev);
        Assert.NotNull(error);
    }
}

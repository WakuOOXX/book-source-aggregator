# -*- coding: utf-8 -*-
"""
Legado(开源阅读)规则引擎 —— 纯规则书源语法子集实现。

支持:
* 变量替换 {{key}}/{{page}};  "URL,\n{json}" 请求配置(method/body/headers)
* 选择器: class.x / id.x / tag.x / text.x / #x / .x / 简化 XPath / 简化 JSONPath
  以 @ 串联: id.content@html / class.x@a / text.章节目录@href / tag.a.1@text
* 叶子:@text @textNodes @ownText @html @href @src @value 及其余按属性
* 取值:##正则##替换##... 链式替换;|| 备选;&& 拼接(字符串级)
* 集合规则返回节点,单值规则返回字符串(自动取第一/拼接)
不支持 @js / <js> 脚本 —— 调用方需预先跳过含脚本的书源,这里抛 RuleError。
"""
import re
import json
from bs4 import BeautifulSoup, Tag, NavigableString

ATTRS = {"text", "textnodes", "owntext", "html", "all", "href", "src",
         "value", "alt", "title", "content", "data-src", "data-original",
         "datetime", "data-id"}
_TAGLIKE = re.compile(r"^[a-zA-Z][\w\-]*$")


class RuleError(Exception):
    pass


# ============================================================ URL / 请求 ====
def subst(raw: str, ctx: dict) -> str:
    """替换 {{key}}/{{page}} 等;未知变量原样保留。"""
    def rep(m):
        k = m.group(1)
        if k in ("key", "searchKey", "searchkey"):
            return str(ctx.get("key", ""))
        return str(ctx.get(k, m.group(0)))
    return re.sub(r"\{\{\s*([\w.]+?)\s*\}\}", rep, raw)


def _opt_int(v, default=0):
    try:
        return int(str(v).strip())
    except Exception:
        return default


def parse_request(raw: str, ctx: dict) -> dict:
    """→ dict(url, method, body, headers, retry, charset, useWebView)。
    支持 'url,{json}' 与纯 URL(GET);retry/charset/useWebView 为 URL option
    (参照 Legado: {"retry":3} 非 2xx/连接异常重试, {"charset":"gbk"} 显式解码)。"""
    r = subst(raw, ctx).strip()
    if r.startswith("data:") or r.startswith("javascript:"):
        raise RuleError("不支持的 URL 类型: %s" % r[:40])
    m = re.match(r"^(.*?),\s*(\{.*\})\s*$", r, re.S)
    if not m:
        return {"url": r, "method": "GET", "body": "", "headers": None,
                "retry": 0, "charset": "", "useWebView": False}
    url, cfgtext = m.group(1).strip(), m.group(2)
    try:
        cfg = json.loads(cfgtext)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "url": url,
        "method": str(cfg.get("method", "GET")).upper(),
        "body": cfg.get("body", ""),
        "headers": cfg.get("headers") or None,
        "retry": _opt_int(cfg.get("retry")),
        "charset": str(cfg.get("charset") or ""),
        "useWebView": bool(cfg.get("useWebView")),
    }


def parse_header(raw) -> dict:
    """书源 header 字段(JSON 字符串)。@js 或解析失败返回 {}。"""
    if not raw:
        return {}
    raw = str(raw).strip()
    if raw.startswith("@js") or "<js>" in raw:
        return {}
    try:
        h = json.loads(raw)
        return h if isinstance(h, dict) else {}
    except Exception:
        return {}


# ======================================================== 通用文本处理 =======
def norm_text(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _split_outside(rule: str, seps):
    """按顶层分隔子串切分(支持多字符,忽略 [] 与引号)。返回段列表。"""
    parts, cur, depth, q = [], "", 0, None
    i = 0
    while i < len(rule):
        ch = rule[i]
        if q:
            cur += ch
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "\"'":
            q = ch
            cur += ch
            i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if depth == 0:
            hit = None
            for s in seps:
                if rule.startswith(s, i):
                    hit = s
                    break
            if hit:
                parts.append(cur)
                cur = ""
                i += len(hit)
                continue
        cur += ch
        i += 1
    parts.append(cur)
    return parts


def _first_hash_hash(rule: str) -> int:
    """返回括号外第一个 '##' 的位置,无则 -1。"""
    depth = 0
    q = None
    i = 0
    while i < len(rule) - 1:
        ch = rule[i]
        if q:
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "\"'":
            q = ch
            i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if depth == 0 and rule[i] == "#" and rule[i + 1] == "#":
            return i
        i += 1
    return -1


def apply_replaces(text: str, tail: str) -> str:
    """'##regex##repl##regex2##repl2' 链式;单段视为删除。"""
    if not tail:
        return text
    parts = [p for p in tail[2:].split("##")]
    pairs = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts):
            pairs.append((parts[i], parts[i + 1]))
            i += 2
        else:
            pairs.append((parts[i], ""))
            i += 1
    out = text
    for pat, rep in pairs:
        if pat == "":
            continue
        try:
            rep2 = rep.replace("\\n", "\n").replace("\\r", "\r")
            out = re.sub(pat, rep2, out)
        except re.error:
            continue
    return out


# ============================================================ HTML 工具 =====
_PARSER = "lxml"


def set_parser(name):
    global _PARSER
    _PARSER = name


def parse_dom(html: str):
    try:
        return BeautifulSoup(html or "", _PARSER)
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def _find_text_node(node, text: str):
    want = norm_text(text)
    if not want:
        return None
    best = None
    for leaf in node.find_all(string=True):
        t = norm_text(str(leaf))
        if not t:
            continue
        if t == want:
            return leaf.parent
        if want in t and best is None:
            best = leaf.parent
    return best


def _leaf(el, name):
    name = name.lower()
    if name == "text" or name == "textnodes":
        # 无分隔拼接:避免站点对命中词加 <mark>/<font> 高亮造成 "斗破苍 穹" 式空格
        return "".join(el.stripped_strings)
    if name == "owntext":
        return (el.string or "").strip()
    if name == "html":
        return "".join(str(c) for c in el.children)
    if name == "all":
        return str(el)
    return el.get(name, "") or ""


# ======================================================== XPath 简化子集 ====
def _convert_xpath(xp: str):
    """'//tag[@a='v'][2]/@attr' / '//meta[@property="x"]/@content' 等常见形态。
    逐 '/' 拆(引号/括号内不拆),转内部步骤。"""
    segs, buf, depth, q = [], "", 0, None
    for ch in xp:
        if q:
            buf += ch
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
            buf += ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "/" and depth == 0:
            segs.append(buf)
            buf = ""
        else:
            buf += ch
    if buf or not segs:
        segs.append(buf)
    chain = []
    for raw in segs:
        s = raw
        if s.startswith("//"):
            s = s[2:]
        elif s.startswith("/"):
            s = s[1:]
        if not s:
            continue
        if s.startswith("@"):
            chain.append(("attr", s[1:]))
            continue
        if s == "text()":
            chain.append(("attr", "text"))
            continue
        m = re.match(r"^([A-Za-z*][\w\-]*)?(.*)$", s)
        tag = m.group(1) or "*"
        bracket = m.group(2) or ""
        preds = []
        idx = None
        for pm in re.finditer(r"\[@([\w\-]+)\s*(==|=|!=|~=)\s*['\"]([^'\"]*)['\"]\]", bracket):
            preds.append((pm.group(1), pm.group(2), pm.group(3)))
        pm = re.search(r"\[(\d+)\]", bracket)
        if pm:
            idx = int(pm.group(1)) - 1
        chain.append(("xptag", tag, preds, idx))
    return chain


# ====================================================== JSONPath 简化子集 ===
def _jp_member(o, key):
    if isinstance(o, dict):
        return [o[key]] if key in o else []
    if isinstance(o, list):
        out = []
        for x in o:
            if isinstance(x, dict) and key in x:
                out.append(x[key])
        return out
    return []


def jsonpath_find(obj, expr: str):
    """$.a.b、$..name、[n]、[*]、['k']、[?(@.k=='v')]。返回命中值列表。"""
    out = []
    stack = [(obj, expr.lstrip("$"))]
    while stack:
        cur, rest = stack.pop()
        if rest == "":
            if not isinstance(cur, (dict, list)):
                out.append(cur)
            else:
                out.append(cur)
            continue
        # 过滤器 [?(@.k==...)] / [?(@.k)] 
        fm = re.match(r"^\[\?\s*@\.([\w]+)\s*(==|!=)\s*['\"]([^'\"]*)['\"]\s*\]", rest)
        if fm:
            k, op, v = fm.group(1), fm.group(2), fm.group(3)
            items = cur if isinstance(cur, list) else [cur]
            tail = rest[fm.end():]
            for it in items:
                val = it.get(k) if isinstance(it, dict) else None
                ok = (str(val) == v) if op == "==" else (str(val) != v)
                if ok:
                    stack.append((it, tail))
            continue
        fe = re.match(r"^\[\?\s*@\.([\w]+)\s*\]", rest)
        if fe:
            k = fe.group(1)
            items = cur if isinstance(cur, list) else [cur]
            tail = rest[fe.end():]
            for it in items:
                if isinstance(it, dict) and it.get(k):
                    stack.append((it, tail))
            continue
        if rest.startswith(".."):
            m = re.match(r"^\.\.([\w$\-]+)", rest)
            if not m:
                # $..* 
                def walk(o):
                    if isinstance(o, dict):
                        for v in o.values():
                            out.append(v)
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            out.append(v)
                            walk(v)
                walk(cur)
                continue
            k, tail = m.group(1), rest[m.end():]
            def rec(o):
                for h in _jp_member(o, k):
                    out.append(h)
                if isinstance(o, dict):
                    for v in o.values():
                        rec(v)
                elif isinstance(o, list):
                    for v in o:
                        rec(v)
            rec(cur)
            if tail:
                nxt = list(out)
                out.clear()
                for o in nxt:
                    stack.append((o, tail))
            continue
        m = re.match(r"^\.([\w$\-]+)|^\[(\d+)\]|^\[\*\]|^\[['\"]([^'\"]+)['\"]\]|^\['([^']+)'\]|^\[\"([^\"]+)\"\]", rest)
        if not m:
            # 未识别的 key 段
            key = rest.strip(". '\"[]")
            hits = _jp_member(cur, key)
            out.extend(hits)
            continue
        tail = rest[m.end():]
        if m.group(1):
            hits = _jp_member(cur, m.group(1))
        elif m.group(2):
            idx = int(m.group(2))
            hits = [cur[idx]] if isinstance(cur, list) and -len(cur) <= idx < len(cur) else []
        elif m.group(3):
            hits = cur if isinstance(cur, list) else []
        else:
            key = m.group(4) or m.group(5) or m.group(6)
            hits = _jp_member(cur, key)
        for h in hits:
            stack.append((h, tail)) if tail else out.append(h)
    return out


# ========================================================== 选择器步骤执行 ==
def _apply_step(nodes, step):
    res = []
    kind = step["kind"]
    if kind == "tag":
        for nd in nodes:
            if not isinstance(nd, Tag):
                continue
            name = step["name"].lower()
            for el in nd.find_all(name if name != "*" else True):
                if name == "*" or (el.name or "").lower() == name:
                    res.append(el)
    elif kind == "class":
        for nd in nodes:
            if not isinstance(nd, Tag):
                continue
            for el in nd.find_all(True):
                if step["name"] in (el.get("class") or []):
                    res.append(el)
    elif kind == "id":
        for nd in nodes:
            if not isinstance(nd, Tag):
                continue
            for el in nd.find_all(True):
                if el.get("id") == step["name"]:
                    res.append(el)
    elif kind == "textnode":
        for nd in nodes:
            if not isinstance(nd, Tag):
                continue
            el = _find_text_node(nd, step["name"])
            if el is not None:
                res.append(el)
    elif kind == "xptag":
        for nd in nodes:
            if not isinstance(nd, Tag):
                continue
            name = step["name"].lower()
            for el in nd.find_all(name if name != "*" else True):
                if name != "*" and (el.name or "").lower() != name:
                    continue
                ok = True
                for attr, op, val in step["preds"]:
                    got = el.get(attr, "")
                    if isinstance(got, list):
                        got = " ".join(got)
                    got = str(got)
                    if op in ("=", "==") and got != val:
                        ok = False
                        break
                    if op == "!=" and got == val:
                        ok = False
                        break
                    if op == "~=" and val not in got.split():
                        ok = False
                        break
                if ok:
                    res.append(el)
    return res


def _parse_token(tk: str):
    """'class.x'/'id.x'/'tag.x[.N]'/'text.x'/'#x'/'.x'/裸tag/[attrs的xptag] 由转换层处理。"""
    m = re.match(r"^(tag|class|id|text)\.(.+)$", tk, re.I)
    kind = m.group(1).lower() if m else None
    rest = m.group(2) if m else tk
    if kind is None:
        if tk.startswith("#"):
            return {"kind": "id", "name": tk[1:], "idx": None}
        if tk.startswith("."):
            name = tk[1:]
            im = re.search(r"\.(-?\d+)$", name)
            idx = int(im.group(1)) if im else None
            if im:
                name = name[: im.start()]
            return {"kind": "class", "name": name, "idx": idx}
        name = tk.split(".")[0]
        return {"kind": "tag", "name": name, "idx": None}
    idx = None
    im = re.search(r"\.(-?\d+)$", rest)
    if im:
        idx = int(im.group(1))
        rest = rest[: im.start()]
    elif re.search(r"\.(all|last)$", rest):
        idx = -1 if re.search(r"\.last$", rest) else None
        rest = re.sub(r"\.(all|last)$", "", rest)
    return {"kind": kind, "name": rest, "idx": idx}


def _run_chain(root, chain, leaf):
    nodes = [root] if isinstance(root, Tag) else []
    for step in chain:
        nodes = _apply_step(nodes, step)
        if step.get("idx") is not None:
            idx = step["idx"]
            if idx < 0:
                idx = len(nodes) + idx
            nodes = [nodes[idx]] if 0 <= idx < len(nodes) else []
        if not nodes:
            break
    return nodes


def _split_leaf(rule_body: str):
    """把规则体按 @ 拆成 (步骤token列表, 叶子名或None)。"""
    toks = []
    cur = ""
    depth = 0
    q = None
    for ch in rule_body:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
            cur += ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "@" and depth == 0:
            toks.append(cur)
            cur = ""
        else:
            cur += ch
    if cur or not toks:
        toks.append(cur)
    toks = [t for t in toks if t.strip()]
    if not toks:
        return [], None
    # 最后一段若是叶子 → leaf
    last = toks[-1].strip()
    if last.lower() in ("text", "textnodes", "owntext") or \
       re.match(r"^(href|src|value|alt|title|content|data-[\w\-]+|html|all)$", last, re.I):
        return toks[:-1], last
    return toks, None


def extract_chain(root, rule: str, want="str"):
    """统一入口。root: BeautifulSoup/Tag/字典/列表。want='str'|'nodes'|'values'。"""
    rule = (rule or "").strip()
    # 定位第一个 '##'(括号外)
    hp = _first_hash_hash(rule)
    base, tail = (rule[:hp], rule[hp:]) if hp >= 0 else (rule, "")
    # 顶层 && 拼接交给 extract_value;这里直接按 & 处理为并列
    # JSONPath
    if base.startswith("$"):
        vals = jsonpath_find(root, base)
        if want == "values":
            return vals
        txt = "".join(str(v) for v in vals if not isinstance(v, (dict, list)))
        return apply_replaces(txt, tail)
    if base.startswith("/"):
        steps = _convert_xpath(base)
        leaf = None
        if steps and steps[-1][0] == "attr":
            leaf = steps[-1][1]
            steps = steps[:-1]
        steps2 = [{"kind": "xptag", "name": s[1], "preds": s[2], "idx": s[3]} for s in steps]
        nodes = _run_chain(root, steps2, leaf)
        if want == "nodes":
            return [n for n in nodes if isinstance(n, Tag)]
        if leaf:
            return apply_replaces("".join(_leaf(n, leaf) for n in nodes if isinstance(n, Tag)), tail)
        return apply_replaces("".join("".join(n.stripped_strings) for n in nodes if isinstance(n, Tag)), tail)
    # 元素链 / 叶子自取 / 文字
    if base in ("text", "textNodes", "ownText") or base.lower() in ("textnodes", "owntext"):
        if isinstance(root, Tag):
            out = _leaf(root, base)
        else:
            out = ""
        return apply_replaces(out, tail)
    if base == "":
        return apply_replaces("", tail)
    is_selector = base.startswith(("class.", "id.", "tag.", "text.", "#", ".")) or "@" in base or _TAGLIKE.match(base)
    if not is_selector:
        return apply_replaces(base, tail)  # 字面量
    toks, leaf = _split_leaf(base)
    if not toks:
        if leaf:
            if isinstance(root, Tag):
                return apply_replaces(_leaf(root, leaf), tail)
        return ""
    steps = [_parse_token(t) for t in toks]
    nodes = _run_chain(root, steps, leaf)
    if want == "nodes":
        return [n for n in nodes if isinstance(n, Tag)]
    if leaf:
        out = "".join(_leaf(n, leaf) for n in nodes if isinstance(n, Tag))
    else:
        out = "".join("".join(n.stripped_strings) for n in nodes if isinstance(n, Tag))
    return apply_replaces(out, tail)


def _fill_vars(rule: str, root) -> str:
    """root 为 dict/list 时,把规则中的 {{$.xxx}} 用 JSONPath 取值替换。"""
    if not isinstance(root, (dict, list)):
        return rule

    def rep(m):
        expr = m.group(1).strip()
        if expr.startswith("$"):
            try:
                for v in jsonpath_find(root, expr):
                    if not isinstance(v, (dict, list)):
                        return str(v)
            except Exception:
                pass
        return m.group(0)

    return re.sub(r"\{\{([^{}]+)\}\}", rep, rule)


def extract_value(root, rule: str) -> str:
    """单值:支持 || 备选 与 && 拼接;dict/list 根支持 {{$.x}} 变量。"""
    rule = (rule or "").strip()
    if isinstance(root, (dict, list)):
        filled = _fill_vars(rule, root)
        txt = filled if filled != rule else rule
        if txt.startswith("$") or txt.startswith(".[") or txt.startswith("["):
            for v in jsonpath_find(root, txt):
                if not isinstance(v, (dict, list)):
                    return str(v)
            return ""
        if "{{$" in txt or "{{ $." in txt:
            return ""  # 变量无法解析(依赖 JS/上下文)
        return txt
    alts = _split_outside(rule, ["||"])
    for alt in alts:
        if not alt.strip():
            continue
        parts = _split_outside(alt, ["&&"])
        try:
            seg = "".join(extract_chain(root, p, "str") for p in parts if p.strip())
        except RuleError:
            continue
        if seg:
            return seg
    return ""


def extract_list(root, rule: str):
    """集合规则 → 节点列表。规则可为多条 ||。"""
    rule = (rule or "").strip()
    out = []
    for alt in _split_outside(rule, ["||"]):
        try:
            out.extend(extract_chain(root, alt, "nodes"))
        except RuleError:
            continue
    return out


def uses_js_text(text: str) -> bool:
    """粗略判断规则文本是否依赖 JS 脚本(误判可接受,倾向跳过)。"""
    if not text:
        return False
    return "<js>" in text or "@js" in text or "@js:" in text


# ========================================================== 正文 HTML→文本 ==
def fragment_to_text(inner_html: str) -> str:
    """章节 innerHTML → 可读纯文本(保留段落结构)。"""
    soup = parse_dom(inner_html or "")
    lines = []
    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "blockquote", "pre", "td", "th", "ul", "ol"}
    SKIP = {"script", "style", "noscript", "iframe", "ins"}

    def walk(node):
        if isinstance(node, NavigableString):
            lines.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        nm = (node.name or "").lower()
        if nm in SKIP:
            return
        if nm == "br":
            lines.append("\n")
            return
        if nm in BLOCK:
            lines.append("\n")
        for c in node.children:
            walk(c)
        if nm in BLOCK:
            lines.append("\n")

    for c in soup.children:
        walk(c)
    text = "".join(lines).replace("\xa0", " ")
    raw = text.split("\n")
    out = []
    for ln in raw:
        ln = ln.strip()
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    joined = "\n".join(out)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()

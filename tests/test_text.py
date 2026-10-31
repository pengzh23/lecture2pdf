from pathlib import Path

from lecture2pdf.notes import _split_text, _tag, normalize_math
from lecture2pdf.render import _undefined_macro
from lecture2pdf.transcript import fmt_time, import_transcript, slice_text

FIX = Path(__file__).parent / "fixtures"


def test_inline_math_spaces():
    assert normalize_math("设 $ x $ 为 $y$ 的函数") == "设 $x$ 为 $y$ 的函数"


def test_paren_bracket_to_dollar():
    assert normalize_math(r"\(E(B)\) 是投影") == "$E(B)$ 是投影"
    assert normalize_math(r"公式\[ E^2=E \]结束") == "公式\n\n$$\nE^2=E\n$$\n\n结束"


def test_escaped_dollar_untouched():
    assert normalize_math(r"价格 \$5 与 $ d $") == r"价格 \$5 与 $d$"


def test_strip_code_fence():
    assert normalize_math("```markdown\n## 标题\n```") == "## 标题"


def test_tag():
    assert _tag("<board>\nabc\n</board><terms>x</terms>", "board") == "abc"
    assert _tag("<board>未闭合", "board") == "未闭合"


def test_split_text():
    parts = _split_text("一句话。" * 100, 50)
    assert all(len(p) <= 50 for p in parts) and "".join(parts) == "一句话。" * 100


def test_undefined_macro():
    err = "! Undefined control sequence.\nl.148   \\(\\undefinedmacro\n"
    assert _undefined_macro(err) == "undefinedmacro"


def test_fmt_time():
    assert fmt_time(65) == "01:05"


def test_import_srt_and_slice():
    segs = import_transcript(FIX / "sample.srt")
    assert len(segs) == 3 and segs[0]["text"] == "好，咱们上课。"
    text = slice_text(segs, 0, 10)
    assert "咱们上课" in text and "谱定理" not in text


def test_merge_sentences():
    from lecture2pdf.transcript import _merge_sentences

    segs = _merge_sentences([(0, 2, "我们来看，"), (2, 4, "自伴算子。"), (4, 40, "很长的一句话"), (40, 41, "结尾")])
    assert [s["text"] for s in segs] == ["我们来看，自伴算子。", "很长的一句话", "结尾"]
    assert segs[0]["start"] == 0 and segs[0]["end"] == 4


def test_parse_outline():
    from lecture2pdf.notes import _parse_outline

    raw = '好的：{"title": "T", "chapters": [{"title": "A", "sections": [1, 2]}, {"title": "B", "sections": [5]}], "skip": [3]}'
    o = _parse_outline(raw, [1, 2, 3, 4, 5])
    assert o["skip"] == [3] and o["chapters"][0]["sections"] == [1, 2, 4]  # 漏掉的 4 并入前一章
    assert _parse_outline('{"chapters": [{"title": "A", "sections": [1, 1]}]}', [1]) is None
    assert _parse_outline("not json", [1]) is None


def test_fix_list_breaks():
    from lecture2pdf.render import fix_list_breaks

    assert fix_list_breaks("段落。\n- 项一\n- 项二") == "段落。\n\n- 项一\n- 项二"
    assert fix_list_breaks("$$\n- x\n$$") == "$$\n- x\n$$"
    assert fix_list_breaks("文字\n1. 第一") == "文字\n\n1. 第一"


def test_indented_display_math():
    from lecture2pdf.render import fix_list_breaks

    md = normalize_math("- 定义：\n  $$\n  a=b\n  $$\n  说明\n- 下一项")
    assert "\n  $$\n  a=b\n  $$" in md
    broken = "- 定义：\n  \n\n$$\n  a=b\n  $$\n\n  说明\n- 下一项"  # 旧版缓存
    fixed = fix_list_breaks(broken)
    assert "\n  $$\n  a=b\n  $$" in fixed and "说明\n\n- 下一项" in fixed


def test_scrub_meta():
    from lecture2pdf.notes import find_meta, scrub_meta

    md = ("对 Borel 集 $B$（原板书此处不清），$f$ 的取值落在 $B$ 中。讲解在此处未展开具体表达式。\n"
          "  （原板书中 $\\dot p_i$ 后的式子不清；讲解给出上式。）\n"
          "在 $U$ 上给定[推断: 概率]测度。由此可推断 $f$ 可测。\n"
          "$$\nx (板书)\n$$\n"
          "## 标题")
    out = scrub_meta(md)
    assert "板书" not in out.split("$$")[0] and "讲解" not in out
    assert "给定概率测度" in out and "由此可推断 $f$ 可测" in out  # 正文中的「推断」保留
    assert "$$\nx (板书)\n$$" in out  # 公式块不动
    assert find_meta(out) == []
    assert find_meta("老师强调 $a$ 成立") == ["老师强调 $a$ 成立"]


def test_balance_divs():
    from lecture2pdf.render import balance_divs

    md = "文字\n::: {.definition title=\"X\"}\n内容\n## 下一节\n:::\n尾"
    out = balance_divs(md)
    assert out == '文字\n\n::: {.definition title="X"}\n\n内容\n\n:::\n\n## 下一节\n尾\n'
    assert balance_divs("::: proof\n证毕") == "::: proof\n\n证毕\n\n:::\n"


def test_clean_title():
    from lecture2pdf.render import clean_title

    assert clean_title("量子力学的数学方法_第一周第1次课") == "量子力学的数学方法 · 第一周第1次课"


def test_outline_intro():
    from lecture2pdf.notes import _parse_outline

    o = _parse_outline('{"title": "T", "intro": "提要", "chapters": [{"title": "A", "sections": [1]}]}', [1])
    assert o["intro"] == "提要" and o["skip"] == []


def test_lua_filter_latex():
    import subprocess

    from lecture2pdf.render import LUA_FILTER, pandoc_bin

    md = '::: {.theorem title="Riesz 表示定理"}\n设 $H$ 为 Hilbert 空间。\n:::\n\n::: proof\n显然。\n:::\n'
    r = subprocess.run([pandoc_bin(), "-f", "markdown", "-t", "latex", "--lua-filter", str(LUA_FILTER)],
                       input=md, capture_output=True, text=True, check=True)
    assert "\\begin{theorem}{Riesz 表示定理}{}" in r.stdout and "\\end{theorem}" in r.stdout
    assert "\\begin{proofbox}" in r.stdout and "证明.}\\quad{}显然" in r.stdout


def test_llm_retries_when_truncated(monkeypatch):
    from lecture2pdf.config import LLMEndpoint
    from lecture2pdf.llm import LLMClient

    c = LLMClient(LLMEndpoint("k", "http://localhost", "m"), "测试")
    calls = []

    def fake(messages, temperature, max_tokens):
        calls.append(max_tokens)
        return ("完整" if max_tokens > 8192 else "截断"), 1, 1, max_tokens <= 8192

    monkeypatch.setattr(c, "_chat", fake)
    assert c.chat("s", "u") == "完整" and calls == [8192, 16384]


def test_llm_retries_when_reasoning_eats_budget(monkeypatch):
    from lecture2pdf.config import LLMEndpoint
    from lecture2pdf.llm import LLMClient

    c = LLMClient(LLMEndpoint("k", "http://localhost", "m"), "测试")
    # 推理模型把额度全用在思考上：正文为空、finish_reason=length
    monkeypatch.setattr(c, "_chat", lambda m, t, mt: ("正文" if mt >= 16384 else "", 1, 1, mt < 16384))
    assert c.chat("s", "u") == "正文"

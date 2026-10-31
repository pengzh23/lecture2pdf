"""
汇总为 Markdown，并用 pandoc 生成 PDF（xelatex 优先，失败回退 typst），同时输出 HTML。

- handout.md / .pdf：正式讲义，只含知识内容（排版样式见 templates/preamble.tex）。
- handout_full.md / .pdf（--full）：按版面顺序的详细稿，附板书原图与时间，用于核对。
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import shutil
import subprocess
from pathlib import Path

from .keyframes import Keyframe
from .transcript import fmt_time

logger = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
PREAMBLE = TEMPLATES / "preamble.tex"
LUA_FILTER = TEMPLATES / "theorems.lua"

HTML_CSS = """<style>
body{max-width:52em;margin:auto;padding:1em 2em;font-family:serif;line-height:1.7}
h1,h2,h3,h4{color:#2d5fb4} h1{text-align:center}
.l2p-box{border:1px solid #9ab;border-radius:6px;padding:.2em 1em;margin:1em 0;background:#f7fafd}
.definition{border-color:#009646;background:#f3fbf6}.theorem{border-color:#e66e14;background:#fff8f2}
.postulate,.axiom{border-color:#7846aa;background:#f8f4fc}
.example,.remark,.proof{border:none;background:none;padding:0}
</style>"""


def pandoc_bin() -> str | None:
    exe = shutil.which("pandoc")
    if exe:
        return exe
    try:
        import pypandoc

        return pypandoc.get_pandoc_path()
    except Exception:  # noqa: BLE001
        return None


def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def clean_title(title: str) -> str:
    """视频文件名中的下划线换成分隔点，合并多余空白。"""
    return re.sub(r"\s+", " ", re.sub(r"\s*_+\s*", " · ", title)).strip(" ·")


def _header(doc_title: str, subtitle: str, cjk_font: str, main_font: str,
            abstract: str = "") -> str:
    today = _dt.date.today()
    lines = [
        "---",
        f"title: {_yaml_str(doc_title)}",
    ]
    if subtitle:
        lines.append(f"subtitle: {_yaml_str(subtitle)}")
    lines += [
        f"date: {_yaml_str(f'{today.year} 年 {today.month} 月 {today.day} 日')}",
        "documentclass: ctexart",
        # 未指定中文字体时用 TeX Live 自带的 Fandol 字体（有粗体），保证跨平台可编译
        f"classoption: [{'fontset=none' if cjk_font else 'fontset=fandol'}]",
        "papersize: a4",
        "geometry: [top=2.5cm, bottom=2.5cm, left=2.6cm, right=2.6cm]",
        "fontsize: 11pt",
        "linestretch: 1.35",
        "indent: true",
        "toc: true",
        "toc-depth: 2",
        "numbersections: true",
        "secnumdepth: 2",
        "colorlinks: true",
        "linkcolor: l2pblue",
        "urlcolor: l2pblue",
    ]
    if cjk_font:
        lines += [f"CJKmainfont: {_yaml_str(cjk_font)}", "CJKoptions: [AutoFakeBold=2]"]
    if main_font:
        lines.append(f"mainfont: {_yaml_str(main_font)}")
    if abstract.strip():
        lines.append("abstract: |")
        lines += ["  " + ln if ln.strip() else "" for ln in abstract.strip().splitlines()]
    lines += ["---", "", "```{=latex}", "\\clearpage", "```", ""]
    return "\n".join(lines)


def build_handout(work: Path, doc_title: str, video_title: str, intro: str, chapters: list[str],
                  cjk_font: str, main_font: str) -> Path:
    """正式讲义：各章知识内容，不含图片、时间戳、文字稿。"""
    md = _header(doc_title, clean_title(video_title), cjk_font, main_font, intro)
    md += "\n\n".join(c.strip() for c in chapters if c.strip()) + "\n"
    path = work / "handout.md"
    path.write_text(_finalize(md), encoding="utf-8")
    return path


def build_full(work: Path, doc_title: str, video_title: str, source: str, overview: str,
               kfs: list[Keyframe], sections: dict[int, str], corrected: dict[int, str],
               with_images: bool, appendix: bool, cjk_font: str, main_font: str) -> Path:
    """核对版：按版面顺序的全部小节，附板书原图与时间；可选附录文字稿。"""
    parts = [_header(doc_title, "详细稿 · " + clean_title(video_title), cjk_font, main_font)]
    if source:
        src = source if source.startswith("http") else Path(source).name
        parts.append(f"> 来源：{src}\n")
    if overview:
        parts.append(overview + "\n")
    for kf in kfs:
        sec = sections.get(kf.index, "").strip()
        if not sec:
            continue
        parts.append(sec + "\n")
        if with_images:
            cap = f"板书（{fmt_time(kf.time)}，对应 {fmt_time(kf.seg_start)}–{fmt_time(kf.seg_end)}）"
            parts.append(f"![{cap}]({kf.image}){{width=100%}}\n")
    if appendix and any(corrected.values()):
        parts.append("## 附录：课堂文字稿\n")
        for kf in kfs:
            t = corrected.get(kf.index, "").strip()
            if t:
                parts.append(f"### {fmt_time(kf.seg_start)}–{fmt_time(kf.seg_end)}\n\n{t}\n")
    path = work / "handout_full.md"
    path.write_text(_finalize("\n".join(parts)), encoding="utf-8")
    return path


def _finalize(md: str) -> str:
    return fix_list_breaks(balance_divs(_demote_stray_h1(md)))


def _demote_stray_h1(md: str) -> str:
    """模型偶尔会输出一级标题，统一降为二级（渲染时 ## 对应 LaTeX 的 \\section）。"""
    out, in_yaml = [], False
    for i, line in enumerate(md.splitlines()):
        if line.strip() == "---" and (i == 0 or in_yaml):
            in_yaml = not in_yaml
        if not in_yaml and line.startswith("# "):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


_DIV_OPEN = re.compile(r"^\s*:{3,}\s*(\{[^}]*\}|[A-Za-z][\w-]*)\s*$")
_DIV_CLOSE = re.compile(r"^\s*:{3,}\s*$")


def balance_divs(md: str) -> str:
    """
    整理 ::: 块：围栏前后补空行（pandoc 要求），未闭合的块在下一个标题前或文末补上 :::，
    多余的 ::: 删除；不支持嵌套（模型输出的块不需要嵌套）。
    """
    out: list[str] = []
    depth = 0
    in_math = False

    def fence(line: str) -> None:
        if out and out[-1].strip():
            out.append("")
        out.append(line)
        out.append("")

    for line in md.split("\n"):
        s = line.strip()
        if s == "$$" or (s.startswith("$$") and s.count("$$") == 1):
            in_math = not in_math
        if not in_math and _DIV_OPEN.match(line):
            if depth:
                fence(":::")
            fence(":::" + " " + _DIV_OPEN.match(line).group(1))
            depth = 1
            continue
        if not in_math and _DIV_CLOSE.match(line):
            if depth:
                fence(":::")
                depth = 0
            continue
        if not in_math and depth and re.match(r"^#{1,6} ", line):
            fence(":::")
            depth = 0
        out.append(line)
    if depth:
        fence(":::")
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "\n".join(out)).strip() + "\n"


def render_pdf(work: Path, md_path: Path, engine: str = "xelatex") -> Path | None:
    pandoc = pandoc_bin()
    if not pandoc:
        logger.error("未找到 pandoc（pip install pypandoc_binary 或系统安装 pandoc）")
        return None
    stem = md_path.stem
    pdf = work / f"{stem}.pdf"
    base = [pandoc, md_path.name, "-f", "markdown", "--resource-path", str(work),
            "--shift-heading-level-by=-1", "--lua-filter", str(LUA_FILTER)]

    # HTML（MathJax）——便于在浏览器里快速查看
    logger.info(f"生成 {stem}.html…")
    css = work / "handout_style.html"
    css.write_text(HTML_CSS, encoding="utf-8")
    try:
        subprocess.run(base + ["-s", "--mathjax", "--toc", "--embed-resources", "-H", css.name,
                               "-o", f"{stem}.html"], cwd=work, capture_output=True, text=True,
                       timeout=180)
    except subprocess.TimeoutExpired:
        logger.warning("生成 HTML 超时（内嵌 MathJax 需要联网），已跳过")

    engines = [engine] + [e for e in ("xelatex", "typst") if e != engine]
    patch = work / "latex_preamble.tex"
    patched: list[str] = []
    patch.write_text(PREAMBLE.read_text(encoding="utf-8"), encoding="utf-8")
    err = ""
    for eng in engines:
        if eng in ("xelatex", "lualatex") and not shutil.which(eng):
            continue
        if eng == "typst" and not shutil.which("typst"):
            continue
        logger.info(f"生成 {stem}.pdf（{eng}）…")
        is_tex = eng in ("xelatex", "lualatex")
        for _attempt in range(8 if is_tex else 1):
            cmd = base + ["-o", pdf.name, f"--pdf-engine={eng}"]
            if is_tex:
                cmd += ["-H", patch.name]
            r = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
            if r.returncode == 0 and pdf.exists():
                if patched:
                    logger.warning("以下未定义的 LaTeX 宏已用占位定义代替（显示为正体宏名），"
                                   "建议在 Markdown 中修正：" + " ".join("\\" + m for m in patched))
                return pdf
            err = (r.stderr or "").strip()
            macro = _undefined_macro(err) if is_tex else None
            if not macro or macro in patched:
                break
            patched.append(macro)
            with patch.open("a", encoding="utf-8") as f:
                f.write(f"\\providecommand{{\\{macro}}}{{\\ensuremath{{\\operatorname{{{macro}}}}}}}\n")
            logger.info(f"  未定义的宏 \\{macro}，补占位定义后重试")
        (work / f"render_{eng}.log").write_text(err, encoding="utf-8")
        logger.warning(f"{eng} 失败（详见 render_{eng}.log）：{_short_err(err)}")
        if is_tex:
            # 留一份 .tex 方便手工修改后编译
            subprocess.run(base + ["-s", "-H", patch.name, "-o", f"{stem}.tex"], cwd=work,
                           capture_output=True)
    return None


def _undefined_macro(err: str) -> str | None:
    """从「! Undefined control sequence.\nl.148 ...\\foo」中取出 foo。"""
    m = re.search(r"Undefined control sequence\.\s*\n\s*l\.\d+ (.*)", err)
    if not m:
        return None
    names = re.findall(r"\\([A-Za-z]+)", m.group(1))
    return names[-1] if names else None


def _short_err(err: str) -> str:
    m = re.search(r"^! .*$", err, re.M)
    if m:
        return m.group(0)[:200]
    return err[-300:]


_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def fix_list_breaks(md: str) -> str:
    """pandoc 要求列表前有空行，否则「段落\\n- 项」会被并成一段；在段落与列表之间补空行。"""
    lines = md.split("\n")
    # 1) 公式块开头的 $$ 与结尾缩进不一致（旧版规范化把列表内公式的开头挤到了行首）→ 按结尾补齐
    opens = [i for i, ln in enumerate(lines) if ln.strip() == "$$"]
    for a, b in zip(opens[::2], opens[1::2], strict=False):
        ind = lines[b][: len(lines[b]) - len(lines[b].lstrip())]
        if ind and not lines[a].startswith(ind):
            lines[a] = ind + "$$"
            if a > 0 and not lines[a - 1].strip():
                lines[a - 1] = ""
    # 2) 列表前补空行
    out: list[str] = []
    in_math = False
    for line in lines:
        if line.strip() == "$$":
            in_math = not in_math
        if (not in_math and _LIST_RE.match(line) and out and out[-1].strip()
                and not _LIST_RE.match(out[-1])):
            out.append("")
        out.append(line)
    return "\n".join(out)

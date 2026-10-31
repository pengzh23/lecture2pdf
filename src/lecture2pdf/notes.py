"""
AI 部分：视觉识别板书 → 用板书校正文字稿 → 生成讲义小节 → 按主题归并为正式讲义。

所有模型输出都缓存在工作目录（vision/、corrected/、sections/、overview.md），
重跑时已存在的文件直接复用；想重新生成某一步就删掉对应目录或使用 --force。
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from .keyframes import Keyframe
from .llm import LLMClient, parallel_map
from .transcript import Segment, fmt_time, slice_text

logger = logging.getLogger(__name__)

# ======================================================================= prompts

VISION_SYSTEM = """你是一名严谨的理工科助教，负责把课堂视频截图中的板书或 PPT 准确转写成文字。
要求：
1. 只转写画面上真实存在的内容，按版面从左到右、从上到下的阅读顺序；黑板分栏时按栏输出，栏之间用一行 `---` 分隔。
2. 数学公式一律用 LaTeX：行内 $...$，独立成行的用 $$...$$。不要用 \\( \\) 或 \\[ \\]。
3. 截图分辨率有限、可能有反光或被人遮挡。请结合「同时段讲解文字稿」判断模糊的字符（例如文字稿说"算子"，板书上像"箕子"的应为"算子"）。
4. 实在无法辨认的写 [?]；根据讲解推断补出的内容写成 [推断: ...]。宁可标注，不要编造。
5. 讲解文字稿来自语音识别，可能有同音错字，它只是辅助，以画面为准。"""

VISION_USER = """课程：{title}
截图时刻：{time}（该版面对应的讲解时段 {start}–{end}）

【同时段讲解文字稿（语音识别，可能有错字）】
{transcript}

请按下面格式输出，不要输出其他内容：
<board>
（板书/PPT 的完整转写，Markdown + LaTeX）
</board>
<terms>
（画面中出现的专业术语、人名、符号名称，每行一个，用于纠正语音识别错误）
</terms>"""

CORRECT_SYSTEM = """你是课堂录音文字稿的校对员。文字稿由语音识别生成，常见问题：同音/近音错字（尤其是专业术语和符号）、缺标点、繁简混杂、口头语重复。
请参照同时段的板书内容逐句校正：
- 修正错字和术语，统一为简体中文，补全标点，合理分段。
- 口头的数学表达可以保留口语，也可以在关键处补一个 LaTeX 行内公式（$...$）。
- 去掉无意义的口头禅和重复（"那个那个""对吧对吧"），但不得删减任何实际讲解内容，不得添加原文没有的内容。
- 保留形如 [12:34] 的时间标记，放在对应段落开头。
只输出校正后的文字稿，用 <transcript></transcript> 包裹。"""

CORRECT_USER = """课程：{title}

【板书内容】
{board}

【术语表】
{terms}

【待校正的文字稿（{start}–{end}）】
{transcript}"""

# 讲义正文的写作规范：小节（notes）与精简版（concise）共用
STYLE_RULES = """【体例】
- 正文以连贯的书面段落为主，像一本教材；列表只用于确实并列的条目，不要把内容写成要点堆砌。
- 定义、定理等用下面的块书写（title 可省略，可含 $...$ 公式；块不能嵌套，块内不要再写「定义：」之类的标签）：
::: {{.definition title="名称"}}
内容
:::
  可用类型：definition（定义）、theorem（定理）、proposition（命题）、property（性质）、lemma（引理）、
  corollary（推论）、postulate（公设/公理，title 写完整名称，如「公设一（态空间）」）、example（例）、
  remark（注）、proof（证明，紧跟在被证明的结论之后）。块的开始行与结束行 `:::` 前后各空一行。
- 数学公式一律用 LaTeX：行内 $...$，独立成行的用 $$...$$（多行用 $$\\begin{{aligned}} ... \\end{{aligned}}$$）。
  不要用 \\( \\) \\[ \\]，不要用 align/equation 环境。
【措辞】讲义只陈述知识本身，读者看不到也不关心素材的来源：
- 禁止出现「板书」「讲解」「文字稿」「语音」「转写」「识别」「视频」「画面」「截图」「老师」「课堂上」「本节课」「原文」
  「原讲义」等描述素材或整理过程的词，也不要写「（存疑）」「（不清）」「（推断）」之类的标注。
- 素材中标 [推断: ...] 的内容直接采用；标 [?] 的地方，若能由上下文和数学常识确定就直接写出正确内容，否则略去该细节，不作说明。
- 不写「本节讲了」「下面我们来看」之类的套话和总结性的空话；不得编造素材中没有的结论。"""

NOTES_SYSTEM = """你是一名优秀的大学课程助教，要把一段课堂内容整理成讲义中的一节。
输入是本段的板书转写和讲课文字稿（已校对），二者都只是素材。
内容要求：
1. 以板书为骨架，用文字稿补充动机、直觉、推导步骤、例子和需要注意的地方。板书上写了但没有展开的，忠实保留；
   文字稿中有而板书没写的重要内容（口头推导、举例、提醒）也要整理进去。
2. 结构：第一行是二级标题 `## 小节标题`；下面可用 `###` 小标题。
3. 「上一版面」是本段之前已经整理过的板书（可能是同一块黑板换了机位，或只擦掉了一部分）。
   其中已经出现过的内容不要重复整理，必要时一句话引用。
4. 只有课程安排、作业、考试、闲聊的段落，用一两句话简要记下即可。

""" + STYLE_RULES + """

使用{lang}。只输出讲义 Markdown，不要用代码块包裹。"""

NOTES_USER = """课程：{title}
本段时间：{start}–{end}

【上一版面（已整理过，避免重复）】
{prev_board}

【本段板书】
{board}

【本段讲解文字稿】
{transcript}"""

OVERVIEW_SYSTEM = """你根据一节课各小节的讲义，为整份讲义写开头部分。只陈述知识，不提及视频、板书、老师、课堂等素材来源。
只输出 Markdown，不要用代码块包裹。"""

OVERVIEW_USER = """课程视频标题：{title}

下面是本节课各小节讲义（按时间顺序）：
{sections}

请输出：
第一行：`TITLE: ` 后接一个简洁的讲义标题（体现本节主题，不超过 30 字）。
然后输出：
## 内容提要
（3–6 句话的概述）

### 知识要点
（5–10 条列表，关键公式用 LaTeX）

使用{lang}。"""

LANG_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}


# ======================================================================= helpers

def _tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(.*?)\s*(?:</{tag}>|\Z)", text, re.S)
    return m.group(1).strip() if m else ""


def normalize_math(md: str) -> str:
    """把 \\( \\) \\[ \\] 统一为 $ / $$；去掉模型偶尔加的 ```markdown 包裹。"""
    md = re.sub(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", r"\1", md.strip(), flags=re.S)
    md = re.sub(r"\\\[(.+?)\\\]", lambda m: f"\n$$\n{m.group(1).strip()}\n$$\n", md, flags=re.S)
    md = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1).strip()}$", md, flags=re.S)
    # 独立公式 $$...$$ 前后需要空行，否则 pandoc 可能当成行内
    # 保留行首缩进：列表项内的公式块靠缩进归属于该项
    md = re.sub(r"(^[ \t]*)?\$\$(.+?)\$\$\n?",
                lambda m: f"\n\n{m.group(1) or ''}$${m.group(2)}$$\n\n", md, flags=re.S | re.M)
    md = _strip_inline_math_spaces(md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _strip_inline_math_spaces(md: str) -> str:
    """
    pandoc 要求行内公式 $...$ 的内侧紧贴内容（`$ x $` 不会被识别为公式）。
    按顺序成对扫描每一行的单个 $，跳过 $$ 块和转义的 \\$，只修剩公式内侧的空白。
    """
    out_lines = []
    in_block = False
    for line in md.split("\n"):
        if line.strip().startswith("$$"):
            # 单独成行的 $$（块公式边界）；同一行开合的 $$...$$ 不改变状态
            if line.strip() == "$$" or line.strip().count("$$") == 1:
                in_block = not in_block
            out_lines.append(line)
            continue
        if in_block or "$" not in line:
            out_lines.append(line)
            continue
        parts = re.split(r"(?<!\\)\$", line)
        if len(parts) % 2 == 0:  # $ 不成对，不动
            out_lines.append(line)
            continue
        for i in range(1, len(parts), 2):
            parts[i] = parts[i].strip()
        out_lines.append("$".join(parts))
    return "\n".join(out_lines)


# 描述素材来源 / 整理过程的词。讲义中不应出现，scrub_meta 会删掉含这些词的括注和纯说明句。
META_WORDS = ("板书", "讲解", "文字稿", "语音", "转写", "视频", "画面", "截图", "老师", "存疑",
              "课堂上", "本节课", "原讲义")
_META_RE = "|".join(META_WORDS)
# 括注里额外匹配的词（这些词单独出现在正文中可能是正常用法，只在括注中删除）
_PAREN_RE = re.compile(rf"[（(][^（）()\n]*?(?:{_META_RE}|不清|推断|原文|\[\?\])[^（）()\n]*?[）)]")
_INFER_RE = re.compile(r"\[推断[:：]\s*([^\]]*)\]")
_UNESC_DOLLAR = re.compile(r"(?<!\\)\$")


def _outside_math(line: str, pos: int) -> bool:
    return len(_UNESC_DOLLAR.findall(line[:pos])) % 2 == 0


def _scrub_line(line: str) -> str:
    line = _INFER_RE.sub(r"\1", line)

    def paren(m: re.Match) -> str:
        if _outside_math(line, m.start()) and len(_UNESC_DOLLAR.findall(m.group(0))) % 2 == 0:
            return ""
        return m.group(0)

    line = _PAREN_RE.sub(paren, line)
    # 删除不含公式、只在说明素材情况的句子（如「讲解在此处未展开具体表达式。」）
    prefix = re.match(r"^(\s*(?:[-*+]|\d+[.)])?\s*)", line).group(1)
    body = line[len(prefix):]
    sents = re.findall(r"[^。！？；]*[。！？；]?", body)
    kept = [x for x in sents if x and ("$" in x or not re.search(_META_RE, x))]
    body = "".join(kept)
    if "$" in body and body.count("$") % 2 == 0:
        parts = _UNESC_DOLLAR.split(body)
        body = "$".join(p if i % 2 else p.replace("[?]", "") for i, p in enumerate(parts))
    elif "$" not in body:
        body = body.replace("[?]", "")
    body = re.sub(r"[（(]\s*[）)]", "", body)
    if not body.strip():
        return "" if prefix.strip() or not line.strip() else body
    return prefix + body


def scrub_meta(md: str) -> str:
    """去掉讲义中描述素材来源 / 整理过程的括注和说明句，[推断: x] → x，删去 [?]。公式块与标题不动。"""
    out, in_math = [], False
    for line in md.split("\n"):
        s = line.strip()
        if s == "$$" or (s.startswith("$$") and s.count("$$") == 1):
            in_math = not in_math
            out.append(line)
            continue
        if in_math or not s or s.startswith(("#", ":::", "$$", "![", "```")):
            out.append(line)
            continue
        new = _scrub_line(line)
        if new.strip() or not s:
            out.append(new)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def find_meta(md: str) -> list[str]:
    """返回仍含过程性词汇的行（用于日志提示）；忽略公式与图片。"""
    hits, in_math = [], False
    for ln in md.splitlines():
        s = ln.strip()
        if s == "$$" or (s.startswith("$$") and s.count("$$") == 1):
            in_math = not in_math
            continue
        if in_math or s.startswith("!["):
            continue
        if re.search(_META_RE, re.sub(r"\$[^$]*\$", "", ln)):
            hits.append(s)
    return hits


def prepare_image(path: Path, max_side: int, quality: int) -> Path:
    """发送前缩放/压缩，避免请求体过大导致上传超时（408）。"""
    from PIL import Image

    out = path.with_name(path.stem + f".send{max_side}.jpg")
    if out.exists():
        return out
    im = Image.open(path).convert("RGB")
    scale = max_side / max(im.size)
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    out.write_bytes(buf.getvalue())
    return out


def _read(p: Path) -> str | None:
    return p.read_text(encoding="utf-8") if p.exists() and p.stat().st_size else None


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ======================================================================= steps

def run_vision(work: Path, kfs: list[Keyframe], segs: list[Segment], title: str,
               client: LLMClient | None, s) -> dict[int, dict]:
    """返回 {index: {"board": str, "terms": [str]}}；client 为 None 时为纯文字模式。"""
    out_dir = work / "vision"

    def one(kf: Keyframe) -> tuple[int, dict]:
        cache = out_dir / f"kf_{kf.index:02d}.txt"
        raw = _read(cache)
        if raw is None:
            if client is None:
                return kf.index, {"board": "", "terms": []}
            text = slice_text(segs, kf.seg_start, kf.seg_end)
            if len(text) > s.max_transcript_chars:
                text = text[: s.max_transcript_chars // 2] + "\n……\n" + text[-s.max_transcript_chars // 2:]
            img = prepare_image(work / kf.image, s.vision_max_side, s.vision_quality)
            raw = client.chat(
                VISION_SYSTEM,
                VISION_USER.format(title=title, time=fmt_time(kf.time), start=fmt_time(kf.seg_start),
                                   end=fmt_time(kf.seg_end), transcript=text or "（无）"),
                images=[img],
            )
            _write(cache, raw)
            logger.info(f"  [视觉] 关键帧 {kf.index}/{len(kfs)} 完成")
        board = normalize_math(_tag(raw, "board") or raw)
        terms = [t.strip(" -*•\t") for t in _tag(raw, "terms").splitlines() if t.strip(" -*•\t")]
        return kf.index, {"board": board, "terms": terms}

    return dict(parallel_map(one, kfs, s.concurrency))


def run_correct(work: Path, kfs: list[Keyframe], segs: list[Segment], title: str,
                boards: dict[int, dict], client: LLMClient, s) -> dict[int, str]:
    out_dir = work / "corrected"

    def one(kf: Keyframe) -> tuple[int, str]:
        cache = out_dir / f"seg_{kf.index:02d}.md"
        done = _read(cache)
        if done is not None:
            return kf.index, done
        raw_text = slice_text(segs, kf.seg_start, kf.seg_end)
        if not raw_text:
            _write(cache, "")
            return kf.index, ""
        b = boards.get(kf.index, {})
        pieces = _split_text(raw_text, s.max_transcript_chars)
        fixed = []
        for p in pieces:
            r = client.chat(CORRECT_SYSTEM, CORRECT_USER.format(
                title=title, board=b.get("board") or "（无）",
                terms="\n".join(b.get("terms") or []) or "（无）",
                start=fmt_time(kf.seg_start), end=fmt_time(kf.seg_end), transcript=p))
            fixed.append(_tag(r, "transcript") or r)
        text = normalize_math("\n\n".join(fixed))
        _write(cache, text)
        logger.info(f"  [校正] 第 {kf.index}/{len(kfs)} 段完成")
        return kf.index, text

    return dict(parallel_map(one, kfs, s.concurrency))


def run_notes(work: Path, kfs: list[Keyframe], title: str, boards: dict[int, dict],
              corrected: dict[int, str], client: LLMClient, s) -> dict[int, str]:
    out_dir = work / "sections"
    lang = LANG_NAMES.get(s.note_language, s.note_language)

    def one(kf: Keyframe) -> tuple[int, str]:
        cache = out_dir / f"sec_{kf.index:02d}.md"
        done = _read(cache)
        if done is not None:
            return kf.index, scrub_meta(done)
        prev = boards.get(kf.index - 1, {}).get("board") or "（无）"
        board = boards.get(kf.index, {}).get("board") or "（未识别板书，仅依据讲解整理）"
        transcript = corrected.get(kf.index) or "（本段无讲解）"
        if len(transcript) > s.max_notes_input_chars:
            logger.warning(f"  第 {kf.index} 段文字稿较长（{len(transcript)} 字），已截断到 "
                           f"{s.max_notes_input_chars} 字；可减小 --max-gap 让分段更细")
            transcript = transcript[: s.max_notes_input_chars]
        r = client.chat(
            NOTES_SYSTEM.format(lang=lang),
            NOTES_USER.format(title=title, start=fmt_time(kf.seg_start), end=fmt_time(kf.seg_end),
                              prev_board=prev, board=board, transcript=transcript),
            temperature=0.3,
        )
        md = scrub_meta(normalize_math(r))
        if not md.lstrip().startswith("## "):
            md = f"## 第 {kf.index} 部分\n\n" + md
        _write(cache, md)
        logger.info(f"  [讲义] 第 {kf.index}/{len(kfs)} 节完成")
        return kf.index, md

    return dict(parallel_map(one, kfs, s.concurrency))


def run_overview(work: Path, title: str, sections: dict[int, str], client: LLMClient,
                 s) -> tuple[str, str]:
    """返回 (讲义标题, 概要 Markdown)。"""
    cache = work / "overview.md"
    raw = _read(cache)
    if raw is None:
        joined = "\n\n".join(sections[k] for k in sorted(sections))
        if len(joined) > 60000:
            # 太长时只给每节的前若干字
            joined = "\n\n".join(sections[k][:2500] for k in sorted(sections))
        raw = client.chat(OVERVIEW_SYSTEM, OVERVIEW_USER.format(
            title=title, sections=joined, lang=LANG_NAMES.get(s.note_language, s.note_language)))
        _write(cache, raw)
    m = re.match(r"\s*TITLE:\s*(.+)", raw)
    doc_title = m.group(1).strip().strip("#* ") if m else title
    body = raw[m.end():] if m else raw
    return doc_title, scrub_meta(normalize_math(body))


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[。！？!?\n])", text):
        if len(cur) + len(sent) > limit and cur:
            parts.append(cur)
            cur = ""
        cur += sent
    if cur:
        parts.append(cur)
    return parts


# ======================================================================= 正式讲义（归并、精简）

OUTLINE_SYSTEM = """你是大学课程助教。下面是一节课按时间顺序整理出的讲义小节（每节只给出开头部分）。
小节是按黑板版面切分的，同一主题常被拆成多节，也有一些小节只是课程安排、作业通知、闲聊或与课程知识无关的内容。
请为一份「只保留知识」的讲义规划结构：
1. 把讲同一主题的相邻小节归并为一章，章的顺序与讲课顺序一致，章数一般为 4~10 个；章标题简洁，像教材的节标题。
2. 只包含课程安排、作业、考试、闲聊、设备调试等、没有课程知识的小节，放进 skip。
3. 每个小节编号必须且只能出现一次（在某一章或 skip 中）。
4. title 是整份讲义的标题（体现本讲主题，不超过 30 字）；intro 是 2~4 句话的内容提要，
   用书面语概述本讲的主要内容及其联系，不提及课堂、视频、老师等。
只输出 JSON，不要输出其他内容，格式：
{"title": "讲义标题", "intro": "内容提要", "chapters": [{"title": "章标题", "sections": [1, 2]}], "skip": [3]}"""

CONCISE_SYSTEM = """你是优秀的大学课程助教，要把一章课堂讲义改写为精炼的复习讲义，风格接近数学教材。
保留：定义、定理/命题（含条件）、关键公式、必要的推导或证明思路、帮助理解的核心例子、重要的注意事项。
删除：课程安排、作业、闲聊、口语化的过渡和重复表述、时间戳、图片、与知识无关的背景故事。
结构：
1. 第一行是二级标题 `## 章标题`；可先用一两句话交代本章内容或动机，下面用 `###` 小标题组织；同一内容只出现一次，合并重复表述。
2. 「前面各章」已经讲过的定义和公式不要重复写出，需要时一句话引用。
3. 篇幅约为原文的 1/3 到 1/2，简洁准确。

""" + STYLE_RULES + """

使用{lang}。只输出 Markdown，不要用代码块包裹。"""

CONCISE_USER = """课程：{title}
本章标题（可微调）：{chapter}
前面各章：{prev}

【本章原始讲义】
{body}"""


def _parse_outline(raw: str, keys: list[int]) -> dict | None:
    import json

    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        chapters = [{"title": str(c["title"]), "sections": [int(x) for x in c["sections"]]}
                    for c in d["chapters"] if c.get("sections")]
        skip = [int(x) for x in d.get("skip", [])]
    except (ValueError, KeyError, TypeError):
        return None
    used = [k for c in chapters for k in c["sections"]] + skip
    missing = [k for k in keys if k not in used]
    if not chapters or len(set(used)) != len(used) or any(k not in keys for k in used):
        return None
    for k in missing:  # 漏掉的小节并入时间上最近的前一章
        target = max((c for c in chapters if min(c["sections"]) < k),
                     key=lambda c: min(c["sections"]), default=chapters[0])
        target["sections"].append(k)
    for c in chapters:
        c["sections"].sort()
    return {"title": str(d.get("title") or ""), "intro": str(d.get("intro") or ""),
            "chapters": chapters, "skip": sorted(skip)}


def run_concise(work: Path, title: str, sections: dict[int, str], client: LLMClient,
                s) -> tuple[str, str, list[str]]:
    """生成正式讲义：先归并主题拟大纲，再逐章精简。返回 (标题, 内容提要, 各章 Markdown)。"""
    import json

    keys = sorted(k for k in sections if sections[k].strip())
    out_dir = work / "concise"
    outline_path = out_dir / "outline.json"
    if outline_path.exists():
        outline = json.loads(outline_path.read_text(encoding="utf-8"))
    else:
        brief = "\n\n".join(f"[{k}] {sections[k][:400]}" for k in keys)
        outline = None
        for _ in range(2):
            raw = client.chat(OUTLINE_SYSTEM, f"课程：{title}\n\n{brief}", temperature=0.2)
            outline = _parse_outline(raw, keys)
            if outline:
                break
        if not outline:  # 兜底：每 5 节一章
            logger.warning("精简版大纲解析失败，按顺序每 5 节归为一章")
            outline = {"title": "", "intro": "", "skip": [], "chapters": [
                {"title": sections[keys[i]].splitlines()[0].lstrip("# ").strip(),
                 "sections": keys[i:i + 5]} for i in range(0, len(keys), 5)]}
        _write(outline_path, json.dumps(outline, ensure_ascii=False, indent=1))
    logger.info(f"精简版：{len(outline['chapters'])} 章，剔除无关小节 {outline['skip'] or '无'}")
    lang = LANG_NAMES.get(s.note_language, s.note_language)
    chapters = outline["chapters"]

    def one(i: int) -> str:
        cache = out_dir / f"ch_{i + 1:02d}.md"
        done = _read(cache)
        if done is not None:
            return scrub_meta(done)
        ch = chapters[i]
        body = "\n\n".join(sections[k] for k in ch["sections"])
        prev = "；".join(c["title"] for c in chapters[:i]) or "（无，这是第一章）"
        md = scrub_meta(normalize_math(client.chat(
            CONCISE_SYSTEM.format(lang=lang),
            CONCISE_USER.format(title=title, chapter=ch["title"], prev=prev, body=body),
            temperature=0.2)))
        if not md.lstrip().startswith("## "):
            md = f"## {ch['title']}\n\n{md}"
        _write(cache, md)
        logger.info(f"  [讲义] 第 {i + 1}/{len(chapters)} 章完成")
        return md

    intro = scrub_meta(outline.get("intro") or "")
    return (outline.get("title") or title, intro,
            parallel_map(one, list(range(len(chapters))), s.concurrency))

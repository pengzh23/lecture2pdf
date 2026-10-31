# 使用说明

输入视频网址或本地视频，输出整理好的 PDF 讲义。讲义同时依据**语音**（老师的讲解）和**画面**（板书 / PPT），但正文只陈述知识本身。

## 流程

| 步骤 | 做什么 | 是否调用 AI | 缓存 |
|---|---|---|---|
| media | 下载视频（≤1080p）或使用本地文件 | 否 | `media.json` |
| transcript | 抽音轨，用 FunASR（中文，默认）或 faster-whisper 转写；术语提示；繁转简 | 否（本地） | `transcript.json`、`transcript_raw.md` |
| frames | 每 2 秒抽一帧（一次解码同时生成分析用的小灰度图和原分辨率 JPG） | 否 | `frames/`、`small.raw` |
| keyframes | 检测擦黑板 / 换页 / 机位变化，取每个版面「最满」的时刻，用多帧中位数去掉老师的遮挡，再做光照均衡和锐化 | 否 | `keyframes/` |
| vision | 视觉模型识别每张关键帧，同时参考该时段的文字稿 | 视觉模型 | `vision/` |
| correct | 用板书内容校对该时段的文字稿（同音错字、术语） | 文本模型 | `corrected/`、`transcript_corrected.md` |
| notes | 每个版面整理成一节讲义，并生成标题和概要 | 文本模型 | `sections/`、`overview.md` |
| concise | 按知识主题把小节归并为章，剔除课程安排 / 闲聊，精简成正式讲义 | 文本模型 | `concise/` |
| render | 用 pandoc + XeLaTeX 排版为 PDF（另附 HTML） | 否 | `handout.md/.pdf/.html` |

每一步的结果都缓存在工作目录（默认 `output/<视频名>/`），中断后重跑会自动跳过已完成的步骤。

## 安装与配置

见 [installation.md](installation.md)。配置好后运行 `lecture2pdf-doctor` 自检。

## 配置模型（.env）

```ini
# 文本模型（DeepSeek 即可）
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com

# 视觉模型：必须支持图片输入（DeepSeek 不行）。任何 OpenAI 兼容接口均可，例如：
VISION_API_KEY=sk-...
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen-vl-max
```

没有配置视觉模型时程序不会报错，会只根据语音整理讲义。之后补上配置，运行 `--force vision` 即可补上画面识别。

## 用法

```bash
# 1) 先只跑到关键帧（不需要任何 API Key），检查截图效果
lecture2pdf lecture.mp4 --until keyframes

# 2) 完整流程；术语有助于语音识别和校对
lecture2pdf "https://www.bilibili.com/video/BVxxxx" \
    --cookies-from-browser chrome --terms "算子,谱测度,投影值测度,希尔伯特空间"

# 另外生成附板书原图的详细稿 handout_full.pdf，便于核对
lecture2pdf lecture.mp4 --full

# 已有字幕或文字稿时可跳过转写
lecture2pdf lecture.mp4 --transcript lecture.srt

# 调整关键帧参数后，只重做关键帧及之后的步骤（参数变化也会被自动检测）
lecture2pdf lecture.mp4 --max-gap 300 --sensitivity 0.5
```

结束时会在 stdout 打印 `{"pdf": ..., "pdf_full": ..., "markdown": ..., "html": ..., "work_dir": ...}`。

### 输出

| 文件 | 内容 |
|---|---|
| `handout.pdf` | 正式讲义：标题、内容提要、目录；按主题分章，定义 / 定理 / 命题等放在彩色框中 |
| `handout_full.pdf` | `--full` 时生成：按时间顺序的全部小节，每节附板书原图和时间，用于核对识别结果 |
| `transcript_corrected.md` | 校对后的完整文字稿 |

### 常用参数

| 参数 | 说明 |
|---|---|
| `--asr whisper` | 非中文课程使用 Whisper（`--whisper-model large-v3-turbo` 更快） |
| `--min-age 15` | PPT 课程翻页较快时调小（默认 40 秒，适合板书课） |
| `--sensitivity 0.5` | 越小越容易判定为「擦板」，关键帧越多（默认 0.6） |
| `--max-gap 300` | 最长 300 秒必须有一张关键帧（默认 600） |
| `--crop x,y,w,h` | 只保留黑板区域（原视频像素坐标），可提高文字清晰度 |
| `--vision-max-side 1280 --vision-quality 75` | 网络差、上传超时（408）时缩小上传的图片 |
| `--no-vision` | 不识别画面 |
| `--no-concise` | 不按主题归并精简，`handout.pdf` 直接由各版面小节组成（更详细、篇幅更长） |
| `--full` | 另外生成附板书原图的详细稿 `handout_full.pdf` |
| `--appendix-transcript` | 在详细稿末尾附上校对后的完整文字稿（隐含 `--full`） |
| `--cjk-font "Noto Serif CJK SC"` | 指定中文字体（默认使用 TeX Live 自带的 Fandol 字体） |
| `--clean` | 完成后删除抽帧和音频（约数百 MB） |

## 手工修订

- 修改 `concise/ch_XX.md`（正式讲义的各章）或 `sections/sec_XX.md`（详细稿的各节）后运行 `--force render`，只重新排版。
- 想让某章 / 某节重新生成：删掉对应文件后重跑；`--force concise` 重新归并全部章节，`--force notes` 连小节一起重新生成。
- 讲义中可用的块：`definition` 定义、`theorem` 定理、`proposition` 命题、`property` 性质、`lemma` 引理、`corollary` 推论、`postulate` 公设、`example` 例、`remark` 注、`proof` 证明，写法如 `::: {.theorem title="谱定理"}` … `:::`。
- PDF 编译失败时，Markdown 和 HTML 仍会生成。错误信息见 `render_xelatex.log`；模型写出的未定义 LaTeX 宏会被自动补上占位定义。
- 排版样式在 `src/lecture2pdf/templates/preamble.tex`（颜色、标题、彩色框）和 `theorems.lua`（块 → LaTeX 环境）。

## 已知局限

- 老师长时间站在同一处时，中位数合成无法去掉遮挡；被遮挡的内容只能依据讲解补全，无法确定的细节会被略去。
- 机位推拉或平移会被当作版面变化，从而多出关键帧。小节生成时会参考上一版面，归并成章时再次去重。
- 识别和整理都可能出错，重要公式请用 `--full` 生成的详细稿对照板书原图核对。

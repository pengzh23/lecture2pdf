# lecture2pdf

把课程视频（网址或本地文件）整理成**教材风格的 PDF 讲义**：同时依据老师的讲解（语音）和黑板 / PPT（画面），输出带编号章节、彩色定义 / 定理框、LaTeX 公式的讲义，正文只陈述知识本身。

```
视频 ──► 语音转写 (FunASR / Whisper) ─────────────┐
  │                                              ├──► 文字稿校对 ──► 讲义小节 ──► 按主题归并精简 ──► PDF
  └──► 本地关键帧检测 ──► 板书识别 (视觉模型) ──────┘      (LLM)        (LLM)          (LLM)        (pandoc + XeLaTeX)
```

## 特点

- **教材式排版**：ctex + tcolorbox 版式——蓝色编号标题、绿色「定义」框、橙色「定理」框、蓝色「命题 / 性质 / 引理 / 推论」框，「证明」「注」「例」行内标签，页眉显示当前章节，首页为标题、内容提要和目录。
- **只写知识**：讲义按知识主题归并、剔除课程安排和闲聊；提示词禁止「板书」「讲解」「语音转写」之类描述整理过程的措辞，另有规则过滤兜底。
- **省钱**：不逐帧调用 AI。本地检测「擦黑板 / 换页」的时刻，每个版面只取一张图。一节 90 分钟的课约 30 张图，文本模型调用约 70 次。
- **去遮挡**：用多帧中位数合成，把挡在黑板前的老师「抹掉」，并做光照均衡和锐化。
- **语音与画面互相校正**：板书纠正语音识别的同音错字，讲解帮助辨认模糊的板书。
- **可续跑**：每一步都有缓存，中断后重跑自动跳过；改参数只重做受影响的步骤。
- **国内网络友好**：中文默认用 FunASR（ModelScope 下载）；模型接口支持任意 OpenAI 兼容服务。

## 快速开始

```bash
pip install -e ".[funasr]"       # 详见 docs/installation.md（还需 ffmpeg 和 TeX Live）
cp .env.example .env             # 填写文本模型和视觉模型的 API Key
lecture2pdf-doctor               # 环境自检

# 先只跑到关键帧（不调用任何付费接口），检查截图效果
lecture2pdf lecture.mp4 --until keyframes

# 完整流程
lecture2pdf "https://www.bilibili.com/video/BVxxxx" --terms "算子,谱测度,希尔伯特空间"
```

输出位于 `output/<视频名>/`：

| 文件 | 内容 |
|---|---|
| `handout.pdf` | 正式讲义：按知识主题组织，只含知识内容（另有同名 `.md` / `.html`） |
| `handout_full.pdf` | 加 `--full` 时生成的详细稿：按时间顺序的全部小节，附板书原图，便于核对 |

讲义 Markdown 中的定义、定理等使用 pandoc 的 fenced div 书写，可以手工修改后用 `--force render` 重新排版：

```markdown
::: {.definition title="Hilbert 空间"}
完备的内积空间称为 Hilbert 空间。
:::
```

## 文档

- [安装](docs/installation.md)
- [使用说明与参数](docs/usage.md)
- [设计说明：关键帧算法、AI 流程与排版](docs/design.md)

## 适用范围

- ✅ 固定机位的板书课、PPT 课、板书 + PPT 混合课
- ⚠️ 频繁推拉 / 切换镜头的视频会产生较多关键帧（讲义仍可生成，归并时会去重）
- ⚠️ 老师长时间挡住同一块区域时，被挡的内容只能根据讲解补全

## 开发

```bash
pip install -e ".[dev]"
pytest && ruff check src tests
```

## 致谢

视频下载与转写部分参考了 [AI-Video-Transcriber](https://github.com/wendy7756/AI-Video-Transcriber)；依赖 [FunASR](https://github.com/modelscope/FunASR)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)、[pandoc](https://pandoc.org/)、[ctex](https://ctan.org/pkg/ctex)、[tcolorbox](https://ctan.org/pkg/tcolorbox)。

## 许可证

[MIT](LICENSE)

> 请仅处理你有权使用的课程视频，生成的讲义请遵守原课程的版权要求。

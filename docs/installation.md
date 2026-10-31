# 安装

## 1. 系统依赖

| 依赖 | 用途 | Ubuntu / Debian | macOS (Homebrew) |
|---|---|---|---|
| ffmpeg | 解码视频、抽帧、抽音轨 | `sudo apt install ffmpeg` | `brew install ffmpeg` |
| XeLaTeX + ctex + tcolorbox | 排版 PDF（中文默认用 TeX Live 自带的 Fandol 字体） | `sudo apt install texlive-xetex texlive-lang-chinese texlive-latex-extra` | `brew install --cask mactex-no-gui` |

pandoc 不需要单独安装，Python 依赖里的 `pypandoc_binary` 已经自带。

想换中文字体时用 `--cjk-font` 指定任意已安装的字体，例如 `--cjk-font "Noto Serif CJK SC"`（Linux）或 `--cjk-font "Songti SC"`（macOS）。

## 2. Python 环境（3.10+）

```bash
git clone https://github.com/pengzh23/lecture2pdf.git
cd lecture2pdf
conda create -n lecture2pdf python=3.11 -y && conda activate lecture2pdf   # 或 python -m venv .venv
pip install -e ".[funasr]"
# 国内网络可加：-i https://pypi.tuna.tsinghua.edu.cn/simple
# 只有 CPU 时可先装 CPU 版 torch，体积小很多：
#   pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```

- **FunASR**（默认，中文课程）：`[funasr]` 附加依赖会安装 funasr、modelscope、torch。首次转写时从 ModelScope 自动下载 Paraformer 模型（约 2.2 GB）。
- **Whisper**（其他语言，`--asr whisper`）：`pip install -e .` 即可。首次转写时自动下载 large-v3（约 3 GB）；Hugging Face 无法访问时会改从 ModelScope 下载。有 NVIDIA GPU 时自动使用 CUDA（需要 CUDA 12 + cuDNN 9）。

模型默认缓存在 `~/.cache/lecture2pdf/models/`，可用 `LECTURE2PDF_MODEL_DIR` 改到空间更大的磁盘。

## 3. 模型配置

```bash
cp .env.example .env    # 然后编辑 .env
lecture2pdf-doctor      # 自检：列出缺什么、怎么装
```

- **文本模型**（必需）：任意 OpenAI 兼容接口，例如 DeepSeek。
- **视觉模型**（推荐）：必须支持图片输入，例如通义千问 VL、GPT-4o、Gemini（经 OpenAI 兼容网关）。

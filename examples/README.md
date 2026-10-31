# 示例

## 术语表

- `terms_quantum_math.txt`：「量子力学的数学方法」课程的核心术语。
- `terms_week1.txt`：同一课程第一周（Hilbert 空间、算子）的术语。

```bash
lecture2pdf lecture.mp4 --terms-file examples/terms_week1.txt
```

术语表会作为语音识别的热词提示，并在校对文字稿时使用。为你的课程准备 10~30 个核心术语，识别准确率会明显提高。

## 讲义样式

生成的 `handout.pdf` 采用常见的 LaTeX 数学讲义版式：首页为标题、内容提要和目录；正文按「1 章节 / 1.1 小节」编号，定义（绿框）、定理（橙框）、命题 / 性质 / 引理 / 推论（蓝框）、公设（紫框）按节编号，「证明」「注」「例」为行内标签。样式可在 `src/lecture2pdf/templates/preamble.tex` 中调整。

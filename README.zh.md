# ebook-bilingual

[English](README.md) | **简体中文**

把 **EPUB** 或**文字版 PDF** 转成「一段英文、一段中文」对照版 **EPUB**——每个英文段后紧跟它的中文译文。

> ⚠️ **先看这条:后端依赖。** 翻译走本机 **[Claude Code](https://claude.com/claude-code) CLI**
> (`claude -p`),用你的 **Claude 订阅**——不读 API key,目前也**没有 API key 回退**。没装
> Claude Code 并登录就跑不起来。想用 OpenAI/DeepL/Gemini 等 API key 后端,请用
> [bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker)。

## 能做什么

- **EPUB → 双语 EPUB**:英文零篡改,每个元素后追加一个带样式的中文同级元素。翻 `<p>`、标题
  `<h1>`–`<h6>`、`<li>`、`<blockquote>`(`--tags` 可配),跳过 `<sup>`/`<code>`。
- **文字版 PDF → 双语 EPUB**:`pdftotext` + 段落重建(按行宽判段末、跨页合并、软连字符接词、
  数字去空格、去页眉页脚/页码、文本层被打散时自动回退 `-raw`、裁掉尾部索引/封底)→ 从零建规范 EPUB。
- **自动术语表**:每个高频专有名词在全书固定一个中文译名,保持一致。
- **三层反幻觉质检**:确定性检查(数字、长度、残留英文)→ 独立语义回查 → 自修复重译。
- **可续跑**:状态全在 SQLite,随时杀掉、重跑即续。
- **多书隔离**:每本书独立在 `runs/<slug>/` 下。

## 依赖

- **Python 3.9+** + `lxml`(`pip install -r requirements.txt`)
- **Claude Code CLI** 在 `PATH` 上、已登录(订阅有效)——翻译后端
- **poppler**(`pdftotext`)——仅 PDF 输入需要(macOS `brew install poppler`,
  Debian/Ubuntu `apt install poppler-utils`)

## 快速开始

```bash
python3 ebook_bilingual.py run --epub <书.epub>     # EPUB → 双语 EPUB
python3 ebook_bilingual.py run --pdf  <书.pdf>      # 文字版 PDF → 双语 EPUB
python3 ebook_bilingual.py status                    # 列出所有 run 的进度
```

产物是 **`<原名> - Bilingual EN-ZH.epub`**(加 `--single-translate` 则为 `<原名> - ZH.epub`),
生成在**源文件同目录**。

`run` **幂等可续**:中途停了(Ctrl-C、崩溃、用量上限)就重跑同一条命令,只接着做没做完的部分——
每段译文都缓存在 `runs/<slug>/cache.sqlite` 里。

## 流程——`run` 的六个阶段

`run` 串起六个阶段,已缓存的自动跳过,所以平时只用 `run` 和 `status` 即可。每个阶段也是独立子命令——
想重做或调试某一步时单独跑它。

| 子命令 | 作用 | 何时单独跑 |
|---|---|---|
| `extract` | 解压源文件,挑出正文文档(spine、词数 ≥ `--min-words`、去掉前后杂项),按 ~`--unit-words` 词切成单元。 | 改了 `--skip` / `--min-words` / `--tags`。 |
| `glossary` | 一次 Claude 调用建专有名词术语表(`glossary.json`),全书译名一致。 | 想先审阅/修改术语。 |
| `translate` | `claude -p` worker 池;按单元超时、重试、续跑。 | 续跑或重试卡住的单元。 |
| `qa` | 三层反幻觉质检 → `qa-report.md`。 | 重译后再查一遍。 |
| `inject` | 每个已译元素后加一个带样式的 `<… class="zh">` 同级元素(仅 EPUB,英文不动)。 | 改过库里译文后重新渲染。 |
| `repackage` | 重新打包规范 EPUB(EPUB 源)/ 从零构建(PDF 源)。 | 重新生成成品文件。 |
| `run` | 上述六步按序执行,可续。 | 通常情况。 |
| `status` | 所有 run 的进度(或 `--book <slug>` 看单本)。 | 随时;只读。 |

## 运行目录布局——状态与产物在哪

每本书独立在 `runs/<slug>/` 下(slug 由源文件名生成):

```
runs/
  active.txt          # 最近操作的 slug——不带 --epub/--pdf/--book 时用它
  <slug>/
    cache.sqlite      # 全部状态:段落、单元、译文、QA 结论
    work/             # 正在编辑的解压 EPUB(PDF 则是构建暂存目录)
    glossary.json     # 专有名词术语表
    qa-report.md      # 待人工复核的段落
```

- **新书**:`--epub` / `--pdf <文件>`(生成 slug 并设为 active)。
- **已有书**:`--book <slug>`。
- **都不带**:用 `active.txt` 里的 slug。
- 成品 EPUB 生成在**源文件**旁,不在 `runs/` 下。

## 参数

| 参数 | 默认 | 作用 |
|------|------|------|
| `--epub` / `--pdf <文件>` | — | 源文件(据文件名生成 run slug) |
| `--book <slug>` | 最近 active | 操作 `runs/<slug>/` 下已有的 run |
| `--model` | `sonnet` | 传给 `claude -p` 的模型 |
| `--tags` | `p,h1,h2,h3,h4,h5,h6,li,blockquote` | 翻哪些 EPUB 标签 |
| `--single-translate` | 关 | 只出中文,不双语 |
| `--translation-style` | `color:#777; font-size:0.92em;` | 中文文字的 CSS |
| `--concurrency` | `10` | 并发 `claude -p` worker 数 |
| `--unit-words` | `2500` | 每个翻译单元的词数 |
| `--unit-timeout` | `240` | 单元 worker 超时秒数 |
| `--max-attempts` | `5` | 判定单元「卡住」前的重试次数 |
| `--qa-sample` | `0.20` | 进入语义回查的段落比例 |
| `--min-words` | `150` | 一个 spine 文档被翻译的最低正文词数 |
| `--skip` | 常见前后杂项 | 要排除的文件名子串 |
| `--no-auto-skip` | 关 | 保留被内容识别为前后杂项的文件(不自动跳过) |
| `--test-file <名>` | — | 只处理某文件(如 `Chap1`) |

## 工作原理

- **后端**:`claude -p` 瘦身 worker(`--tools "" --strict-mcp-config --system-prompt`)
  + `MAX_THINKING_TOKENS=0`,每次开销 ~3.9k token、关掉对翻译纯属浪费的扩展思考。
- **批量协议**:段落用哨兵 `@@SEG@@` 分隔(比 JSON 抗引号/破折号);段数对不上就分治重试。
- **EPUB 注入**:`lxml.etree` 在每个可译元素后追加同标签 `<… class="zh">` 同级元素,并往每个
  `<head>` 自注入 `<style>`;源字节其余部分不动。

## 续跑与排错

- **中断了?** 重跑同一条 `run`——已缓存的自动跳过。
- **`N units stuck (need attention)`**:这些单元用尽了 `--max-attempts`。重跑可重试;超时就调大
  `--unit-timeout`,撞到用量上限就过会儿再跑。具体原因看 `cache.sqlite` 的 `units.error` 列。
- **被限流?** `translate` 会指数退避继续跑,过会儿重跑也行。
- **译文质量?** 看 `runs/<slug>/qa-report.md`——被质检标记的段落列在那里供复核。它们仍会进入成品
  (英文始终完整)。
- **`0 translatable paragraphs`?** 全被跳过了——调小 `--min-words`、收窄 `--skip`,或加
  `--no-auto-skip`。扫描件(纯图片)没有可翻译的文字层。

## 开发

- **测试**:`python3 -m unittest test_ebook_bilingual.py`(纯 Python,不需要联网,也不需要
  `claude`/`pdftotext`——覆盖各种确定性文本启发式)。
- 单文件程序(`ebook_bilingual.py`),无构建步骤。
- **约定**:代码注释与提交信息用英文;用户文档是中英成对文件(`*.md` + `*.zh.md`),两边一起改。
  架构地图与不变量见 [CLAUDE.md](CLAUDE.md)。

## 局限

- **必须有 Claude Code + Claude 订阅**(见上方提示)。
- **扫描版 PDF**(无文字层)直接报错——不含 OCR。
- **PDF 分章**是 best-effort(识别显式「Chapter N」/ 全大写标题);漏了也不影响,全书会作为
  一整篇正常阅读。
- **EPUB 的导航原样透传**——源 EPUB 没有目录,双语成品也不会有。
- 针对**单栏正文**优化;复杂多栏 / 表格排版可能重排不完美。

## 许可

[MIT](LICENSE)

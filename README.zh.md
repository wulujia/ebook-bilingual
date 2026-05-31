# ebook-bilingual

把 **EPUB** 或**文字版 PDF** 转成「一段英文、一段中文」对照版 **EPUB**——每个英文段后紧跟它的中文译文。

> English: [README.md](README.md)

> ⚠️ **先看这条:后端依赖。** 翻译走本机 **[Claude Code](https://claude.com/claude-code) CLI**
> (`claude -p`),用你的 **Claude 订阅**——不读 API key,目前也**没有 API key 回退**。没装
> Claude Code 并登录就跑不起来。想用 OpenAI/DeepL/Gemini 等 API key 后端,请用
> [bilingual_book_maker](https://github.com/yihong0618/bilingual_book_maker)。

## 用法

```bash
python3 ebook_bilingual.py run --epub <书.epub>     # EPUB → 双语 EPUB
python3 ebook_bilingual.py run --pdf  <书.pdf>      # 文字版 PDF → 双语 EPUB
python3 ebook_bilingual.py status                    # 看当前书进度
python3 ebook_bilingual.py run --book <slug>         # 继续/重跑已有的某本书
```

`run` = `extract → glossary → translate → qa → inject → repackage`。产物在**源文件同目录**
生成 `<原名> - Bilingual EN-ZH.epub`(加 `--single-translate` 则为 `<原名> - ZH.epub`)。进程随时可杀,重跑 `run` 自动续。

## 依赖

- **Python 3.9+** + `lxml`(`pip install -r requirements.txt`)
- **Claude Code CLI** 在 `PATH` 上、已登录(订阅有效)——翻译后端
- **poppler**(`pdftotext`)——仅 PDF 输入需要(macOS `brew install poppler`)

## 多书隔离

每本书一个独立运行目录,互不干扰:`runs/<slug>/`(cache.sqlite + work/ + glossary.json +
qa-report.md)。`--epub/--pdf <x>` 新书(slug 由文件名生成并设为 active);`--book <slug>`
切到已有的书;不带参数则操作 `runs/active.txt` 里那本。

## 参数(均有默认,可覆盖)

`--tags`(翻哪些标签,默认 `p,h1,h2,h3,h4,h5,h6,li,blockquote`)
`--single-translate`(只出中文版,不双语)`--translation-style`(中文 CSS)
`--concurrency`(10)`--unit-words`(2500)`--qa-sample`(0.20)`--min-words`(150)
`--skip`(文件名子串黑名单)`--test-file <名>`(只处理某文件,如 Chap1)

## 翻译哪些内容

- **哪些文件**:从 EPUB 的 OPF spine 自动挑正文词数 ≥ `--min-words` 的 XHTML,减去命中
  `--skip` 的(默认排除 cover/toc/index/bibliography 等)。换书不用改代码。
- **哪些标签**:默认翻 `<p>` + 标题 `<h1>–<h6>` + `<li>` + `<blockquote>`,跳过
  `<sup>`/`<code>`;嵌套时只翻叶子级元素,英文零篡改。
- **PDF**:`pdftotext` 抽文字 → 段落重建(按行宽判段末、跨页合并、去页眉页脚/页码)→ 从零建
  双语 EPUB。扫描件(无文字层)会直接报错(不含 OCR)。分章 best-effort。

## 关键技术点

- **后端**:`claude -p` 瘦身 worker(`--tools "" --strict-mcp-config --system-prompt`)
  + `MAX_THINKING_TOKENS=0`,每次开销 ~3.9k token、关掉无用的扩展思考。
- **协议**:段落用哨兵 `@@SEG@@` 分隔(比 JSON 抗引号/破折号),段数对不上就分治回退。
- **自愈**:状态全在 `cache.sqlite`,幂等可续,进程被杀重跑即续。
- **质检**:L1 确定性 → L2 语义回查(反幻觉,抽样)→ L3 自修复重译。

## 许可

[MIT](LICENSE)

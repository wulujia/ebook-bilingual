# ebook-bilingual — 项目约定

面向 Claude 和贡献者的工作约定。这是公开 OSS 项目，**英文为源、中文为镜像译文**。

## 语言约定

- **代码注释、commit message**：英文。
- **面向用户的文档：中英各一份，配对维护，每次改动两份一起更新。**
  - `README.md`（English）+ `README.zh.md`（简体中文）
  - `CHANGELOG.md`（English）+ `CHANGELOG.zh.md`（简体中文）
  - 每份文件顶部带语言切换头，例如 `**English** | [简体中文](README.zh.md)`。
  - 英文是事实来源；中文是它的对照翻译，不要让两边内容跑偏。

## Changelog

- 每个用户可见的行为变更都要记一条，**中英两份同步**（先写英文，再同步中文）。
- 版本号语义化：bugfix → patch，新增能力 → minor，破坏性变更 → major。

## 生成文件命名

- 产物文件名保持纯 ASCII：`<原名> - Bilingual EN-ZH.epub`，`--single-translate` 时为
  `<原名> - ZH.epub`（见 `edition_label()`）。不要在生成的文件名里用中文。

## 测试

- 纯逻辑单测：`python3 test_ebook_bilingual.py`（无需网络 / 不调 Claude）。改动 helper 后必须跑。

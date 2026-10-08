# 视频剪辑自动化工具包

本仓库独立整理豆包与剪映相关的自动化工具源码、运行规则和版本包，不与个人 Codex Skills 仓库混用，方便团队阅读、测试、交接与下载。内容包括任务编排脚本、剪映工程辅助工具、视觉规则、运行环境说明和测试用例。

## 版本说明

| 工具包 | 编排器与剪映组件 | 视觉规则 | 状态 |
| --- | --- | --- | --- |
| 1.4.0 | 1.4.0 | 1.4.0 | 历史版本归档 |
| 1.5.0 | 1.5.0 | 1.4.1 | 候选预发布版 |

1.4.0 的下载包是根据对应源码标签重建的归档，并非当时留存的原始 ZIP。1.5.0 保留了候选 ZIP，供源码阅读和评估使用。**候选包、源码测试通过或 Release 已发布，都不代表渲染机已经升级或生产验收完成。**

## 仓库目录

- [`packages/`](packages/)：按版本保存的源码包。每个版本含有包说明、环境变量约定、规则、源码和测试。
- [`docs/RELEASES.md`](docs/RELEASES.md)：版本来源、归档方式、测试结果和 ZIP 校验值。
- [`releases/`](releases/)：各版本 ZIP 的 SHA-256 校验清单；ZIP 文件本身通过 GitHub Release 下载。
- [GitHub Releases](https://github.com/menjou061/video-editing-automation/releases)：下载 ZIP 和对应的 SHA-256 清单。

## 从哪里开始

阅读或接手某个版本时，建议按这个顺序：

1. 根据上方版本表和 [`docs/RELEASES.md`](docs/RELEASES.md) 选择版本，确认归档来源与发布状态。
2. 阅读对应版本的 `ENVIRONMENT.md`，确认运行所需的目录和环境变量。
3. 阅读 `RULES.md` 与编排器目录下的 `SKILL.md`，了解执行边界。
4. 查看 `tests/` 和版本清单，了解测试覆盖与组件版本。

## 下载与校验

从 [Releases 页面](https://github.com/menjou061/video-editing-automation/releases)下载同一版本的 ZIP 和 `SHA256SUMS` 文件，将两者放在同一目录后校验：

```bash
# macOS
shasum -a 256 -c SHA256SUMS-video-v1.5.0.txt

# Linux
sha256sum -c SHA256SUMS-video-v1.5.0.txt
```

校验 1.4.0 时，将文件名替换为 `SHA256SUMS-video-v1.4.0.txt`。校验文件显示 `OK` 后再解压使用。

## 使用边界

包内不包含生产凭据、机器专属目录或生产素材。部署环境需按版本目录中的 `ENVIRONMENT.md` 提供所需配置；不要把令牌、密码或真实机器路径提交到仓库。安装源码包不等同于完成渲染机部署，生产使用前还需按对应版本流程进行环境检查和验收。

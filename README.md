# apple-journal-export

把 Apple「手记」(Journal.app) 导出为结构化 Markdown + 媒体文件。

Apple 手记**没有官方导出功能**，数据被锁在本地 Core Data 数据库里。这个脚本只读解析它，
产出带 YAML front-matter 的 Markdown 和整理好的图片/视频，可直接拖进
Obsidian / Logseq / Typora / 任何 Markdown 编辑器。

```
journal-export/
├── entries/
│   ├── 2000-01-01_001.md
│   └── ...
├── media/
│   ├── A1B2C3D4-E5F6_01.jpg
│   └── ...
└── index.json
```

## 特性

- **完整** —— 正文、标题、日期、草稿状态、图片、视频、图文原始顺序全部还原
- **只读** —— 先快照数据库到临时目录再解析，绝不写原库
- **不联网** —— 不发起任何网络请求，不上传任何数据
- **纯系统依赖** —— 除 Python 标准库外，只用 macOS 自带的 `textutil` / `sips`，零第三方包
- **保留原始顺序** —— 按 `ZASSETORDERING` 还原图文混排，实况照片的图/视频正确配对

## 环境要求

- macOS（依赖系统自带的 `textutil` 和 `sips`）
- Python 3.9+

## 安装

```bash
git clone https://github.com/<you>/apple-journal-export.git
cd apple-journal-export
chmod +x export_journal.py
```

### ⚠️ 需要授予「完全磁盘访问权限」

手记的数据放在 Group Container 里，普通权限读不到。给**终端**（或 iTerm / VS Code）授权：

> 系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加「终端」

没授权的话脚本会给出明确提示。

### 建议先退出「手记」App

App 运行时数据库可能正在写入。脚本会检测并提醒，但退出后再导出更保险。

## 用法

```bash
# 导出到 ./journal-export
./export_journal.py

# 指定输出目录
./export_journal.py -o ~/Documents/journal

# 保留 HEIC 原格式（体积小约 40%，但很多预览器不显示 HEIC）
./export_journal.py --keep-heic

# 图片压缩：最长边 1600px、质量 70
./export_journal.py --max-dim 1600 -q 70

# 先检查环境，不写文件
./export_journal.py --dry-run

# 逐条打印进度
./export_journal.py -v
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-o, --out` | `journal-export` | 输出目录 |
| `-s, --source` | Moments Group Container | 手记数据目录 |
| `--keep-heic` | 关 | 不把 HEIC 转 JPEG |
| `--max-dim N` | `0`（不缩放） | 图片最长边限制（只缩放，不改格式） |
| `-q, --quality N` | `80` | JPEG 质量（仅影响 HEIC 转 JPEG） |
| `--skip-drafts` | 关 | 跳过草稿 |
| `--dry-run` | 关 | 只检查环境 |
| `-v, --verbose` | 关 | 逐条打印进度 |
| `-V, --version` | — | 显示版本号 |

数据目录也可以用环境变量指定：

```bash
JOURNAL_SOURCE="/path/to/Library" ./export_journal.py
```

## 输出格式

每篇日记是一个 Markdown 文件，`YYYY-MM-DD_<id>.md`：

```markdown
---
id: 1
date: 2000-01-01 00:00:00
title: "周末露营"
type: blankEntry
draft: false
flagged: false
media: ["A1B2C3D4-E5F6_01.jpg", "A1B2C3D4-E5F6_02.mov"]
---
# 周末露营

今天去山里露营，天气很好……

![图片](media/A1B2C3D4-E5F6_01.jpg)

[▶ 视频](media/A1B2C3D4-E5F6_02.mov)
```

非媒体的记录以行内标记保留：`📍 位置`、`🙂 心情记录`、`🔥 连续记录`。

## 数据是怎么读出来的

这些是逆向出来的内部结构，供参考或排查问题：

```
~/Library/Group Containers/group.com.apple.moments/Library/
├── moments.sqlite     ← Core Data 主库
├── moments.sqlite-wal ← ⚠️ 未 checkpoint 的最新数据
└── Attachments/       ← 媒体文件
```

注意：手记**不在** `group.com.apple.Journal` 容器里，内部走的是 Moments 框架。

| 表 | 内容 |
| --- | --- |
| `ZJOURNALENTRYMO` | 条目（`ZTEXT` / `ZTITLE` 为 RTF，`ZASSETORDERING` 是图文顺序的 JSON） |
| `ZJOURNALENTRYASSETMO` | 资源（`ZASSETTYPE`: photo / livePhoto / multiPinMap / stateOfMind / streakEvent） |
| `ZJOURNALENTRYASSETFILEATTACHMENTMO` | 实际文件（`ZFILEPATH` = `assetUUID/attachmentUUID/fileUUID_resized.ext`） |

几个容易踩的坑：

1. **必须连 `-wal` 一起读** —— 最新数据常常还在 WAL 里没落盘，只读 `.sqlite` 会丢最近的条目。脚本会把三件套一起快照到临时目录。
2. **标题和正文都是 RTF**，不是纯文本。直接按 UTF-8 解码会得到 `{\rtf1\ansi...` 垃圾。必须过 `textutil`。
3. **`ZASSETORDERING` 决定图文顺序** —— 结构是 `["uuid", 0, "uuid", 1, ...]`，不按它排会打乱排版。
4. **实况照片 = 1 图 + 1 视频**（两个 file attachment 挂在同一个 asset 下），靠 `ZNAME` 字段（`image` / `video`）区分，不能靠 UUID 猜。

## 关于图片

### 本地只有缩略图，没有原图

这是个重要的事实：`Attachments/` 里**只有 `_resized` 缩放版**（最长边 ≤ 2829px），
**原图不在本机磁盘上** —— 它们只存在于 iCloud / 照片库里。

所以导出的是「手记里看到的图」，不是「相机原图」。

### 默认行为：HEIC → JPEG

HEIC 压缩率比 JPEG 高，但浏览器和多数 Markdown 预览器不支持，所以默认转成 JPEG（质量 80）。

代价是体积会变大（HEIC 转 JPEG 通常膨胀 30–60%）。以一个 HEIC 照片为主的日记库为例：

| 方案 | 相对体积 | 兼容性 |
| --- | --- | --- |
| `--keep-heic` | 1.0× | 差（HEIC 常显示不出来） |
| 默认（JPEG q80） | ~1.3× | 好 |
| `--max-dim 1600 -q 70` | ~0.6× | 好 |

按需选。想两全其美可以默认导出后用 [Squoosh](https://squoosh.app/) 之类的工具批量再压。

### `--max-dim` 只缩放，不改格式

`--max-dim N` 会把超过 N 像素的图片缩到最长边为 N，但**保持原格式**：
PNG 的透明通道、GIF/TIFF 的格式都不会被改掉。是否把 HEIC 转 JPEG 只由 `--keep-heic` 控制。

`-q` 只在发生 JPEG ⭢ JPEG 编码时生效（即 HEIC 转换），对 PNG/GIF 无效。

### 想要原图？

用 [`osxphotos`](https://github.com/RhetTbull/osxphotos) 按拍摄时间和 GPS 坐标从照片库匹配原图：

```bash
brew install osxphotos
osxphotos query --from-date 2024-09-28 --to-date 2024-09-29 --export ./originals
```

匹配可能漏（照片被改过时间或删除），需要人工复核。

## 疑难排查

### `Operation not permitted`，读不到手记数据库

典型现象：目录能列出来（`ls` 正常），但读文件内容报 `Operation not permitted`。

根因是 macOS 对 `~/Library/Group Containers/` 做了 **App Data 保护**：授权不到位的进程
只能看到目录结构，读不到任何文件内容。特征是容器上多一个 `com.apple.macl` 扩展属性：

```bash
xattr ~/Library/Group\ Containers/group.com.apple.moments
# 有 com.apple.macl 就是它在拦
```

解决：给终端授予**完全磁盘访问权限**，然后**完全退出并重开终端**
（授权对已经运行的进程不生效）：

> 系统设置 → 隐私与安全性 → 完全磁盘访问权限 → ＋ → 添加「终端」

验证（不报错就是好了）：

```bash
head -c 4 ~/Library/Group\ Containers/group.com.apple.moments/Library/moments.sqlite
```

注意两点：

- **`sudo` 没用**，TCC 不认 root
- **不能靠 `xattr -d com.apple.macl` 绕过**，macOS 会立刻重新打上，还会多一个 `com.apple.provenance`

### 导出条目数比在手记里看到的少

- 草稿默认会导出，可用 `--skip-drafts` 排除
- 手记里「最近删除」的条目不会导出
- 刚在别的设备上写过的内容，等 iCloud 同步完再导

### 中文变成乱码或 `\u4e2d\u6587` 这种转义

说明 `textutil` 没跑成功。确认系统里有：`which textutil`。
正常情况下 RTF 的 `\uNNNN` 转义会被正确还原成中文。

## 隐私

- 脚本不联网，不发送任何数据
- 数据库快照写入系统临时目录，**正常与异常退出都会删除**；只有进程被 `SIGKILL` 或断电时才会残留，那类残留会在下次运行时自动清理
- 报错信息里的家目录缩成 `~`、UUID 截断，避免贴到公开 issue 时连带泄露用户名和内部 ID
- 导出的 `journal-export/` 里是**完整的个人日记**。别提交进 Git —— 仓库的 `.gitignore` 已经把它排除了

如果你要 fork 或提 PR，注意别把 `journal-export/` 或 `moments.sqlite*` 一起提交上去。

## 已知限制

- 只在 macOS 上实测过（开发机为 macOS 26）。iOS 需要越狱才能访问，不在范围内
- 位置记录只导出 `📍 位置` 标记，不含坐标和地图缩略图
- 心情记录（State of Mind）只导出标记，不含具体数值
- 每次都是全量导出，但**不会清理陈旧文件**：已从手记删除的条目、或换用 `--keep-heic` 后残留的旧扩展名媒体，都会留在输出目录里。建议每次导出到空目录，或先手动清空
- 增量导出需要自己比对 `index.json` 里的 `id`
- 内部表结构可能随系统更新变化，升级 macOS 后如果报错请开 issue

## License

MIT

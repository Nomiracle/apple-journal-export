#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apple-journal-export — 把 Apple「手记」(Journal.app) 导出为结构化 Markdown

为什么需要这个工具：Apple 手记没有官方导出功能。数据以 Core Data 形式存放在
本地 Group Container 里，本脚本只读解析它，产出 Markdown + 媒体文件，
可直接拖进 Obsidian / Logseq / Typora。

设计原则：
  * 只读 —— 先快照 sqlite(+WAL) 到临时目录再解析，绝不写原库
  * 无网络 —— 不发起任何请求，不上传任何数据
  * 临时文件必删 —— 快照含全部日记内容，异常路径也保证清理

仅支持 macOS（依赖系统自带的 textutil / sips）。

License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# ── 常量 ──────────────────────────────────────────────────────────────────
# Apple 手记内部使用 Moments 框架，数据不在 Journal 自己的容器里
DEFAULT_SOURCE = "~/Library/Group Containers/group.com.apple.moments/Library"
DB_NAME = "moments.sqlite"
ATTACHMENTS_DIR = "Attachments"

# Core Data 的时间戳基准是 2001-01-01，比 Unix epoch 晚 978307200 秒
CORE_DATA_EPOCH = 978307200

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp", ".webp"}
HEIC_EXTS = {".heic", ".heif"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}


# ── 小工具 ────────────────────────────────────────────────────────────────
def die(msg: str, code: int = 1) -> None:
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"警告: {msg}", file=sys.stderr)


UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)


def display_path(path: str) -> str:
    """错误信息里显示的路径：家目录缩成 ~，UUID 截断。

    用户常把报错贴到公开 issue，这里避免连带泄露家目录用户名和日记库内部 ID。
    """
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    return UUID_RE.sub(lambda m: m.group(0)[:8] + "…", path)


def cleanup_stale_snapshots(max_age_sec: int = 3600) -> None:
    """清理上次被强杀（SIGKILL / 断电）留下的快照目录。

    快照里有完整的日记数据库，不该长期躺在临时目录。只动超过 max_age_sec
    的目录，避免误删并发运行的另一个实例。
    """
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not name.startswith("journal-snapshot-"):
            continue
        path = os.path.join(tmp, name)
        try:
            if now - os.path.getmtime(path) > max_age_sec:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def core_data_uuid(raw: bytes | None) -> str | None:
    """ZID 是 16 字节 UUID，转成带横线的大写形式。"""
    if not raw or len(raw) != 16:
        return None
    h = raw.hex().upper()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def rtf_to_text(rtf: bytes | str | None) -> str:
    """用系统 textutil 把 RTF 转纯文本（正确处理中文的 \\uNNNN 转义）。

    手记的正文和标题都是 RTF，不是纯文本，必须走这一步。
    """
    if not rtf:
        return ""
    data = rtf.encode("utf-8") if isinstance(rtf, str) else rtf
    with tempfile.NamedTemporaryFile("wb", suffix=".rtf", delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        proc = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", path],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            warn(f"textutil 转换失败，退回原始文本: {proc.stderr[:120]!r}")
            return data.decode("utf-8", "replace")
        return proc.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        warn("textutil 超时，跳过该条正文")
        return ""
    finally:
        os.unlink(path)


def format_date(ts: float | None, tz) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts + CORE_DATA_EPOCH, tz).strftime("%Y-%m-%d %H:%M:%S")


# ── 前置检查 ──────────────────────────────────────────────────────────────
def preflight(source: str) -> None:
    if sys.platform != "darwin":
        die("本工具依赖 macOS 自带的 textutil / sips，仅支持 macOS。")

    if not os.path.isdir(source):
        die(
            f"找不到手记数据目录:\n  {display_path(source)}\n\n"
            "确认：\n"
            "  1. 本机安装并至少打开过一次「手记」App\n"
            "  2. 终端已获得「完全磁盘访问权限」\n"
            "     系统设置 → 隐私与安全性 → 完全磁盘访问权限"
        )

    db = os.path.join(source, DB_NAME)
    if not os.path.isfile(db):
        die(f"找不到数据库文件: {display_path(db)}")

    # 只读探测：macOS 对 Moments 容器有 TCC 保护，目录能列但文件内容读不到。
    # 在这里就报错，比导出中途才失败清楚得多。
    try:
        with open(db, "rb") as fh:
            fh.read(16)
    except PermissionError:
        die(
            "无法读取手记数据库（Operation not permitted）。\n\n"
            "macOS 保护了手记的数据容器：目录能列出，但文件内容需要额外授权。\n"
            "解决办法（二选一）：\n"
            "  1. 给当前终端授予「完全磁盘访问权限」，然后重启终端\n"
            "     系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加「终端」\n"
            "  2. 改从已有该权限的 App 的集成终端里运行（例如 VS Code）\n"
        )
    except OSError as exc:
        die(f"读取数据库失败: {exc.strerror or exc}\n  路径: {display_path(db)}")

    for tool in ("textutil", "sips"):
        if not shutil.which(tool):
            die(f"缺少系统工具 {tool}")

    if is_journal_running():
        warn(
            "「手记」App 正在运行。数据库可能处于写入状态，"
            "建议先退出 App 再导出，以保证数据一致。"
        )


def is_journal_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-x", "Journal"], capture_output=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


def snapshot_database(source: str, workdir: str) -> str:
    """把 sqlite + WAL + SHM 一起拷到临时目录。

    必须连 -wal 一起拷：手记的最新数据常常还停留在 WAL 里没有 checkpoint，
    只拷 .sqlite 会丢掉最近的条目。
    """
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(source, DB_NAME + suffix)
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(workdir, DB_NAME + suffix))
            except PermissionError:
                die(
                    "读取数据库被拒绝。请给终端授予「完全磁盘访问权限」：\n"
                    "  系统设置 → 隐私与安全性 → 完全磁盘访问权限"
                )
    return os.path.join(workdir, DB_NAME)


def resolve_attachment(attachments: str, rel: str) -> str | None:
    """把数据库里的 ZFILEPATH 解析成绝对路径，拒绝越出 Attachments 的路径。

    ZFILEPATH 是数据库字段，是唯一未校验的输入边界：若库被篡改含 ../ ，
    就能把 Attachments 之外的文件复制进导出目录。越界时返回 None。
    """
    base = os.path.realpath(attachments)
    target = os.path.realpath(os.path.join(base, rel))
    # 必须严格位于 base 之下：同时挡住越界路径和 rel 为空（=base 自身）的情况
    if not target.startswith(base + os.sep):
        return None
    return target


# ── 数据读取 ──────────────────────────────────────────────────────────────
def load_assets(db: sqlite3.Connection) -> tuple[dict, dict]:
    """返回 (entry_pk -> [asset rows], asset_pk -> [file rows])。"""
    files: dict[int, list[sqlite3.Row]] = {}
    for row in db.execute(
        "SELECT Z_PK, ZASSET, ZINDEX, ZNAME, ZFILEPATH "
        "FROM ZJOURNALENTRYASSETFILEATTACHMENTMO"
    ):
        files.setdefault(row["ZASSET"], []).append(row)

    assets: dict[int, list[sqlite3.Row]] = {}
    for row in db.execute(
        "SELECT Z_PK, ZENTRY, ZASSETTYPE, ZCONTENTTYPE, ZSOURCE, ZID AS ZUUID "
        "FROM ZJOURNALENTRYASSETMO "
        "WHERE ZISFULLYREMOVED IS NULL OR ZISFULLYREMOVED = 0"
    ):
        assets.setdefault(row["ZENTRY"], []).append(row)

    return assets, files


def parse_asset_order(blob: bytes | None) -> dict[str, int]:
    """ZASSETORDERING 是 JSON: ["uuid", 0, "uuid", 1, ...]，记录图文的原始顺序。"""
    if not blob:
        return {}
    try:
        arr = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    order = {}
    for i in range(0, len(arr) - 1, 2):
        order[str(arr[i]).upper()] = arr[i + 1]
    return order


# ── 媒体导出 ──────────────────────────────────────────────────────────────
def export_media(src_path: str, dst_media: str, asset: sqlite3.Row,
                 file_row: sqlite3.Row, args) -> str | None:
    """拷贝/转换一个媒体文件，返回目标文件名；失败返回 None。"""
    if not os.path.exists(src_path):
        warn(f"附件文件不存在，跳过: {display_path(src_path)}")
        return None

    ext = os.path.splitext(src_path)[1].lower()
    asset_id = (core_data_uuid(asset["ZUUID"]) or "unknown")[:13]
    stem = f"{asset_id}_{file_row['Z_PK']}"

    if ext in HEIC_EXTS and not args.keep_heic:
        # 方案 A：HEIC 转 JPEG，兼顾浏览器与多数 Markdown 预览器的兼容性
        name = stem + ".jpg"
        dst = os.path.join(dst_media, name)
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(args.quality)]
        if args.max_dim:
            cmd += ["-Z", str(args.max_dim)]
        cmd += [src_path, "--out", dst]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode == 0 and os.path.exists(dst):
            return name
        warn(f"HEIC 转换失败，改为原样复制: {display_path(os.path.basename(src_path))}")
        if os.path.exists(dst):
            os.unlink(dst)
        name = stem + ext
        shutil.copy2(src_path, os.path.join(dst_media, name))
        return name

    if args.max_dim and ext in IMAGE_EXTS:
        # 只缩放、不改格式，避免 PNG 透明通道丢失
        name = stem + ext
        dst = os.path.join(dst_media, name)
        cmd = ["sips", "-Z", str(args.max_dim), src_path, "--out", dst]
        if subprocess.run(cmd, capture_output=True).returncode == 0 and os.path.exists(dst):
            return name
        if os.path.exists(dst):
            os.unlink(dst)

    name = stem + ext
    shutil.copy2(src_path, os.path.join(dst_media, name))
    return name


# ── 主流程 ────────────────────────────────────────────────────────────────
def export(args) -> int:
    source = os.path.abspath(os.path.expanduser(args.source))
    preflight(source)

    out_root = os.path.abspath(os.path.expanduser(args.out))
    entries_dir = os.path.join(out_root, "entries")
    media_dir = os.path.join(out_root, "media")
    attachments = os.path.join(source, ATTACHMENTS_DIR)
    tz = datetime.now().astimezone().tzinfo

    if args.dry_run:
        print(f"干跑模式：将读取 {source}\n          输出到 {out_root}")
        return 0

    os.makedirs(entries_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)

    cleanup_stale_snapshots()
    workdir = tempfile.mkdtemp(prefix="journal-snapshot-")
    db = None
    try:
        db_path = snapshot_database(source, workdir)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        assets_by_entry, files_by_asset = load_assets(db)

        query = """
            SELECT Z_PK, ZTITLE, ZTEXT, ZENTRYDATE, ZCREATEDDATE,
                   ZENTRYTYPE, ZISDRAFT, ZFLAGGED, ZASSETORDERING
            FROM ZJOURNALENTRYMO
            WHERE (ZISFULLYREMOVED IS NULL OR ZISFULLYREMOVED = 0)
              AND (ZRECENTLYDELETED IS NULL OR ZRECENTLYDELETED = 0)
        """
        if args.skip_drafts:
            query += " AND (ZISDRAFT IS NULL OR ZISDRAFT = 0)"
        query += " ORDER BY ZENTRYDATE"

        entries = db.execute(query).fetchall()

        index: list[dict] = []
        media_count = 0
        skipped = 0

        for entry in entries:
            pk = entry["Z_PK"]

            # 手记的正文与标题都是 RTF，必须转纯文本
            body = rtf_to_text(entry["ZTEXT"])
            title = rtf_to_text(entry["ZTITLE"])

            if not title:
                first = next((ln for ln in body.splitlines() if ln.strip()), "")
                title = first[:40] + "…" if len(first) > 40 else first
            if not title:
                title = f"手记 {pk}"

            # 按 ZASSETORDERING 还原图文原始顺序
            order = parse_asset_order(entry["ZASSETORDERING"])
            ordered = sorted(
                assets_by_entry.get(pk, []),
                key=lambda a: order.get(core_data_uuid(a["ZUUID"]) or "", 999),
            )

            blocks: list[str] = []
            media_names: list[str] = []

            for asset in ordered:
                # 实况照片 = 1 图 + 1 视频，ZNAME 已标明类型，图片排在前面
                file_rows = sorted(
                    files_by_asset.get(asset["Z_PK"], []),
                    key=lambda r: (r["ZNAME"] != "image", r["ZINDEX"] or 0),
                )
                for file_row in file_rows:
                    src = resolve_attachment(attachments, file_row["ZFILEPATH"])
                    if src is None:
                        warn(f"跳过越界附件路径: {display_path(file_row['ZFILEPATH'])}")
                        skipped += 1
                        continue
                    name = export_media(src, media_dir, asset, file_row, args)
                    if not name:
                        skipped += 1
                        continue
                    media_names.append(name)
                    media_count += 1
                    is_video = file_row["ZNAME"] == "video" or \
                        os.path.splitext(name)[1].lower() in VIDEO_EXTS
                    blocks.append(f"[▶ 视频](media/{name})" if is_video
                                  else f"![图片](media/{name})")

                if asset["ZASSETTYPE"] == "multiPinMap":
                    blocks.append("`📍 位置`")
                elif asset["ZASSETTYPE"] == "stateOfMind":
                    blocks.append("`🙂 心情记录`")
                elif asset["ZASSETTYPE"] == "streakEvent":
                    blocks.append("`🔥 连续记录`")

            date = format_date(entry["ZENTRYDATE"], tz) \
                or format_date(entry["ZCREATEDDATE"], tz) or "unknown"

            front_matter = [
                "---",
                f"id: {pk}",
                f"date: {date}",
                f"title: {json.dumps(title, ensure_ascii=False)}",
                f"type: {entry['ZENTRYTYPE'] or 'unknown'}",
                f"draft: {str(bool(entry['ZISDRAFT'])).lower()}",
                f"flagged: {str(bool(entry['ZFLAGGED'])).lower()}",
                f"media: {json.dumps(media_names, ensure_ascii=False)}",
                "---",
                "",
            ]

            fname = f"{date[:10]}_{pk:03d}.md"
            with open(os.path.join(entries_dir, fname), "w", encoding="utf-8") as fh:
                fh.write("\n".join(front_matter))
                fh.write(f"# {title}\n\n")
                if body:
                    fh.write(body + "\n")
                if blocks:
                    fh.write("\n\n" + "\n\n".join(blocks) + "\n")

            index.append({
                "id": pk,
                "date": date,
                "title": title,
                "draft": bool(entry["ZISDRAFT"]),
                "file": f"entries/{fname}",
                "media": media_names,
            })
            if args.verbose:
                print(f"  [{pk:>3}] {date[:16]}  {title[:40]}")

        with open(os.path.join(out_root, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)

        print(f"\n✅ 导出完成")
        print(f"   条目:   {len(index)}")
        print(f"   媒体:   {media_count}" + (f"（跳过 {skipped} 个缺失附件）" if skipped else ""))
        print(f"   输出:   {out_root}")
        return 0
    finally:
        if db is not None:
            db.close()
        # 快照含全部日记内容，正常与异常退出都会删；SIGKILL / 断电除外，
        # 那种情况由下次启动时的 cleanup_stale_snapshots() 抖尾
        shutil.rmtree(workdir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apple-journal-export",
        description="把 Apple「手记」导出为 Markdown + 媒体文件（只读，不联网）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  %(prog)s                          # 导出到 ./journal-export
  %(prog)s -o ~/Documents/journal   # 指定输出目录
  %(prog)s --keep-heic              # 保留 HEIC 原格式（体积更小，兼容性差）
  %(prog)s --max-dim 1600 -q 70     # 图片压到最长边 1600px、质量 70

注意:
  --max-dim 只缩放、不转换格式；HEIC 是否转 JPEG 只由 --keep-heic 决定。
  需要给终端授予「完全磁盘访问权限」，否则读不到 Group Container。
""",
    )
    p.add_argument("-o", "--out", default="journal-export",
                   help="输出目录（默认: %(default)s）")
    p.add_argument("-s", "--source", default=os.environ.get("JOURNAL_SOURCE", DEFAULT_SOURCE),
                   help="手记数据目录（默认: Moments Group Container）")
    p.add_argument("--keep-heic", action="store_true",
                   help="不把 HEIC 转成 JPEG（体积小约 40%%，但很多预览器不支持）")
    p.add_argument("--max-dim", type=int, default=0, metavar="N",
                   help="图片最长边限制为 N 像素（默认: 不缩放；只缩放不改格式）")
    p.add_argument("-q", "--quality", type=int, default=80, metavar="N",
                   help="JPEG 质量 0-100（默认: %(default)s）")
    p.add_argument("--skip-drafts", action="store_true", help="跳过草稿")
    p.add_argument("--dry-run", action="store_true", help="只检查环境，不写文件")
    p.add_argument("-v", "--verbose", action="store_true", help="逐条打印进度")
    p.add_argument("-V", "--version", action="version", version="%(prog)s 1.0.0")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not 1 <= args.quality <= 100:
        die("--quality 必须在 1-100 之间")
    if args.max_dim < 0:
        die("--max-dim 不能为负数")
    try:
        sys.exit(export(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

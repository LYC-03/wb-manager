"""合并被拆分的会话记录。

什么时候用：搬动工作空间目录时，如果那条会话正在使用，WorkBuddy 可能在
原位置新建一个空文件，对话就被拆成了两份——打开后只剩最近几条，早期的没了。
这个脚本把两份按「历史在前、当前在后」拼回去。

用法（必须完全退出 WorkBuddy 再跑）：
    python fix_merge.py --session <会话id> \
        --history "被搬走的那个文件.jsonl" \
        --current "原位置现在的文件.jsonl"

它会：先备份两份原件 → 去重 → 历史在前、当前在后拼接 → 写回原位置。
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def wb_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
                             capture_output=True, timeout=8)
        return b"workbuddy" in (out.stdout or b"").lower()
    except Exception:  # noqa: BLE001
        return False


def read_lines(p):
    out = []
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                d = json.loads(s)
            except ValueError:
                continue
            out.append((d.get("id") or "", d.get("timestamp") or 0, s))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--history", required=True, help="有完整历史的那个文件")
    ap.add_argument("--current", required=True, help="现在原位置正在写的文件")
    ap.add_argument("--force", action="store_true", help="WorkBuddy 在跑也强行执行")
    args = ap.parse_args()

    hist = Path(args.history)
    cur = Path(args.current)
    for p in (hist, cur):
        if not p.exists():
            sys.exit(f"文件不存在：{p}")

    if wb_running() and not args.force:
        sys.exit("WorkBuddy 还在运行。先完全退出（含托盘），再跑一次。\n"
                 "确认它不会写这个文件了，可以加 --force。")

    h = read_lines(hist)
    c = read_lines(cur)
    print(f"历史文件 {len(h)} 条 | 当前文件 {len(c)} 条")

    merged = []
    seen = set()
    for rid, ts, raw in h + c:
        key = rid or raw[:120]
        if key in seen:
            continue
        seen.add(key)
        merged.append((ts, raw))

    print(f"去重后 {len(merged)} 条")

    # 备份
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = cur.parent / f"{args.session}.premerge-{stamp}.jsonl"
    shutil.copy2(cur, bak)
    print(f"原文件已备份：{bak}")

    # 历史在前、当前在后。不按 timestamp 重排，避免打乱 parentId 链。
    body = "\n".join(raw for _, raw in merged) + "\n"
    cur.write_text(body, encoding="utf-8")
    print(f"已写回：{cur}  ({cur.stat().st_size} 字节)")

    # 校验：db 里的 cwd 必须指向这个文件所在的目录
    cfg = cur.parent.parent.parent
    db = cfg / "workbuddy.db"
    if db.exists():
        esc = cur.parent.name
        print()
        print(f"会话文件所在目录：{esc}")
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT cwd FROM sessions WHERE id=?",
                            (args.session,)).fetchone()
            con.close()
            if r:
                cwd = (r["cwd"] or "").replace(":", "-")
                cwd = cwd.replace("\\", "-").replace("/", "-")
                ok = cwd.lower() == esc.lower()
                print(f"数据库里的 cwd 推算是：{cwd}")
                print(f"两者一致：{'是' if ok else '否 —— 说明目录和数据库对不上，需要改回其中一个'}")
        except Exception as e:  # noqa: BLE001
            print(f"（校验数据库时出错，跳过：{e}）")

    print()
    print("完成。打开 WorkBuddy 看这条会话的历史是否回来了。")


if __name__ == "__main__":
    main()

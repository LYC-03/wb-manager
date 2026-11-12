"""对话里直接操作会话的入口。

为什么单独做一个：wb-sessions 技能原本只读真库、只写副本，为了安全。
但用户要「在对话里说一句就把事办了」，所以这里开一个受控的写通道——
每个写动作前自动快照，危险动作要显式加 --confirm。

用法（在对话里由模型调用）：
    python wbact.py list --filter change --limit 20
    python wbact.py show 4a8a42f8
    python wbact.py rename 4a8a42f8 "🛠️ 主题｜动作"
    python wbact.py archive 4a8a42f8 ee896545
    python wbact.py unarchive 4a8a42f8
    python wbact.py preview-move 4a8a42f8 "D:\\Projects\\X"
    python wbact.py move 4a8a42f8 --to "F:\\...\\X" --confirm
    python wbact.py unify-paths --confirm
    python wbact.py stats

所有输出都是 JSON，方便模型读。
"""
import argparse
import json
import sys
from pathlib import Path

WB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WB_DIR))

import wbmanager as W  # noqa: E402


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _match(items, prefix):
    """按 id 前缀匹配。前缀太短或匹配多条时报错，避免改错人。"""
    hit = [i for i in items if i["id"].startswith(prefix)]
    if not hit:
        raise SystemExit(f"没有以 {prefix} 开头的会话")
    if len(hit) > 1:
        names = ", ".join(f"{h['id'][:8]}={h['current'][:16]}" for h in hit[:5])
        raise SystemExit(f"前缀 {prefix} 匹配到 {len(hit)} 条，请写长一点：{names}")
    return hit[0]


def cmd_list(a):
    items = W.load_sessions()
    f = a.filter
    if f == "archived":
        items = [i for i in items if i["archived"]]
    elif f == "change":
        items = [i for i in items if i["changed"] and not i["archived"]]
    elif f == "auto":
        items = [i for i in items if i["auto"] and not i["archived"]]
    elif f == "manual":
        items = [i for i in items if not i["auto"] and not i["archived"]]
    elif f == "all":
        pass
    else:
        items = [i for i in items if not i["archived"]]
    total = len(items)
    if a.limit:
        items = items[:a.limit]
    _out({"total": total, "shown": len(items), "items": [
        {"id": i["id"][:8], "current": i["current"][:60],
         "suggest": i["suggest"][:60], "dir": i["dir"],
         "type": i["type"], "changed": i["changed"],
         "auto": i["auto"], "archived": i["archived"], "why": i["why"]}
        for i in items]})


def cmd_show(a):
    items = W.load_sessions()
    it = _match(items, a.id)
    _out(it)


def cmd_stats(a):
    items = W.load_sessions()
    _out(W.stats(items))


def cmd_rename(a):
    items = W.load_sessions()
    it = _match(items, a.id)
    if a.title == it["current"]:
        _out({"skipped": True, "why": "标题没变"})
        return
    r = W.apply_titles([{"id": it["id"], "title": a.title}])
    _out({"ok": True, "id": it["id"][:8], "from": it["current"], "to": a.title,
          "snapshot": r.get("snapshot"), "changed": r.get("changed")})


def cmd_apply_rule(a):
    """把规则建议直接应用到这几条。"""
    items = W.load_sessions()
    targets = [_match(items, x) for x in a.ids]
    pairs = [{"id": t["id"], "title": t["suggest"]} for t in targets
             if t["suggest"] and t["suggest"] != t["current"]]
    if not pairs:
        _out({"skipped": True, "why": "这些会话的标题已经符合规则"})
        return
    r = W.apply_titles(pairs)
    _out({"ok": True, "changed": r.get("changed"), "snapshot": r.get("snapshot"),
          "applied": [{"id": p["id"][:8], "title": p["title"]} for p in pairs]})


def cmd_archive(a):
    r = W.archive_sessions([_match(W.load_sessions(), x)["id"] for x in a.ids],
                           archive=True)
    _out(r)


def cmd_unarchive(a):
    r = W.archive_sessions([_match(W.load_sessions(), x)["id"] for x in a.ids],
                           archive=False)
    _out(r)


def cmd_preview_move(a):
    sid = _match(W.load_sessions(), a.id)["id"]
    _out(W.preview_move([{"id": sid, "cwd": a.to}]))


def cmd_move(a):
    sid = _match(W.load_sessions(), a.id)["id"]
    prev = W.preview_move([{"id": sid, "cwd": a.to}])
    if prev.get("error"):
        _out(prev)
        return
    item = prev["items"][0]
    if item["status"] != "可搬":
        _out({"refused": True, "why": item["why"]})
        return
    if not a.confirm:
        _out({"dry_run": True, "would_move": {
            "id": sid[:8], "from": item["from"], "to": item["to"],
            "size": item["size_h"]},
            "next": "确认无误后加 --confirm 再跑一次"})
        return
    _out(W.move_sessions([{"id": sid, "cwd": a.to}]))


def cmd_unify(a):
    scan = W.scan_path_styles()
    if not scan["total_groups"]:
        _out({"skipped": True, "why": "没有写法分裂"})
        return
    if not a.confirm:
        _out({"dry_run": True, "groups": scan["groups"],
              "next": "确认后加 --confirm"})
        return
    _out(W.unify_paths())


def build():
    ap = argparse.ArgumentParser(description="对话里直接操作 WorkBuddy 会话")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列会话")
    p.add_argument("--filter", default="change",
                   choices=["change", "all", "auto", "manual", "archived", ""])
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="看一条的完整信息")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("stats", help="总体统计")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("rename", help="改一条的标题")
    p.add_argument("id")
    p.add_argument("title")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("apply-rule", help="按规则建议批量改")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_apply_rule)

    p = sub.add_parser("archive", help="归档")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("unarchive", help="取消归档")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_unarchive)

    p = sub.add_parser("preview-move", help="预览搬目录，不动手")
    p.add_argument("id")
    p.add_argument("to")
    p.set_defaults(fn=cmd_preview_move)

    p = sub.add_parser("move", help="搬目录（要 --confirm 才真动）")
    p.add_argument("id")
    p.add_argument("--to", required=True)
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(fn=cmd_move)

    p = sub.add_parser("unify-paths", help="统一路径写法（要 --confirm 才真动）")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(fn=cmd_unify)

    return ap


if __name__ == "__main__":
    args = build().parse_args()
    args.fn(args)

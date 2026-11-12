r"""生成一个模拟的 WorkBuddy 数据环境，用于演示截图和跨机自测。

用法：python make_demo_env.py
产物：demo-workspace/（gitignore，不进仓库）
    demo-workspace/.workbuddy/workbuddy.db      假数据库
    demo-workspace/.workbuddy/projects/...      假正文文件
    demo-workspace/WorkBuddy/...                假默认工作空间

然后用环境变量启动服务（模拟一台只装过 WorkBuddy 的普通机器）：
    set WORKBUDDY_CONFIG_DIR=<路径>\.workbuddy
    python wbmanager.py --port 8767 --no-browser

里面没有任何真实用户数据，全是编造的演示会话。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "demo-workspace"
CFG = ROOT / ".workbuddy"
WS = ROOT / "WorkBuddy"

NOW = int(time.time() * 1000)
DAY = 24 * 3600 * 1000

# (会话id, 目录, 原始标题, 期望被规则改成什么样不重要, 状态, 更新时间偏移天数, 是否定时任务)
SESSIONS = [
    ("demo-web-01",   r"D:\Projects\webdemo",      "新建会话 2026-08-11 2030", "completed", 0,  0),
    ("demo-web-02",   r"D:\Projects\webdemo",      "再问一下布局的事",         "completed", 1,  0),
    ("demo-web-03",   r"D:\Projects\webdemo",      "新建会话 2026-08-03 0912", "completed", 8,  0),
    ("demo-fit-01",   r"D:\Personal\fitness",      "减脂期饮食怎么安排",       "completed", 2,  0),
    ("demo-fit-02",   r"D:\Personal\fitness",      "哑铃动作标准吗",           "completed", 5,  0),
    ("demo-pod-01",   r"D:\Media\podcast",         "第 14 期剪辑",             "working",   0,  0),
    ("demo-pod-02",   r"D:\Media\podcast",         "片头音乐选哪个",           "completed", 3,  0),
    ("demo-trip-01",  r"D:\Personal\travel",       "十一去哪人少",             "completed", 12, 0),
    ("demo-trip-02",  r"D:\Personal\travel",       "新建会话 2026-07-20 1455", "completed", 20, 0),
    ("demo-data-01",  r"D:\Work\datasets",         "CSV 合并去重脚本",         "completed", 6,  0),
    ("demo-data-02",  r"D:\Work\datasets",         "字段类型报错排查",         "completed", 9,  0),
    ("demo-sql-01",   r"D:\Work\datasets",         "导出报表定时任务",         "completed", 15, 1),
    ("demo-read-01",  r"D:\Personal\reading",      "书单整理",                 "completed", 30, 0),
    ("demo-auto-01",  r"C:\Users\demo\automation", "每日数据早报",             "completed", 1,  1),
    ("demo-old-01",   r"D:\Temp\scratch",          "临时测试一下",             "completed", 45, 0),
    ("demo-arch-01",  r"D:\Old\archive",           "去年的总结",               "archived",  90, 0),
]

# 软删除的会话：一条正文还在（能救），一条文件没了（救不回）
TRASH = [
    ("demo-del-01", r"D:\Projects\webdemo", "被误删的落地页讨论", 2, True),
    ("demo-del-02", r"D:\Temp\scratch",     "随手记的草稿",       6, False),
    ("demo-del-03", r"D:\Media\podcast",    "过期的话题清单",     10, True),
]

# 工作空间里的文件夹：空 / 有记忆 / 有文件 / 在用
WS_DIRS = {
    "2026-08-11-2030": [],                       # 空，可清理
    "2026-08-03-0912": [],                       # 空
    "automation-早报":  ["memory.md"],            # 有记忆，保护
    "数据导出":         ["result.csv", "说明.txt"],  # 有文件，保护
}


def esc_dir(cwd: str) -> str:
    r"""与 wbmanager.esc_dir_name 同规则：C:\x\y -> C-x-y（仅演示用，硬编码 Windows 写法）"""
    p = cwd.replace("/", "\\")
    p = p.replace(":\\", "-", 1)
    return p.replace("\\", "-")


def make_jsonl(sid: str, topic: str) -> str:
    lines = [
        {"type": "user", "content": f"帮我处理一下{topic}，先看看现状。"},
        {"type": "assistant", "content": f"好的，我先梳理{topic}的现有结构，然后分两步做。"},
        {"type": "user", "content": "可以，注意别把已有的东西改坏。"},
        {"type": "assistant", "content": f"{topic}处理完成，改动都做了备份。"},
    ]
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"


def main():
    if ROOT.exists():
        import shutil
        shutil.rmtree(ROOT)
    CFG.mkdir(parents=True)
    WS.mkdir(parents=True)

    db = CFG / "workbuddy.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, cwd TEXT, title TEXT, custom_title TEXT,
        status TEXT, created_at INTEGER, updated_at INTEGER,
        is_background_automation INTEGER, is_playground INTEGER, deleted_at INTEGER
    )""")
    rows = []
    for sid, cwd, title, status, days_ago, is_auto in SESSIONS:
        ts = NOW - days_ago * DAY
        rows.append((sid, cwd, title, None, status, ts - 60000, ts, is_auto, 0, None))
    for sid, cwd, title, days_ago, file_ok in TRASH:
        ts = NOW - days_ago * DAY
        rows.append((sid, cwd, title, None, "completed", ts - 60000, ts, 0, 0, NOW - days_ago * DAY))
    con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    proj = CFG / "projects"
    for sid, cwd, title, status, days_ago, is_auto in SESSIONS:
        d = proj / esc_dir(cwd)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text(make_jsonl(sid, title), encoding="utf-8")
    # 存活会话全有正文；软删的只有 file_ok 的有
    for sid, cwd, title, days_ago, file_ok in TRASH:
        if not file_ok:
            continue
        d = proj / esc_dir(cwd)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text(make_jsonl(sid, title), encoding="utf-8")

    for name, files in WS_DIRS.items():
        d = WS / name
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("演示占位内容\n", encoding="utf-8")

    print(f"[demo] 已生成：{ROOT}")
    print(f"[demo] 数据库：{db}（{len(SESSIONS)} 条存活 + {len(TRASH)} 条软删）")
    print("[demo] 启动演示服务：")
    print(f'  set WORKBUDDY_CONFIG_DIR={CFG}')
    print("  python wbmanager.py --port 8767 --no-browser")


if __name__ == "__main__":
    sys.exit(main())

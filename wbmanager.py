"""wb-manager：WorkBuddy 会话管理器（独立工具，不依赖插件）。

直接读写 WorkBuddy 的数据库，能干对话里干不了的事：批量改标题、移到空间、归档。

启动：
    python wbmanager.py            # 起服务并自动开浏览器
    python wbmanager.py --port 8766
    python wbmanager.py --no-browser

安全：
    只监听 127.0.0.1，外网访问不到。
    每次写操作前自动拍快照到 backups/，可一键还原。
    只改 custom_title 和 cwd 两个字段，不碰会话内容。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

def _app_dir() -> Path:
    """数据目录：配置、备份、历史都放这里，跟着 exe（或脚本）走。"""
    if getattr(sys, "frozen", False):          # 已打包成 exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _res_dir() -> Path:
    """只读资源目录：开发时就是代码目录，打包后是临时解包目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


HERE = _app_dir()      # 可写：config.json / backups / history.jsonl
RES = _res_dir()       # 只读：ui.html
IS_WIN = sys.platform.startswith("win")


def _fatal(msg: str, code: int = 1):
    """启动阶段就失败时怎么告诉用户。

    打包成 exe 是 --noconsole，stdout 没有任何人看得到——只 print 的话，
    用户双击后就是「什么也没发生」。所以打包版一律弹原生提示框。

    放在文件这么靠前的位置，是因为数据库定位在模块导入时就会跑，
    等不到 main()——没有可写目录、环境变量指错都要在这一刻说清楚。
    """
    print(msg)
    if getattr(sys, "frozen", False) and IS_WIN:
        try:
            import ctypes
            # MB_ICONERROR | MB_SETFOREGROUND，保证从后台启动也能弹到前面
            ctypes.windll.user32.MessageBoxW(
                None, msg, "wb-manager 没能启动", 0x10 | 0x10000)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(code)


def _box(msg: str, title: str, icon: int = 0x40):
    """弹一个原生提示框。打包版没有控制台，结果只能这么告诉用户。

    设了环境变量 WB_SILENT 就只打印不弹框，方便自动化验收时跑一遍不打扰人。
    """
    print(msg)
    if not IS_WIN or os.environ.get("WB_SILENT"):
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, title,
                                         icon | 0x10000)   # SETFOREGROUND
    except Exception:  # noqa: BLE001
        pass


def _shortcut_icon():
    """给桌面快捷方式找一份独立图标，返回路径；找不到返回 None。

    为什么不让快捷方式直接引用 exe 的图标：Windows 是按 exe 路径缓存图标的，
    换掉 exe 里的资源之后，桌面常常还显示旧图标，用户会以为「根本没换」——
    上一版就踩了这个，重新打包、重建快捷方式都做了，桌面还是老样子。

    所以这里把 ico 单独复制一份到程序目录，文件名带上内容哈希：
    图标一改，文件名就变，不存在命中旧缓存的可能。
    旧的那份顺手删掉，免得目录里越堆越多。
    """
    for src in (RES / "icon.ico", HERE / "icon.ico", HERE / "docs" / "icon.ico"):
        if not src.is_file():
            continue
        try:
            data = src.read_bytes()
        except OSError:
            continue
        dst = HERE / f"wb-manager-{hashlib.sha1(data).hexdigest()[:8]}.ico"
        try:
            if not dst.is_file():
                dst.write_bytes(data)
            for old in HERE.glob("wb-manager-*.ico"):
                if old.name != dst.name:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            return dst
        except OSError:
            return None
    return None


def install_desktop_shortcut():
    """在桌面建一个指向本程序的快捷方式。

    为什么不用 PowerShell：`安装到桌面.bat` 原本靠 powershell 调 WScript.Shell 的
    COM 对象，但有些机器上安全策略会直接拦掉 COM 实例化，脚本一行没执行就退出，
    用户看到的现象就是「双击了，桌面什么也没多」。这里改成 ctypes 直接调
    Windows 自带的 IShellLink，不经过 PowerShell、也不需要装 pywin32。
    """
    import ctypes
    from ctypes import wintypes, POINTER, byref, c_void_p, c_uint, c_int, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def _guid(s):
        g = GUID()
        if ctypes.windll.ole32.CLSIDFromString(c_wchar_p(s), byref(g)) != 0:
            raise OSError(f"CLSID 解析失败: {s}")
        return g

    def _call(obj, index, restype, *argtypes):
        """取 COM 虚表第 index 个函数并调它。前 3 个固定是 QI/AddRef/Release。"""
        vt = ctypes.cast(obj, POINTER(c_void_p))[0]
        fn = ctypes.cast(vt, POINTER(c_void_p))[index]
        return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)

    # 目标：打包版就是自己；源码运行就找同目录的 exe，找不到就用 python 本体
    exe = sys.executable if getattr(sys, "frozen", False) else ""
    if not exe:
        cand = HERE / "wb-manager.exe"
        exe = str(cand) if cand.exists() else sys.executable
    if not Path(exe).exists():
        _box(f"找不到要指向的程序：\n  {exe}\n\n把本文件和 wb-manager.exe 放在同一个文件夹再试。",
             "没建成快捷方式", 0x30)
        return 1

    desk = ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        # CSIDL_DESKTOPDIRECTORY = 0x0010，桌面被挪到别的盘也能拿到真实位置
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:
            desk = buf.value
    except Exception:  # noqa: BLE001
        pass
    if not desk:
        _box("读不到桌面路径。可以直接把 wb-manager.exe 拖到桌面，效果一样。",
             "没建成快捷方式", 0x30)
        return 1

    # 名字和界面标题一致。顺带避开一个坑：Windows 的图标缓存是按
    # 「快捷方式路径」做 key 的，同名重建永远命中旧条目，改完图标桌面
    # 还显示旧的。换个新名字就是新 key，缓存必然 miss，图标当场生效。
    lnk = Path(desk) / "会话管家.lnk"

    # 旧名字留在桌面上的那个一并清掉，免得出现两个看起来一样的图标
    for old_name in ("WorkBuddy 会话整理.lnk",):
        old = Path(desk) / old_name
        if old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)
    link = c_void_p()
    persist = c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            byref(_guid("{00021401-0000-0000-C000-000000000046}")), None, 1,
            byref(_guid("{000214F9-0000-0000-C000-000000000046}")), byref(link))
        if hr != 0 or not link:
            raise OSError(f"创建 IShellLink 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        # 20 SetPath / 9 SetWorkingDirectory / 7 SetDescription / 17 SetIconLocation
        _call(link, 20, ctypes.HRESULT, c_wchar_p, wintypes.DWORD)(link, exe, 0)
        _call(link, 9, ctypes.HRESULT, c_wchar_p)(link, str(HERE))
        _call(link, 7, ctypes.HRESULT, c_wchar_p)(
            link, "WorkBuddy 会话整理 · 批量改名 / 移到空间 / 产物合集")
        # 图标优先用独立 ico（带内容哈希，改图标必生效），拿不到才退回 exe 自带
        icon = _shortcut_icon()
        _call(link, 17, ctypes.HRESULT, c_wchar_p, c_int)(
            link, str(icon or exe) + ",0", 0)

        # QueryInterface 拿 IPersistFile，Save 才真正写盘
        hr = _call(link, 0, ctypes.HRESULT, POINTER(GUID), POINTER(c_void_p))(
            link, byref(_guid("{0000010b-0000-0000-C000-000000000046}")),
            byref(persist))
        if hr != 0 or not persist:
            raise OSError(f"取 IPersistFile 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        hr = _call(persist, 6, ctypes.HRESULT, c_wchar_p, wintypes.BOOL)(
            persist, str(lnk), True)
        if hr != 0:
            raise OSError(f"写快捷方式失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    except OSError as e:
        _box(f"建快捷方式时出错：\n  {e}\n\n"
             "可以退一步：直接把 wb-manager.exe 拖到桌面，效果一样。",
             "没建成快捷方式", 0x10)
        return 1
    finally:
        try:
            if persist:
                _call(persist, 2, ctypes.c_ulong)(persist)
            if link:
                _call(link, 2, ctypes.c_ulong)(link)
            ole32.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass

    _box(f"已经在桌面建好了：\n  {lnk}\n\n以后直接双击桌面那个图标就行，"
         f"不用再进这个文件夹。", "装好了", 0x40)
    return 0


def check_app_dir_writable():
    """数据目录能不能写。写不进去就别让它跑到一半再崩。

    config.json / backups / history.jsonl / port.txt 都放在 exe 旁边。
    放进 C:\\Program Files 这种要管理员权限的目录时，启动写 port.txt 就会
    抛 PermissionError，--noconsole 下用户看到的是「双击没反应」。
    所以先试写一个小文件，写不了就当场说清楚。
    """
    probe = HERE / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False

# 用 pythonw.exe（无窗口启动器）跑时没有控制台，stdout/stderr 是 None，print 会崩。
# 换成空设备，所有 print 照常安全执行；真正需要看输出时用 python.exe 启动。
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

UI = RES / "ui.html"
HISTORY_KEEP = 200          # 只保留最近这些条，文件不会无限长
CONFIG = HERE / "config.json"   # API 配置与库无关，所有库共用一份

sys.path.insert(0, str(HERE))
import rules  # noqa: E402

MAX_BACKUPS = 50
# 快照的「写 + 清理旧份」必须串行：两个请求同时进来会算出同一批旧文件，
# 后一个删到已经不存在的那份，直接把整次操作拖成失败。
_SNAP_LOCK = threading.Lock()
_SNAP_SEQ = 0

# WorkBuddy 用环境变量覆盖配置目录，优先级与程序本体一致。
# 见 app.asar：configDirOverride || WORKBUDDY_CONFIG_DIR || CODEBUDDY_CONFIG_DIR || ~/.workbuddy
CONFIG_ENVS = ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR")


def resolve_config_dir():
    """返回 (配置目录, 命中来源说明)。

    按 WorkBuddy 自己的查找顺序走，这样用户把数据放哪都能找到。
    环境变量明确指定了、但那个目录不存在时**直接停下**：静默回退到
    ~/.workbuddy 的话，用户会以为整理的是自己指定的那份，实际动的是
    另一份数据——这种「安静地改错库」比报错难发现得多。
    """
    for env in CONFIG_ENVS:
        v = (os.environ.get(env) or "").strip()
        if not v:
            continue
        if Path(v).is_dir():
            return Path(v), f"环境变量 {env}"
        _fatal(
            "环境变量指向的文件夹不存在：\n"
            f"  {env} = {v}\n\n"
            "工具不会自己换成另一份数据——那样你可能整理了半天，"
            "才发现动的是另一个库。\n\n"
            "两个办法：\n"
            "1. 把这个路径改对（数据盘是不是没挂载？），再启动\n"
            f"2. 删掉这个环境变量，让工具用默认位置（{Path.home() / '.workbuddy'}）"
        )
    home_dir = Path.home() / ".workbuddy"
    if home_dir.is_dir():
        return home_dir, "用户目录 ~/.workbuddy"
    return home_dir, "用户目录 ~/.workbuddy（尚未创建）"


def resolve_db(cli_db=None):
    """返回 (数据库路径, 说明)。找不到就返回预期位置，让调用方报错。"""
    if cli_db:
        return Path(cli_db).expanduser(), "命令行 --db"
    v = (os.environ.get("WORKBUDDY_DB") or "").strip()
    if v:
        return Path(v).expanduser(), "环境变量 WORKBUDDY_DB"
    cfg, why = resolve_config_dir()
    return cfg / "workbuddy.db", why


REAL_DB, DB_SOURCE = resolve_db()

# 备份、操作历史、命名导出都跟「当前这个库」绑定，不跟工具目录绑定。
# 以前这三样直接放在数据目录根上，一旦用 --db 换了库（或数据盘换位置），
# 另一套库的东西就串进来了：备份列表里能看见别的库的快照，
# 点「还原」还会把别的库的标题和目录套到这份库上。
# 所以按库路径取短哈希分子目录。只有一个库的用户由下面的迁移兜住，观感不变。
def _per_db_paths(db: Path):
    """按数据库路径算出这个库自己的备份/命名/历史位置。"""
    key = hashlib.sha1(str(db).lower().encode("utf-8")).hexdigest()[:8]
    return (HERE / "backups" / key,                       # backups/<库>/snap-*.json
            HERE / "naming" / key,                        # naming/<库>/request-*.json
            HERE / "history" / key / "history.jsonl")     # history/<库>/history.jsonl


BACKUPS, NAMING, HISTORY = _per_db_paths(REAL_DB)
_BOOT_DB = REAL_DB        # 启动时认定的库；main() 里被 --db 改掉时要重新算路径


def _migrate_legacy_data() -> None:
    """把老版本放在数据目录根上的备份/历史/命名包搬进按库分的子目录。

    只在目标里还没有东西时搬一次，搬不动就留着（宁可少搬，不可丢）。
    不搬的话，升级后用户会发现「我的备份全没了」——其实还在老位置。
    """
    legacy_backups = HERE / "backups"
    if legacy_backups.is_dir() and legacy_backups != BACKUPS:
        olds = list(legacy_backups.glob("snap-*.json"))
        if olds:
            BACKUPS.mkdir(parents=True, exist_ok=True)
            for f in olds:
                try:
                    f.replace(BACKUPS / f.name)
                except OSError:
                    pass
    legacy_hist = HERE / "history.jsonl"
    if legacy_hist.is_file() and not HISTORY.exists():
        try:
            HISTORY.parent.mkdir(parents=True, exist_ok=True)
            legacy_hist.replace(HISTORY)
        except OSError:
            pass
    legacy_naming = HERE / "naming"
    if legacy_naming.is_dir() and legacy_naming != NAMING:
        olds = list(legacy_naming.glob("*.json"))
        if olds:
            NAMING.mkdir(parents=True, exist_ok=True)
            for f in olds:
                try:
                    f.replace(NAMING / f.name)
                except OSError:
                    pass


def _ensure_dirs() -> None:
    """把当前库要用的目录建出来，后面所有 mkdir(exist_ok=True) 都不会缺父目录。

    写不进去是另一回事，那种情况由 main() 里的 check_app_dir_writable 明确告知。
    """
    for d in (BACKUPS, NAMING, HISTORY.parent):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


_migrate_legacy_data()
_ensure_dirs()


# ---------- 数据层 ----------

def connect(write=False):
    """打开数据库。

    读用普通连接 + PRAGMA query_only，比 URI 的 mode=ro 稳，
    在 Windows 上 URI 带盘符容易踩坑。

    timeout 压到 3 秒：WorkBuddy 正开着写库时，等 10 秒用户只会觉得
    「点了没反应」，不如早点失败、给一句能看懂的话（见 _db_busy_msg）。
    """
    con = sqlite3.connect(REAL_DB, timeout=3)
    con.row_factory = sqlite3.Row
    if not write:
        con.execute("PRAGMA query_only=ON")
    return con


def _db_busy_msg(err) -> str:
    """把 SQLite 的英文异常翻成用户能自己处理的一句话。"""
    low = str(err).lower()
    if "locked" in low or "busy" in low:
        return ("数据库正被占用：WorkBuddy（或另一个整理窗口）正在写它。\n"
                "把 WorkBuddy 关掉，或者等几秒再点一次就好。")
    if "no such table" in low:
        return ("这个文件里没有会话表，可能不是 WorkBuddy 的数据库，"
                "或者 WorkBuddy 还没初始化完。")
    if "readonly" in low or "unable to open" in low:
        return "数据库打不开：可能被占用或没有读写权限，试试以管理员身份运行。"
    return f"读写数据库失败：{err}"


ACTIVE_WINDOW = 180  # 正文文件 3 分钟内还在写，就认为正在使用


def _is_active(row, now, file_of):
    """这条会话是不是正被使用。两种迹象：

    1) status 是 working —— WorkBuddy 自己标的运行中
    2) 正文文件 3 分钟内还在被写 —— 你正在这个会话里说话

    搬动正在写的会话会把历史切成两截，上次事故就是这么来的，
    所以界面上要显眼标出来。
    """
    if (row.get("status") or "").lower() == "working":
        return "running"
    try:
        f = file_of(row.get("cwd"), row["id"])
        if f.exists() and (now - f.stat().st_mtime) < ACTIVE_WINDOW:
            return "recent"
    except OSError:
        pass
    return ""


def space_for_cwd(con, cwd, create=True):
    """取这个目录在 WorkBuddy 里对应的空间 id。

    新版本（spaces 表 + sessions.space_id）侧栏是按「空间」分组的，不再按
    cwd 字符串分组。所以改目录必须连 space_id 一起改：只改 cwd 的话，文件
    搬走了、界面上那条会话还挂在旧空间下，两边对不上，用户会以为白搬了。

    找不到对应空间就照它自己的规则补一个（name 取目录尾名、source_kind=seed）。
    老版本没有 spaces 表 → 返回 None，调用方就只改 cwd，行为和以前一样。
    """
    target = rules.norm_path(cwd or "").lower()
    if not target:
        return None
    try:
        for r in con.execute(
                "SELECT id, create_folder_path, primary_folder_path FROM spaces"
                " WHERE deleted_at < 0"):
            for p in (r["create_folder_path"], r["primary_folder_path"]):
                # 同一个目录被写成正反斜杠两种样子也算同一个空间
                if p and rules.norm_path(p).lower() == target:
                    return r["id"]

        if not create:
            return None

        # user_id / device_id 是 WorkBuddy 自己的身份，照抄一份现成的，别猜
        base = con.execute(
            "SELECT user_id, device_id FROM spaces WHERE deleted_at < 0"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if base is None:
            return None

        sid = str(uuid.uuid4())
        now = _now_ms()
        con.execute(
            "INSERT INTO spaces (id, user_id, device_id, name, primary_folder_path,"
            " create_folder_path, pinned, sort_order, status, transport,"
            " conversation_origin, source_kind, deleted_at, created_at, updated_at)"
            " VALUES (?,?,?,?,NULL,?,0,NULL,'active','local',NULL,'seed',-1,?,?)",
            (sid, base["user_id"], base["device_id"],
             rules.tail_dir(cwd) or cwd, cwd, now, now))
        return sid
    except sqlite3.OperationalError:
        # 没升级的 WorkBuddy 只有 cwd 一套分组，不需要这个
        return None


def load_sessions():
    con = connect()
    try:
        # 新版本：带空间归属和「最近一次活跃」时间。
        # 侧栏现在按 space 分组、按 last_activity_at 排「最近」，
        # 老查法只看 updated_at，和界面上看到的顺序对不上。
        rows = [dict(r) for r in con.execute("""
            SELECT s.id, s.cwd, s.title, s.custom_title, s.status,
                   s.created_at, s.updated_at, s.last_activity_at, s.space_id,
                   s.is_background_automation, s.is_playground, s.deleted_at,
                   sp.name AS space_name, sp.pinned AS space_pinned
            FROM sessions s
            LEFT JOIN spaces sp ON sp.id = s.space_id AND sp.deleted_at < 0
            WHERE s.deleted_at IS NULL
            ORDER BY COALESCE(s.last_activity_at, s.updated_at, s.created_at) DESC
        """).fetchall()]
    except sqlite3.OperationalError:
        # 没升级的 WorkBuddy：没有空间表，退回按目录分组的老查法
        rows = [dict(r) for r in con.execute("""
            SELECT id, cwd, title, custom_title, status, created_at, updated_at,
                   is_background_automation, is_playground, deleted_at
            FROM sessions
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC
        """).fetchall()]
    con.close()

    out = []
    ai_titles = load_naming()
    naming_style = (load_config().get("style") or "emoji")
    rules.set_custom_rules(load_config().get("custom_types") or [])
    # 判断「正在使用」要看正文文件多久没动，同一个目录只查一次，别每条都遍历 projects
    now = time.time()
    _dir_cache = {}

    def _session_file(cwd, sid):
        key = rules.norm_path(cwd or "").lower()
        if key not in _dir_cache:
            _dir_cache[key] = find_esc_dir(cwd)
        return _dir_cache[key] / f"{sid}.jsonl"

    for r in rows:
        cur = (r["custom_title"] or "").strip()
        raw = (r["title"] or "").strip()
        s = rules.suggest(r, style=naming_style)
        after = s["after"]
        source = "rule"
        why = s["why"]
        if r["id"] in ai_titles:
            after = ai_titles[r["id"]]
            source = "ai"
            why = "AI 读过对话内容，按实际做的事命名"
        elif s["conflict"] and cur:
            # 自己起过名、规则没认出类型：不再是「待确认」，改成如实说明
            why = "名字是你自己定的；规则只是认不出该归哪个类型，不影响使用"
        out.append({
            "id": r["id"],
            "cwd": r["cwd"] or "",
            "cwd_norm": rules.norm_path(r["cwd"] or ""),
            "dir": rules.tail_dir(r["cwd"]),
            # 空间：新版本侧栏的分组单位。空表示这条会话还没归属任何空间
            "space_id": r.get("space_id") or "",
            "space": (r.get("space_name") or "").strip(),
            "space_pinned": bool(r.get("space_pinned")),
            # 「最近」按这个排：它比 updated_at 更贴近界面上看到的活跃时间
            "last_active": (r.get("last_activity_at")
                            or r.get("updated_at") or r.get("created_at") or 0),
            "current": cur or raw,
            "raw": raw[:120],
            "has_custom": bool(cur),
            "suggest": after,
            "source": source,
            "emoji": s["emoji"],
            "type": s["type_name"],
            "why": why,
            # 需确认只留给「从来没起过名、且规则没认出」的会话。
            # 用户自己改过名的（custom_title 非空）说明人家已经定了性，
            # 规则认不出类型 ≠ 还需要确认，不该天天挂着催。
            "conflict": s["conflict"] and source == "rule" and not cur,
            "auto": bool(r["is_background_automation"]),
            "archived": (r["status"] or "").lower() == "archived",
            "active": _is_active(r, now, _session_file),
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            # 用户自己改过名的（has_custom）不算「待改」——那是人家定的名字，
            # 工具不该天天催着改；只有没起过名、又不合规的才进待改清单
            "changed": (not cur) and after != raw,
        })
    return out


def stats(items):
    groups = {}
    dup = {}
    # 有空间数据就按空间算（新库），一条空间都没有才退回按目录算（老库）
    has_space = any((i.get("space") or "").strip() for i in items)
    for i in items:
        if has_space:
            key = (i.get("space") or "").strip() or "未归属"
        else:
            key = i["cwd_norm"] or "未归属"
        groups.setdefault(key, 0)
        groups[key] += 1
        dup.setdefault(i["current"], 0)
        dup[i["current"]] += 1

    dup_groups = {k: v for k, v in dup.items() if v > 1}
    return {
        "total": len(items),
        "auto": sum(1 for i in items if i["auto"]),
        "manual": sum(1 for i in items if not i["auto"]),
        "archived": sum(1 for i in items if i["archived"]),
        "need_change": sum(1 for i in items if i["changed"] and not i["archived"]),
        "conflict": sum(1 for i in items if i["conflict"] and not i["archived"]),
        "groups": len(groups),
        "dup_groups": len(dup_groups),
        "dup_extra": sum(v - 1 for v in dup_groups.values()),
        "dup_detail": sorted(
            [{"title": k, "count": v} for k, v in dup_groups.items()],
            key=lambda x: -x["count"])[:10],
    }


# ---------- 备份 ----------

def snapshot(tag="manual"):
    """把全部会话的标题、目录、状态存成 json 快照。

    带 status 是为了归档能还原——归档改的就是 status，
    恢复时得知道原来是 completed 还是 working。

    整个「写文件 + 清理旧份」都在锁里串行做：两个请求同时进来会算出
    同一个毫秒时间戳，文件名撞车时后一个写不进去（Windows 直接 PermissionError）；
    清理阶段也会重复删同一份。串行化之后这两个坑都不存在，代价只是几十毫秒。
    """
    global _SNAP_SEQ
    BACKUPS.mkdir(exist_ok=True)
    with _SNAP_LOCK:
        con = connect()
        rows = [dict(r) for r in con.execute(
            "SELECT id, custom_title, title, cwd, status FROM sessions").fetchall()]
        con.close()
        # 毫秒 + 进程内序号：同一毫秒连做两次也不会撞名
        _SNAP_SEQ += 1
        stamp = (time.strftime("%Y%m%d-%H%M%S")
                 + f"-{int(time.time() * 1000) % 1000:03d}-{_SNAP_SEQ:04d}")
        name = f"snap-{stamp}-{tag}.json"
        p = BACKUPS / name
        p.write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tag": tag,
            "count": len(rows),
            "rows": rows,
        }, ensure_ascii=False), encoding="utf-8")

        # 只留最近 N 份。逐份删、删不掉就跳过——杀软扫描、OneDrive 同步，
        # 都可能让 unlink 失败；为一份旧快照把整次改名拖垮不值得。
        olds = sorted(BACKUPS.glob("snap-*.json"))
        for f in olds[:-MAX_BACKUPS]:
            try:
                f.unlink()
            except OSError:
                continue
    return p.name


def undo_snapshot(name):
    """撤回某一步操作。

    关键差别：改标题、归档只要改数据库；但移到空间改的是 cwd，
    光把 cwd 改回去而磁盘文件不动，会话就点不开了——WorkBuddy 是按
    projects/<目录名>/<会话id>.jsonl 找正文的。

    所以这里先算目录差异、把文件反向搬回去，再套用快照里的其余字段。
    """
    p = BACKUPS / name
    if not p.exists():
        return {"error": "这份快照已经被新的备份挤掉了，撤不回来"}

    d = json.loads(p.read_text(encoding="utf-8"))
    want = {r["id"]: r for r in d.get("rows") or []}

    con = connect()
    now = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, cwd FROM sessions").fetchall()}
    con.close()

    back = []
    for sid, old in want.items():
        cur = now.get(sid)
        if not cur:
            continue
        if rules.norm_path(cur["cwd"] or "") != rules.norm_path(old.get("cwd") or ""):
            back.append({"id": sid, "cwd": old.get("cwd")})

    moved = 0
    blocked = []
    if back:
        if not IS_WIN:
            return {"error": "撤回移到空间要搬动磁盘文件，这个功能只在 Windows 上可用"}
        r = move_sessions(back)
        if r.get("error"):
            return {"error": r["error"]}
        moved = r.get("changed", 0)
        blocked = r.get("blocked") or []

    rest = restore(name)
    rest["moved_files"] = moved
    if blocked:
        rest["blocked"] = blocked
    return rest


# WorkBuddy 自动建的目录长这样：时间戳、automation- 开头、纯数字。
# 这些不是你起的项目名，所以里面的会话算「散着」。
LOOSE_PAT = re.compile(r"^(?:\d{4}-\d{2}-\d{2}(?:-\d{2}-\d{2}-\d{2})?|automation-.*|\d{10,})$")


def plan_rehome(create_root=None):
    """把散落在自动生成目录里的会话挑出来，给每条一个建议去处。

    判定「散落」只看目录名，别的不猜。建议去处也只走明摆着的线索：
    标题里出现了某个现有项目的名字，就去那个项目；认不出的空着让你自己点，
    绝不瞎猜——猜错了搬来搬去，比不搬更麻烦。

    目标目录不存在时，会在推荐的位置建，而不是往 C 盘塞。
    """
    items = [i for i in load_sessions() if not i["archived"]]

    known = {}
    for i in items:
        d = i["dir"]
        if d and not LOOSE_PAT.match(d):
            known.setdefault(d, i["cwd"])

    if not create_root:
        # 新项目该建在专门的项目目录里，不是建在默认空间。
        # 项目根位置 = 现有项目目录最常见的那个父目录；一个项目都没有时退回扫盘建议。
        import collections
        parents = collections.Counter(str(Path(p).parent) for p in known.values() if p)
        if parents:
            create_root = parents.most_common(1)[0][0]
        else:
            try:
                create_root = str(Path(scan_disks()["suggest_root"]))
            except Exception:  # noqa: BLE001
                create_root = ""

    rows = []
    for i in items:
        d = i["dir"] or ""
        if not LOOSE_PAT.match(d):
            continue
        sug, why = "", "认不出属于哪个项目，你来点"
        for name, path in known.items():
            if name and name in (i["current"] or ""):
                sug, why = path, f"标题里有「{name}」"
                break
        rows.append({
            "id": i["id"],
            "title": i["current"],
            "from": i["cwd"],
            "from_dir": d,
            "suggest": sug,
            "why": why,
            "active": bool(i.get("active")),
        })

    rows.sort(key=lambda x: (x["from_dir"], x["title"]))
    return {
        "items": rows,
        "count": len(rows),
        "known_projects": [{"name": k, "path": v} for k, v in sorted(known.items())],
        "create_root": create_root,
        "root_exists": bool(create_root) and Path(create_root).is_dir(),
    }


def _is_link(p):
    """这个目录是不是联接/软链。Windows 上用 junction 挪数据很常见。"""
    p = Path(p)
    try:
        if not p.exists():
            return False
        if p.is_symlink():
            return True
        checker = getattr(p, "is_junction", None)
        if callable(checker):
            return bool(checker())
        # 老版本 Python 没有 is_junction，退而求其次比真实路径
        return os.path.realpath(str(p)).lower() != str(p).lower()
    except OSError:
        return False


def migration_status():
    """看 WorkBuddy 的数据放在哪，落在 C 盘就给出迁移建议。

    要分清楚两块东西，它们不是一回事：
    - 配置目录（.workbuddy）：数据库、projects 里的对话正文、技能、记忆
    - 默认工作空间（同级的 WorkBuddy）：开对话时自动建的文件夹

    两块都可能落在 C 盘。落 C 盘的问题不是「不能用」，是系统盘紧张时
    容易被清理工具碰到，而且每次读写都要过一遍用户目录。

    这里只做检测和引导，不自动搬——搬错一个链接，WorkBuddy 会直接打不开。
    """
    cfg = REAL_DB.parent
    ws = _ws_root()

    def info(p):
        p = Path(p)
        exists = p.exists()
        return {
            "path": str(p),
            "exists": exists,
            "on_system_drive": str(p).upper().startswith("C:"),
            "is_link": _is_link(p) if exists else False,
            "real_path": os.path.realpath(str(p)) if exists else "",
        }

    c, w = info(cfg), info(ws)

    # 还要看「标准位置」——也就是 WorkBuddy 默认会用的 %USERPROFILE%\.workbuddy。
    # 很多人（包括本机）用 junction 把它指到别的盘，但设置界面里显示的仍是 C 盘路径。
    std = Path.home() / ".workbuddy"
    s = info(std)
    same = False
    try:
        if std.exists() and Path(cfg).exists():
            same = os.path.realpath(str(std)).lower() == os.path.realpath(str(cfg)).lower()
    except OSError:
        pass
    s["points_to_current"] = same

    need = []
    if c["on_system_drive"] and not c["is_link"]:
        need.append("config")
    if w["exists"] and w["on_system_drive"] and not w["is_link"]:
        need.append("workspace")

    # 已经用联接挪走了但显示还是 C 盘的，单独说清楚：数据其实不在 C 盘
    link_only = bool(s["exists"] and s["is_link"] and same)

    return {
        "config": c,
        "workspace": w,
        "standard": s,
        "need_migrate": need,
        "ok": not need,
        "linked_but_shows_c": link_only,
        "hint": ("数据都在系统盘以外，或者已经用链接指到别的盘了，不用动。"
                 if not need else
                 "有数据还在 C 盘，下面有挪走的做法。挪之前务必完全退出 WorkBuddy。"),
    }


def scan_disks():
    """扫一遍本机磁盘，挑一个适合放工作空间的位置。

    判断依据只有三条，别的不猜：
    1) 可用空间（要放回忆录级别的会话，太小没意义）
    2) 是不是系统盘（C 盘放着容易出事，能挪就挪）
    3) 这个盘上有没有现成的 AI 工作目录（有的话放在一起，侧栏不会散）
    """
    import string
    out = []
    for ch in string.ascii_uppercase:
        root = Path(f"{ch}:\\")
        try:
            if not root.exists():
                continue
            du = shutil.disk_usage(str(root))
        except OSError:
            continue

        free_gb = du.free / (1024 ** 3)
        total_gb = du.total / (1024 ** 3)
        is_sys = (ch.upper() == "C")

        # 有没有现成的工作目录，以及上面是否已经有 WorkBuddy 工作空间
        # 只看这些目录下面的 WorkBuddy，不扫盘根的——盘根那个多半是程序安装目录
        hints = []
        existing_ws = []
        for n in ("WorkBuddy", "AI", "AI-Work"):
            p = root / n
            try:
                if not p.is_dir():
                    continue
            except OSError:
                continue
            hints.append(str(p))
            w = p / "WorkBuddy"
            try:
                # 盘上那个 WorkBuddy 可能是程序安装目录，不是工作空间。
                # 认一下：有 electron 许可文件或 exe 的是程序，跳过。
                if w.is_dir() and not any(
                        (w / m).exists() for m in
                        ("LICENSE.electron.txt", "WorkBuddy.exe", "resources/app.asar")):
                    existing_ws.append(str(w))
            except OSError:
                pass

        score = 0
        if not is_sys:
            score += 40
        if free_gb >= 100:
            score += 30
        elif free_gb >= 50:
            score += 22
        elif free_gb >= 20:
            score += 14
        elif free_gb >= 5:
            score += 6
        if hints:
            score += 25
        if existing_ws:
            score += 30          # 已经有现成工作空间的盘，优先选它，别让人再挪一次

        out.append({
            "drive": f"{ch.upper()}:",
            "free_gb": round(free_gb, 1),
            "total_gb": round(total_gb, 1),
            "is_system": is_sys,
            "hints": hints,
            "existing_ws": existing_ws,
            "score": score,
        })

    out.sort(key=lambda x: -x["score"])
    best = out[0] if out else None

    # 推荐逻辑：正在用的位置如果已经不在系统盘，就别让人再挪一次；
    # 只有落在 C 盘（或还没有）时，才给一个更好的盘。
    cur = _ws_root()
    cur_on_sys = str(cur).upper().startswith("C:")
    suggest = ""
    if cur.is_dir() and not cur_on_sys:
        suggest = str(cur)
    elif best:
        if best["existing_ws"]:
            suggest = best["existing_ws"][0]
        elif best["hints"]:
            suggest = str(Path(best["hints"][0]) / "WorkBuddy")
        else:
            # 没有任何现成目录：推荐盘根下建一个 WorkBuddy 工作空间
            # （WorkBuddy 的默认工作空间名就叫 WorkBuddy，对所有用户一致）
            suggest = f"{best['drive']}\\WorkBuddy"

    return {"drives": out, "best": best, "suggest_root": suggest,
            "current_root": str(cur),
            "current_on_system_drive": cur_on_sys}


WORKSPACE_ROOT_NAMES = ("memory.md", "MEMORY.md", "USER.md", "SOUL.md", "IDENTITY.md")


def _ws_root():
    """默认工作空间：配置目录的父目录下的 WorkBuddy。

    WorkBuddy 没把「默认工作目录」存进配置，是推导出来的：
    配置目录（.workbuddy）的上一级 + WorkBuddy。所以搬家挪盘之后这里会跟着变。
    """
    return REAL_DB.parent.parent / "WorkBuddy"


def scan_workspaces():
    """扫默认工作空间里的各个文件夹，判断哪些是闲置的。

    最关键的一条：里面有 memory.md 就不能删——那是 AI 的记忆，
    删了等于把你在那个工作空间里养出来的东西清空。
    所以这里只允许删「完全空」的，其余只报告。

    判定的四种状态：
    - inuse  还有存活会话在用它，动都不能动
    - memory 留着记忆文件，要处理也先迁走再删
    - files  留着其他文件，得自己看一眼
    - empty  空的，可以清
    """
    root = _ws_root()
    if not root.is_dir():
        return {"root": str(root), "exists": False, "items": [],
                "total": 0, "cleanable": 0, "total_size": 0}

    con = connect()
    used = {}
    for r in con.execute("""SELECT cwd, COUNT(*) c FROM sessions
                            WHERE deleted_at IS NULL GROUP BY cwd"""):
        used[rules.norm_path(r["cwd"] or "").lower()] = r["c"]
    con.close()

    items = []
    total_size = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        key = rules.norm_path(str(d)).lower()
        n_sess = used.get(key, 0)

        files = []
        for p in d.rglob("*"):
            try:
                if p.is_file() and p.name.lower() != "nul":
                    files.append(p)
            except OSError:
                continue
        size = sum(p.stat().st_size for p in files)
        total_size += size

        mems = [p for p in files if p.name in WORKSPACE_ROOT_NAMES]
        others = [p for p in files if p.name not in WORKSPACE_ROOT_NAMES]

        if n_sess:
            state, why = "inuse", f"还有 {n_sess} 条会话放在这"
        elif mems:
            state, why = "memory", f"留着 {len(mems)} 个记忆文件"
        elif others:
            state, why = "files", f"留着 {len(others)} 个文件"
        else:
            state, why = "empty", "空的"

        items.append({
            "name": d.name,
            "path": str(d),
            "sessions": n_sess,
            "files": len(files),
            "size": size,
            "size_h": human_size(size),
            "memory_files": [p.name for p in mems],
            "other_files": [p.name for p in others[:6]],
            "state": state,
            "why": why,
            "cleanable": state == "empty",
        })

    order = {"inuse": 0, "memory": 1, "files": 2, "empty": 3}
    items.sort(key=lambda x: (order.get(x["state"], 9), -x["size"]))
    return {
        "root": str(root),
        "exists": True,
        "items": items,
        "total": len(items),
        "cleanable": sum(1 for x in items if x["cleanable"]),
        "total_size": total_size,
        "total_size_h": human_size(total_size),
    }


def clean_workspaces(paths):
    """清掉闲置的工作空间文件夹。

    只做一件事：删完全空的目录。有任何文件就整体拒绝，不递归删内容——
    这个工具不负责判断哪个文件重要。
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {"error": "没有选中要清理的文件夹", "removed": 0}

    root = _ws_root().resolve()
    removed, blocked = [], []
    for raw in paths:
        try:
            d = Path(raw).resolve()
        except OSError as e:
            blocked.append({"path": raw, "why": f"路径不可用：{e}"})
            continue

        # 只能在默认工作空间里动，别的地方一律不碰
        try:
            d.relative_to(root)
        except ValueError:
            blocked.append({"path": raw, "why": "不在默认工作空间里，不处理"})
            continue
        if d == root:
            blocked.append({"path": raw, "why": "这是工作空间根目录，不能删"})
            continue
        if not d.is_dir():
            blocked.append({"path": raw, "why": "文件夹已经不存在"})
            continue

        left = [p for p in d.iterdir()]
        # Windows 上目录里偶尔有个名叫 nul 的幽灵文件，stat 都会失败，
        # 按名字排除掉，否则任何目录都「还有内容」
        real = [p for p in left if p.name.lower() != "nul"]
        if real:
            blocked.append({"path": raw,
                            "why": f"里面还有 {len(real)} 项内容，不删（要删请自己处理）"})
            continue
        try:
            for p in left:
                if p.name.lower() != "nul":
                    continue
                try:
                    p.unlink()
                except OSError:
                    pass
            d.rmdir()
            removed.append(d.name)
        except OSError as e:
            blocked.append({"path": raw, "why": f"删不掉：{e}"})

    if removed:
        log_action("清理闲置工作空间", len(removed), None,
                   "、".join(removed[:5]) + ("…" if len(removed) > 5 else ""))
    return {"removed": len(removed), "names": removed, "blocked": blocked}


def read_workspace_memory(path):
    """只读地看一眼某个闲置空间里养出来的记忆（memory.md 原文）。

    给「这个日期名字的文件夹到底是什么任务」提供判断依据，
    看完可以决定改名或留着，这里不写不删。
    """
    try:
        d = Path(path).resolve()
        root = _ws_root().resolve()
        d.relative_to(root)
    except (OSError, ValueError):
        return {"error": "只能看默认工作空间里的文件夹"}
    if not d.is_dir():
        return {"error": "文件夹不存在"}
    mems = [p for p in d.rglob("*") if p.is_file() and p.name in WORKSPACE_ROOT_NAMES]
    if not mems:
        return {"error": "这个文件夹里没有记忆文件"}
    out = []
    for m in mems[:3]:
        try:
            text = m.read_text(encoding="utf-8", errors="replace")
            out.append({"file": m.name, "chars": len(text),
                        "head": text[:1200], "truncated": len(text) > 1200})
        except OSError as e:
            out.append({"file": m.name, "error": str(e)})
    return {"items": out}


def log_action(action, changed, snapshot=None, note=""):
    """记一条写操作，供「操作历史」查看和撤回。

    只追加、不改旧记录。改坏了最怕「不知道刚才动了什么」，
    这条日志配上当时的快照名，就能原路退回去。
    """
    rec = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": int(time.time()),
        "action": action,
        "changed": int(changed or 0),
        "snapshot": snapshot,
        "note": note or "",
    }
    try:
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim_history()
    except OSError:
        pass
    return rec


def _trim_history():
    """日志只留最近 HISTORY_KEEP 条，避免越攒越大。"""
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
        if len(lines) <= HISTORY_KEEP:
            return
        keep = lines[-HISTORY_KEEP:]
        HISTORY.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def list_history(limit=30):
    if not HISTORY.exists():
        return {"items": [], "count": 0}
    rows = []
    try:
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return {"items": [], "count": 0}
    rows.reverse()
    # 标出哪条还能撤回：快照文件还在的才行
    for r in rows[:limit]:
        snap = r.get("snapshot")
        r["can_undo"] = bool(snap and (BACKUPS / snap).exists())
    return {"items": rows[:limit], "count": len(rows)}


def list_backups():
    BACKUPS.mkdir(exist_ok=True)
    out = []
    for f in sorted(BACKUPS.glob("snap-*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": f.name, "at": d.get("at"), "tag": d.get("tag"),
                        "count": d.get("count"), "size": f.stat().st_size})
        except (ValueError, OSError):
            continue
    return out


def delete_backup(name):
    """手动删一份快照。快照只是「撤回用的存档」，删掉不影响会话数据本身，
    损失的只是那一步撤不回。只认文件名里的 snap-*.json，防路径穿越。"""
    name = Path(name).name
    if not (name.startswith("snap-") and name.endswith(".json")):
        return {"error": "只允许删本工具生成的 snap-*.json 快照"}
    p = BACKUPS / name
    if not p.exists():
        return {"error": "快照不存在（可能已被新的备份挤掉）"}
    p.unlink()
    return {"ok": True, "left": len(list_backups())}


# ---------- 写操作 ----------

def apply_titles(pairs):
    """pairs: [{id, title}]。只改 custom_title，不动其他字段。"""
    if not pairs:
        return {"changed": 0, "snapshot": None}
    snap = snapshot("before-apply")
    con = connect(write=True)
    changed = 0
    skipped_auto = 0
    try:
        with con:
            for p in pairs:
                # 控制字符（\x00-\x1f）粘进来会让标题在界面上显示成空白，
                # 之后既搜不到也认不出，统一洗成空格再收。
                t = str(p.get("title") or "")
                t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
                t = re.sub(r"\s+", " ", t).strip()[:200]
                if not t or not p.get("id"):
                    continue
                cur = con.execute(
                    "SELECT custom_title, is_background_automation FROM sessions WHERE id=?",
                    (p["id"],)).fetchone()
                if cur is None:
                    continue
                # 定时任务的标题归 WorkBuddy 管，这里一律不写。
                # 闸设在这一层是因为上面还有 AI 命名、手动应用、一键整理
                # 好几个入口，逐个堵容易漏；这里堵住，所有路径都过不去。
                if cur["is_background_automation"]:
                    skipped_auto += 1
                    continue
                if (cur["custom_title"] or "") == t:
                    continue
                con.execute("UPDATE sessions SET custom_title=? WHERE id=?", (t, p["id"]))
                changed += 1
    finally:
        con.close()
    log_action("改标题", changed, snap, f"{len(pairs)} 条提交")
    out = {"changed": changed, "snapshot": snap}
    if skipped_auto:
        out["skipped_auto"] = skipped_auto
    return out


_AUTO_PREFIX = re.compile(
    r"^((?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\uFE0F?\s*)+)(.*)$", re.S)


def auto_title_fix(title):
    """把定时任务标题里重复叠出来的部分收敛掉，返回修好的标题。

    旧版规则引擎对定时任务每次都往前面补一个 🔁 和一段日期，而且不先剥旧的，
    所以跑两次就成了「🔁 🔁 美团领券｜09-29｜09-29」，跑三次更长。

    这里只做两件最保守的事：前缀只留一个、相邻重复的段去掉。
    不改主体文字、不改日期，结果形如「🔁 美团领券｜09-29」。
    没有需要收敛的地方就返回 None，让调用方跳过。
    """
    raw = re.sub(r"\s+", " ", (title or "").strip())
    if not raw:
        return None
    m = _AUTO_PREFIX.match(raw)
    if m:
        prefix, rest = m.group(1).strip(), m.group(2).strip()
        syms = [c for c in prefix if not c.isspace() and c != "\uFE0F"]
        uniq = []
        for c in syms:
            if not uniq or uniq[-1] != c:
                uniq.append(c)
        prefix = " ".join(uniq)
    else:
        prefix, rest = "", raw
    segs = [s.strip() for s in rest.split("｜")]
    dedup = []
    for s in segs:
        if s and (not dedup or dedup[-1] != s):
            dedup.append(s)
    if not dedup:
        return None
    body = "｜".join(dedup)
    fixed = f"{prefix} {body}" if prefix else body
    return fixed if fixed != raw else None


def auto_title_issues():
    """列出标题被叠重复了的定时任务，给「一次性收敛」用。"""
    out = []
    for i in load_sessions():
        if not i["auto"]:
            continue
        fixed = auto_title_fix(i["current"])
        if fixed:
            out.append({"id": i["id"], "current": i["current"], "after": fixed})
    return out


def fix_auto_titles(dry_run=False):
    """把定时任务被叠重复的标题收敛回单层。

    这是修工具自己造成的损坏，不是给定时任务改名——目标格式
    （🔁 名称｜日期）本来就是旧版第一次运行时的结果。
    以后不再跑：suggest() 对定时任务已经直接返回原名，apply_titles 也拒绝写。
    """
    issues = auto_title_issues()
    if dry_run or not issues:
        return {"count": len(issues), "items": issues[:80],
                "changed": 0, "dry_run": bool(dry_run)}
    snap = snapshot("before-auto-fix")
    con = connect(write=True)
    changed = 0
    try:
        with con:
            for it in issues:
                # 只认定时任务，防止手误传进普通会话的 id
                con.execute(
                    "UPDATE sessions SET custom_title=? "
                    "WHERE id=? AND is_background_automation=1",
                    (it["after"], it["id"]))
                changed += 1
    finally:
        con.close()
    log_action("收敛定时任务标题", changed, snap, f"{len(issues)} 条")
    return {"count": len(issues), "changed": changed, "snapshot": snap,
            "need_restart": True}


def esc_dir_name(cwd):
    """把 cwd 转成 WorkBuddy 的 projects 子目录名。

    实测规则（仅 Windows）：F:\\AI\\x -> F-AI-x
    （盘符后的 :\\ 合成一个减号，其余 \\ 各变减号）
    本机 98 条会话全部符合这个规则，无一例外。

    ⚠️ macOS / Linux 上的规则没有验证过。Unix 路径本身以 / 开头，
    首字符会变成减号，实际 WorkBuddy 怎么处理未知。
    所以非 Windows 平台上搬目录是被禁掉的（见 preview_move），
    只保留改标题、归档这些不碰路径的操作。
    """
    p = rules.norm_path(cwd or "")
    if not IS_WIN:
        # Unix 路径：把 / 变成 -，去掉开头的减号
        return re.sub(r"[\\/]", "-", p).lstrip("-")
    p = re.sub(r":\\", "-", p)
    return re.sub(r"[\\/]", "-", p)


def session_file(sid, cwd):
    """会话正文文件路径：<配置目录>/projects/<转义cwd>/<会话id>.jsonl"""
    cfg = REAL_DB.parent
    return cfg / "projects" / esc_dir_name(cwd) / f"{sid}.jsonl"


def wb_running():
    """WorkBuddy 是否在跑。

    踩过坑：tasklist 的输出是 GBK，用 text=True 解码会抛 UnicodeDecodeError，
    异常被吞掉后这里恒返回 False，防护等于没有。所以按 bytes 比，不解码。
    macOS / Linux 没有 tasklist，改用 pgrep。
    """
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
                                 capture_output=True, timeout=8)
            return b"workbuddy" in (out.stdout or b"").lower()
        out = subprocess.run(["pgrep", "-fi", "workbuddy"],
                             capture_output=True, timeout=8)
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def is_active(path, seconds=180):
    """这条会话是不是正在被使用。

    正在写会话时搬它的文件，等于在别人打字时拿走他的纸。
    jsonl 三分钟内被改过就认为在用。
    """
    try:
        return (time.time() - path.stat().st_mtime) < seconds
    except OSError:
        return False


def find_esc_dir(cwd):
    """按 WorkBuddy 的规则算目录名，并尽量复用磁盘上已存在的那个。

    NTFS 不区分大小写但保留原样，所以 'F-AI-x' 和 'f-AI-x' 可能是同一个目录。
    遍历一次 projects，忽略大小写找已存在的名字，避免凭空造出重复目录。
    """
    want = esc_dir_name(cwd)
    proj = REAL_DB.parent / "projects"
    try:
        if proj.is_dir():
            low = want.lower()
            for child in proj.iterdir():
                if child.is_dir() and child.name.lower() == low:
                    return child
    except OSError:
        pass
    return proj / want


def export_naming(ids=None, include_auto=False):
    """导出命名请求包：把每条会话的对话摘要攒成一个 json，交给模型读。

    为什么不内置 AI：工具跑在本地，没有模型可用。把摘要导出、让 WorkBuddy
    会话里的模型来命名，既零成本又比关键词规则准。导出只读会话文件，不改任何东西。
    """
    items = load_sessions()
    picked = [i for i in items if i["id"] in ids] if ids else \
             [i for i in items if include_auto or not i["auto"]]

    req = []
    for i in picked:
        f = session_file(i["id"], i["cwd"])
        d = rules.read_digest(f)
        req.append({
            "id": i["id"],
            "current": i["current"],
            "dir": i["dir"],
            "cwd": i["cwd"],
            "auto": i["auto"],
            "ai_title": d["ai_title"],
            "user_said": d["users"],
            "tools": d["tools"][:10],
            "rule_suggest": i["suggest"],
            "rule_type": i["type"],
        })

    NAMING.mkdir(exist_ok=True)
    name = f"request-{time.strftime('%Y%m%d-%H%M%S')}.json"
    p = NAMING / name
    p.write_text(json.dumps({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ask": "给每条会话起名，格式：<类型emoji> <主题>｜<这次做了什么>，"
               f"类型只能是 {type_menu()}。"
               "主题不超过 14 字，取这次真正干的事，不要照抄 user_said。"
               "返回严格 JSON：{\"titles\":[{\"id\":\"...\",\"title\":\"...\"}]}",
        "count": len(req),
        "items": req,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"file": str(p), "name": name, "count": len(req),
            "items": req[:3]}


def import_naming(payload):
    """导入模型给出的命名结果，存成 latest.json，界面据此显示 AI 建议。

    只存建议，不直接改库。要落到库里还得用户点应用。
    """
    titles = payload.get("titles") or []
    if not isinstance(titles, list):
        return {"error": "格式不对，需要 {\"titles\":[{\"id\",\"title\"}]}"}

    clean = {}
    for t in titles:
        if not isinstance(t, dict):
            continue
        sid = (t.get("id") or "").strip()
        title = re.sub(r"\s+", " ", (t.get("title") or "").strip())[:200]
        if sid and title:
            clean[sid] = title
    if not clean:
        return {"error": "没解析出任何有效标题"}

    NAMING.mkdir(exist_ok=True)
    (NAMING / "latest.json").write_text(json.dumps({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "titles": clean,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"imported": len(clean)}


def load_naming():
    p = NAMING / "latest.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("titles") or {}
    except (OSError, ValueError):
        return {}


KEEP_NAMING = 5

DEFAULT_CFG = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",
    "batch": 20,
    "style": "emoji",   # 命名风格模板：emoji / plain / date
    "custom_types": [],  # 自定义类型规则：[{emoji,name,keys}]，界面可配
}


def load_config():
    """读配置。没有就返回默认值，不写文件。"""
    cfg = dict(DEFAULT_CFG)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def public_config():
    """给界面看的配置。密钥只回掩码，不回明文。"""
    cfg = load_config()
    key = cfg.get("api_key") or ""
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("已设置" if key else "")
    return {"base_url": cfg["base_url"], "model": cfg["model"],
            "batch": cfg.get("batch", 20),
            "style": cfg.get("style", "emoji"),
            "custom_types": cfg.get("custom_types", []),
            "has_key": bool(key), "key_mask": masked}


def save_config(payload):
    """只接受已知字段。空字符串表示「不改」，避免误清空已存的 key。"""
    cfg = load_config()
    for k in ("base_url", "model"):
        v = (payload.get(k) or "").strip()
        if v:
            cfg[k] = v
    key = (payload.get("api_key") or "").strip()
    if key and key != "***":
        cfg["api_key"] = key
    if payload.get("clear_key"):
        cfg["api_key"] = ""
    try:
        cfg["batch"] = max(5, min(40, int(payload.get("batch") or cfg.get("batch", 20))))
    except (TypeError, ValueError):
        pass
    if payload.get("style") in ("emoji", "plain", "date"):
        cfg["style"] = payload["style"]
    ct = payload.get("custom_types")
    if isinstance(ct, list):
        clean = []
        for it in ct:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            keys = [str(k).strip() for k in (it.get("keys") or []) if str(k).strip()]
            if name and keys:
                clean.append({"emoji": str(it.get("emoji") or "").strip() or "🏷️",
                              "name": name, "keys": keys[:30]})
        cfg["custom_types"] = clean[:50]
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(CONFIG, 0o600)   # 只有本人可读，聊胜于无
    except OSError:
        pass
    return public_config()


def type_menu() -> str:
    """给模型看的类型清单：从规则表实时生成。

    写死七个的时候，用户后来加的 14 类（写作/健康/运营…）AI 永远用不上，
    所以这里跟着 TYPE_RULES 走，加类型不用再改提示词。
    """
    return " ".join(f"{e}{n}" for e, n, _ in rules.TYPE_RULES)


def naming_prompt() -> str:
    return (
        "你是给程序员整理工作记录的人。下面是一批 WorkBuddy 会话的摘要，"
        "每条有：当前标题、所在目录、模型给过的旧标题、用户说过的话、用过的工具。\n\n"
        "给每条重新起名，格式固定为：<类型emoji> <主题>｜<这次做了什么>\n"
        f"类型只能从这些里选一个：{type_menu()}\n"
        "要求：\n"
        "1. 主题不超过 14 个字，写清「做的是什么东西」，不要抄用户的原话。\n"
        "2. 竖线后面写「这件事进行到哪一步」或「这次的产出」，不超过 12 个字。\n"
        "3. 信息不够就按目录和工具猜，别留空，别写「未知」。\n"
        "4. 不输出任何解释，只返回 JSON。\n\n"
        "返回格式：{\"titles\":[{\"id\":\"原样返回的id\",\"title\":\"起的名字\"}]}"
    )


def call_llm(cfg, items):
    """调一次模型，返回 (titles_dict, usage_dict)。失败抛异常。"""
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": naming_prompt()},
            {"role": "user", "content": json.dumps(
                [{"id": i["id"], "当前标题": i["current"], "目录": i["dir"],
                  "旧标题": i["ai_title"], "用户说过": i["user_said"][:3],
                  "工具": i["tools"][:8]} for i in items],
                ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))

    text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    # 模型有时会套 ```json，剥掉
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("模型没返回 JSON")
        parsed = json.loads(m.group(0))

    titles = {}
    for t in parsed.get("titles") or []:
        if isinstance(t, dict) and t.get("id") and t.get("title"):
            titles[t["id"]] = re.sub(r"\s+", " ", str(t["title"]).strip())[:200]
    return titles, d.get("usage") or {}


def naming_auto(ids=None):
    """用 API 批量命名。没配 key 直接报错，让前端退回手动通道。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        return {"error": "还没配置 API key。要么填一个，要么用「导出命名请求」走手动。"}

    items = load_sessions()
    picked = [i for i in items if i["id"] in ids] if ids else [i for i in items if not i["auto"]]
    if not picked:
        return {"error": "没有需要命名的会话"}

    # 把摘要凑齐
    payload = []
    for i in picked:
        d = rules.read_digest(find_esc_dir(i["cwd"]) / f"{i['id']}.jsonl")
        payload.append({"id": i["id"], "current": i["current"], "dir": i["dir"],
                        "ai_title": d["ai_title"], "user_said": d["users"],
                        "tools": d["tools"]})

    batch = int(cfg.get("batch") or 20)
    got = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    errors = []
    for n, start in enumerate(range(0, len(payload), batch), 1):
        chunk = payload[start:start + batch]
        try:
            titles, u = call_llm(cfg, chunk)
            got.update(titles)
            for k in usage:
                usage[k] += int(u.get(k) or 0)
        except Exception as e:  # noqa: BLE001
            errors.append(f"第 {n} 批失败：{type(e).__name__}: {e}")

    if not got:
        return {"error": "全部批次都失败了。" + ("；".join(errors) if errors else "")}

    NAMING.mkdir(exist_ok=True)
    old = {}
    p = NAMING / "latest.json"
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8")).get("titles") or {}
        except (OSError, ValueError):
            pass
    old.update(got)
    p.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "source": "api", "titles": old},
                            ensure_ascii=False, indent=1), encoding="utf-8")

    return {"imported": len(got), "usage": usage, "errors": errors,
            "batches": (len(payload) + batch - 1) // batch}


def human_size(n):
    if n >= 1024 ** 3:
        return f"{n/1024**3:.1f} GB"
    if n >= 1024 * 1024:
        return f"{n/1024/1024:.1f} MB"
    if n >= 1024:
        return f"{n/1024:.0f} KB"
    return f"{n} B"


def storage_info():
    """这个工具自己占了多少地方。只统计，不删。"""
    def stat_dir(d, pat="*"):
        files = [f for f in (sorted(d.glob(pat)) if d.is_dir() else []) if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        return {"count": len(files), "bytes": size, "human": human_size(size)}

    naming_reqs = stat_dir(NAMING, "request-*.json")

    # 正文目录总占用（会话越多越大，这是 WorkBuddy 占空间的大头）
    proj = REAL_DB.parent / "projects"
    proj_files = [f for f in proj.rglob("*") if f.is_file()] if proj.is_dir() else []
    proj_bytes = sum(f.stat().st_size for f in proj_files)

    # 已删除会话的残留文件：正文 + 同名附件目录还躺在盘上，纯垃圾
    junk_count = 0
    junk_bytes = 0
    con = connect()
    del_rows = con.execute(
        "SELECT id, cwd FROM sessions WHERE deleted_at IS NOT NULL").fetchall()
    con.close()
    for r in del_rows:
        f = session_file(r["id"], r["cwd"])
        if f.exists():
            junk_count += 1
            junk_bytes += f.stat().st_size
            att = f.parent / f.stem
            if att.is_dir():
                junk_bytes += sum(g.stat().st_size for g in att.rglob("*") if g.is_file())

    # 数据库所在盘的剩余空间（C 盘紧张的用户最关心这个）
    try:
        drive_free = shutil.disk_usage(REAL_DB.anchor).free
    except OSError:
        drive_free = 0

    return {
        "backups": stat_dir(BACKUPS, "snap-*.json"),
        "naming_requests": naming_reqs,
        "naming_keep": KEEP_NAMING,
        "naming_extra": max(0, naming_reqs["count"] - KEEP_NAMING),
        "total_human": human_size(stat_dir(BACKUPS, "snap-*.json")["bytes"]
                                  + naming_reqs["bytes"]),
        "projects_human": human_size(proj_bytes),
        "projects_count": len(proj_files),
        "junk_count": junk_count,
        "junk_human": human_size(junk_bytes),
        "drive": REAL_DB.anchor,
        "drive_free_human": human_size(drive_free),
    }


def clean_storage():
    """清掉多余的命名导出包。只动本工具自己生成的文件。"""
    if not NAMING.is_dir():
        return {"deleted": 0, "freed": "0 B"}
    reqs = sorted([f for f in NAMING.glob("request-*.json") if f.is_file()],
                  key=lambda f: f.stat().st_mtime, reverse=True)
    drop = reqs[KEEP_NAMING:]
    freed = 0
    for f in drop:
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return {"deleted": len(drop), "freed": human_size(freed)}


ARCHIVE_ORIG = HERE / "archive-orig.json"


def _now_ms():
    return int(time.time() * 1000)


def _load_orig():
    if ARCHIVE_ORIG.exists():
        try:
            return json.loads(ARCHIVE_ORIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def _save_orig(d):
    ARCHIVE_ORIG.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def _ms_to_str(ms):
    """WorkBuddy 的时间都是毫秒时间戳，转成看得懂的写法。"""
    if not ms:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (ValueError, OSError, OverflowError):
        return "—"


# ============================================================================
# 产物合集
#
# WorkBuddy 给每个会话记一份产物清单：<配置目录>/artifact-index/<会话ID>.json，
# 每条含 uri（file:/// 开头的真实路径）、大小、类型、是哪个工具产出的。
# 索引只记路径、不存副本，所以文件被清理后索引还在——必须逐条查存在性，
# 否则界面上会把一堆早已不存在的文件当成「我的产物」列出来。
#
# 项目归属靠会话反查：产物 -> 会话 -> cwd -> 项目。cwd 有正反斜杠两种写法，
# 必须先规范化再聚合，否则同一个项目会裂成两个文件夹。
# ============================================================================

_ART_TTL = 10.0
_ART_CACHE = {"t": 0.0, "data": None}
_ART_MAX_READ = 2 * 1024 * 1024      # 单个文本产物最大读取字节，防大文件拖慢扫描

_ART_KINDS = [
    ("image",   {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"}),
    ("video",   {".mp4", ".mov", ".webm", ".mkv", ".avi"}),
    ("audio",   {".mp3", ".wav", ".m4a", ".flac", ".ogg"}),
    ("code",    {".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
                 ".py", ".json", ".css", ".bat", ".cmd", ".ps1", ".sh", ".sql",
                 ".yml", ".yaml", ".toml", ".ini", ".java", ".go", ".rs", ".c", ".cpp", ".h"}),
    ("doc",     {".md", ".txt", ".pdf", ".docx", ".doc", ".rtf"}),
    ("sheet",   {".xlsx", ".xls", ".csv", ".tsv"}),
    ("slide",   {".pptx", ".ppt"}),
    ("archive", {".zip", ".7z", ".rar", ".tar", ".gz"}),
]
_ART_TEXT_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".vue", ".md", ".svg"}
_ART_ENTRY_NAMES = {"index.html", "index.htm", "main.py", "app.py", "server.py",
                    "start.cmd", "启动.bat", "README.md", "readme.md"}

_REF_RE = re.compile(r"""(?:src|href|data-src)\s*=\s*["']([^"'>]+)""", re.I)
_CSS_RE = re.compile(r"""url\(\s*["']?([^"')]+)""", re.I)


def _art_index_dir() -> Path:
    """产物索引目录，永远跟着当前这个库走（--db 换库时自动跟着换）。"""
    return Path(REAL_DB).parent / "artifact-index"


def _art_trash_dir() -> Path:
    """产物回收站，按库分目录。每次现算，避免 main() 换库后指向旧目录。"""
    key = hashlib.sha1(str(REAL_DB).lower().encode("utf-8")).hexdigest()[:8]
    return HERE / "artifact-trash" / key


def _art_trash_manifest() -> Path:
    return _art_trash_dir() / "manifest.jsonl"


def _uri_to_path(uri: str):
    """file:///F:/... 转成 Path。注意 file:/// 是 8 个字符，多截一位会多出斜杠。"""
    if not uri:
        return None
    try:
        u = urlparse(uri)
    except ValueError:
        return None
    p = unquote(u.path or "")
    if not p:
        return None
    if p.startswith("/") and len(p) > 2 and p[2] == ":":
        p = p[1:]
    return Path(p.replace("/", "\\")) if IS_WIN else Path(p)


def _art_kind(ext: str) -> str:
    ext = (ext or "").lower()
    for kind, exts in _ART_KINDS:
        if ext in exts:
            return kind
    return "file"


def _art_id(path) -> str:
    """产物 ID：按路径取稳定哈希，同一文件多次出现算同一个。"""
    return hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:12]


def _art_refs_of(path: Path, base: Path):
    """从一个文本产物里找出它引用的本地文件，返规范化后的候选路径集合。

    只解析「同目录/子目录」的相对引用——产物散落在整个磁盘上，
    跟着 ../.. 一路外扩会把无关文件误判成依赖。
    """
    out = set()
    try:
        if path.stat().st_size > _ART_MAX_READ:
            return out
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return out
    for raw in list(_REF_RE.findall(text)) + list(_CSS_RE.findall(text)):
        ref = (raw or "").strip()
        if not ref or ref.startswith(("http://", "https://", "#", "data:",
                                      "javascript:", "mailto:", "file://", "//")):
            continue
        ref = ref.split("?")[0].split("#")[0]
        if not ref:
            continue
        try:
            target = (path.parent / ref).resolve()
        except (OSError, ValueError):
            continue
        try:
            target.relative_to(base)      # 只认同目录/子目录内
        except ValueError:
            continue
        if target.is_file():
            out.add(str(target))
    return out


def scan_artifacts(force: bool = False):
    """扫产物索引，按项目合并成文件夹，并算出依赖关系。

    返回里每个产物带三样关键信息：
      exists         文件还在不在（不在的不能打开、也没必要删）
      ref_by         谁引用了我（非空 = 删了会让别人的页面破掉）
      version_index  同名文件里的第几版（1 = 最新）
    """
    now = time.time()
    if not force and _ART_CACHE["data"] and now - _ART_CACHE["t"] < _ART_TTL:
        return _ART_CACHE["data"]

    idx_dir = _art_index_dir()
    if not idx_dir.is_dir():
        data = {"has_index": False, "index_dir": str(idx_dir), "projects": [],
                "items": [], "stats": {"total": 0, "exists": 0, "missing": 0,
                                       "size": 0, "projects": 0, "protected": 0,
                                       "bundles": 0}, "trash": art_trash_list()}
        _ART_CACHE.update(t=now, data=data)
        return data

    # ---- 1. 读索引，摊平成产物列表 ----
    raw_items = []
    bad_idx = 0
    for f in idx_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bad_idx += 1
            continue
        sid = f.stem
        for a in d.get("artifacts") or []:
            if not isinstance(a, dict):
                continue
            p = _uri_to_path(a.get("uri") or "")
            if not p:
                continue
            raw_items.append({
                "path": p,
                "name": a.get("name") or a.get("title") or p.name,
                "mime": a.get("mimeType") or "",
                "ctype": a.get("contentType") or "",
                "size_idx": int(a.get("size") or 0),
                "created": int(a.get("createdAt") or 0),
                "updated": int(a.get("updatedAt") or 0),
                "tool": ((a.get("_meta") or {}).get("sourceTool") or ""),
                "sid": sid,
            })

    # ---- 2. 会话 -> 项目（cwd 规范化） ----
    sess = {}
    try:
        con = connect()
        for r in con.execute("SELECT id, cwd, title, custom_title FROM sessions"):
            sess[r["id"]] = dict(r)
        con.close()
    except sqlite3.Error:
        pass

    # ---- 3. 逐条查存在性，组装 ----
    items = []
    seen = {}
    for it in raw_items:
        p: Path = it["path"]
        try:
            st = p.stat()
            exists, size, mtime = True, st.st_size, int(st.st_mtime * 1000)
        except (OSError, ValueError):
            exists, size, mtime = False, it["size_idx"], it["updated"]

        s = sess.get(it["sid"]) or {}
        cwd_raw = (s.get("cwd") or "").strip()
        proj_key = rules.norm_path(cwd_raw) if cwd_raw else ""
        proj_name = rules.tail_dir(cwd_raw) if cwd_raw else "未归属"

        aid = _art_id(p)
        if aid in seen:                     # 同一文件被多个会话记过，只留一条
            continue
        rec = {
            "id": aid,
            "path": str(p),
            "name": it["name"],
            "ext": p.suffix.lower(),
            "kind": _art_kind(p.suffix),
            "size": size,
            "mtime": mtime,
            "exists": exists,
            "is_entry": p.name.lower() in {n.lower() for n in _ART_ENTRY_NAMES},
            "project": proj_name,
            "project_key": proj_key or "(未归属)",
            "project_path": cwd_raw,
            "session_id": it["sid"],
            "session_title": (s.get("custom_title") or s.get("title") or "")[:60],
            "tool": it["tool"],
            "refs": [],
            "ref_by": [],
            "version_index": 1,
            "version_total": 1,
            "protected": False,
            "protect_reason": "",
        }
        seen[aid] = rec
        items.append(rec)

    # ---- 4. 依赖检测：只对存在的文本产物读内容 ----
    by_path = {}
    for it in items:
        if it["exists"]:
            try:
                by_path[str(Path(it["path"]).resolve()).lower()] = it
            except (OSError, ValueError):
                pass

    budget = 96 * 1024 * 1024          # 本次扫描允许读取的总字节
    for it in items:
        if not it["exists"] or it["ext"] not in _ART_TEXT_EXT:
            continue
        p = Path(it["path"])
        try:
            sz = p.stat().st_size
        except (OSError, ValueError):
            continue
        if sz > budget:
            continue
        budget -= sz
        try:
            base = p.parent.resolve()
        except (OSError, ValueError):
            base = p.parent
        for tgt in _art_refs_of(p, base):
            hit = by_path.get(tgt.lower())
            if hit and hit["id"] != it["id"]:
                it["refs"].append(hit["id"])
                hit["ref_by"].append(it["id"])
        if budget <= 0:
            break

    # ---- 5. 同名版本链（同一项目 + 同一文件名） ----
    # 按时间正序编号：v1 是最早那版，序号最大的就是最新版。
    # 反过来的话界面上会把最新版标成 v1，看着像旧文件。
    byname = {}
    for it in items:
        byname.setdefault((it["project_key"], it["name"].lower()), []).append(it)
    for group in byname.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (x["mtime"], x["size"]))
        last = len(group) - 1
        for i, it in enumerate(group):
            it["version_index"] = i + 1
            it["version_total"] = len(group)
            it["is_latest"] = (i == last)

    # ---- 6. 删除保护：被引用 / 目录入口 / 同名最新版 ----
    for it in items:
        if it["ref_by"]:
            it["protected"] = True
            it["protect_reason"] = f"被 {len(it['ref_by'])} 个产物引用，删了那些会打不开或破图"
        elif it["is_entry"] and it["version_total"] == 1:
            it["protected"] = True
            it["protect_reason"] = "这是所在目录的入口文件，删掉整个产出就打不开了"
        elif it.get("is_latest"):
            it["protected"] = True
            it["protect_reason"] = f"同名文件里最新的一版（共 {it['version_total']} 版），删了就没法回退"

    # ---- 7. 捆绑组：同一会话 + 同一目录 = 一次任务的一次产出 ----
    bundles = {}
    for it in items:
        parent = os.path.dirname(it["path"]).lower()
        key = f"{it['session_id']}|{parent}"
        it["bundle"] = key
        b = bundles.setdefault(key, {"key": key, "session_id": it["session_id"],
                                     "session_title": it["session_title"],
                                     "dir": os.path.dirname(it["path"]),
                                     "project": it["project"], "count": 0})
        b["count"] += 1
    for it in items:
        it["bundle_count"] = bundles[it["bundle"]]["count"]
        it["bundle_label"] = f"{os.path.basename(it['bundle'].split('|')[1]) or '根目录'} 的 {it['bundle_count']} 个产出"

    # ---- 8. 按项目合并成文件夹 ----
    proj = {}
    for it in items:
        k = it["project_key"]
        p = proj.setdefault(k, {
            "key": k, "name": it["project"], "path": it["project_path"],
            "count": 0, "exists": 0, "missing": 0, "size": 0,
            "protected": 0, "types": {}, "recent": 0,
            "sessions": set(), "preview": [],
        })
        p["count"] += 1
        p["exists" if it["exists"] else "missing"] += 1
        p["size"] += it["size"]
        p["protected"] += 1 if it["protected"] else 0
        p["types"][it["kind"]] = p["types"].get(it["kind"], 0) + 1
        p["recent"] = max(p["recent"], it["mtime"])
        p["sessions"].add(it["session_id"])

    # 文件夹封面：优先挑存在的、有代表性的 4 个
    for it in sorted(items, key=lambda x: (not x["exists"], -x["mtime"])):
        p = proj.get(it["project_key"])
        if p and len(p["preview"]) < 4 and it["exists"]:
            p["preview"].append({"id": it["id"], "name": it["name"], "kind": it["kind"]})

    projects = []
    for p in proj.values():
        p["sessions"] = len(p["sessions"])
        projects.append(p)
    projects.sort(key=lambda x: (-x["count"], x["name"]))

    total_size = sum(i["size"] for i in items if i["exists"])
    data = {
        "has_index": True,
        "index_dir": str(idx_dir),
        "projects": projects,
        "items": items,
        "bundles_count": len(bundles),
        "bad_index": bad_idx,
        "trash": art_trash_list(),
        "stats": {
            "total": len(items),
            "exists": sum(1 for i in items if i["exists"]),
            "missing": sum(1 for i in items if not i["exists"]),
            "size": total_size,
            "projects": len(projects),
            "protected": sum(1 for i in items if i["protected"]),
            "bundles": len(bundles),
        },
    }
    _ART_CACHE.update(t=now, data=data)
    return data


def _art_find(aid: str):
    """按 ID 找产物。找不到就返回 None——不抛异常，调用方统一提示。"""
    for it in scan_artifacts()["items"]:
        if it["id"] == aid:
            return it
    return None


def art_open(ids):
    """用系统默认程序打开产物。丢失的文件不打开，直接说明原因。"""
    opened, skipped = [], []
    for aid in (ids or [])[:20]:
        it = _art_find(aid)
        if not it:
            skipped.append({"id": aid, "why": "索引里找不到这个产物"})
            continue
        if not it["exists"]:
            skipped.append({"id": aid, "name": it["name"], "why": "文件已不在原位置（被清理或搬走了）"})
            continue
        try:
            if IS_WIN:
                os.startfile(it["path"])          # noqa: S606 - 打开用户自己的文件
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open",
                                  it["path"]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            opened.append({"id": aid, "name": it["name"]})
        except OSError as e:
            skipped.append({"id": aid, "name": it["name"], "why": f"打不开：{e}"})
    return {"opened": opened, "skipped": skipped}


def art_reveal(aid):
    """在资源管理器里选中这个文件。"""
    it = _art_find(aid)
    if not it:
        return {"error": "索引里找不到这个产物"}
    if not it["exists"]:
        return {"error": "文件已不在原位置，只能定位到不存在的路径"}
    try:
        if IS_WIN:
            subprocess.Popen(["explorer", f"/select,{it['path']}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["open", "-R" if sys.platform == "darwin" else "--reveal",
                              it["path"]], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return {"ok": True, "path": it["path"]}
    except OSError as e:
        return {"error": f"定位失败：{e}"}


def art_trash(ids, force=False):
    """把产物移进回收站（移动，不直接删，可还原）。

    被引用的产物默认拦下来：删掉它，引用它的页面就会破图或打不开。
    用户确认要走 force 才会真移走，并在返回里说清会影响谁。
    """
    tdir = _art_trash_dir()
    try:
        tdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"回收站目录建不出来：{e}"}

    moved, blocked, skipped = [], [], []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    man = _art_trash_manifest()

    for n, aid in enumerate((ids or [])[:200]):
        it = _art_find(aid)
        if not it:
            skipped.append({"id": aid, "why": "索引里找不到这个产物"})
            continue
        if not it["exists"]:
            skipped.append({"id": aid, "name": it["name"], "why": "文件已不在原位置，无从删除"})
            continue
        if it["protected"] and not force:
            blocked.append({"id": aid, "name": it["name"],
                            "why": it["protect_reason"],
                            "ref_by": it["ref_by"][:5]})
            continue
        src = Path(it["path"])
        dest = tdir / f"{stamp}-{aid}-{src.name}"
        try:
            shutil.move(str(src), str(dest))
        except OSError as e:
            skipped.append({"id": aid, "name": it["name"], "why": f"移动失败：{e}"})
            continue
        rec = {"id": aid, "name": it["name"], "orig": str(src),
               "trashed": str(dest), "size": it["size"],
               "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "project": it["project"], "session_id": it["session_id"]}
        try:
            with man.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        moved.append({"id": aid, "name": it["name"],
                      "affects": [x for x in it["ref_by"][:5]]})

    _ART_CACHE["data"] = None            # 状态变了，下次扫描重新读
    if moved:
        log_action("删除产物", len(moved), None,
                   f"移入回收站 {len(moved)} 个" + ("（含被引用项）" if force else ""))
    return {"moved": moved, "blocked": blocked, "skipped": skipped,
            "trash_dir": str(tdir)}


def art_trash_list():
    """回收站里的产物，按移入时间倒序。"""
    man = _art_trash_manifest()
    out = []
    if not man.is_file():
        return out
    try:
        lines = man.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        dest = Path(r.get("trashed") or "")
        r["still_there"] = dest.is_file()
        out.append(r)
    out.sort(key=lambda x: x.get("at") or "", reverse=True)
    return out


def art_trash_restore(ids):
    """从回收站还原回原路径。原位置已被占用时改名，不覆盖。"""
    ids = set(ids or [])
    if not ids:
        return {"error": "没指定要还原的产物"}
    man = _art_trash_manifest()
    if not man.is_file():
        return {"error": "回收站是空的"}
    try:
        lines = [ln for ln in man.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as e:
        return {"error": f"读不了回收站清单：{e}"}

    restored, skipped, keep = [], [], []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("id") not in ids:
            keep.append(ln)
            continue
        src = Path(r.get("trashed") or "")
        dst = Path(r.get("orig") or "")
        if not src.is_file():
            skipped.append({"name": r.get("name"), "why": "回收站里这份文件已经没了"})
            continue
        if dst.exists():
            dst = dst.with_name(f"{dst.stem}_还原{dst.suffix}")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored.append({"name": r.get("name"), "path": str(dst)})
        except OSError as e:
            skipped.append({"name": r.get("name"), "why": f"还原失败：{e}"})
            continue

    try:
        man.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except OSError:
        pass
    _ART_CACHE["data"] = None
    if restored:
        log_action("还原产物", len(restored), None, f"从回收站还原 {len(restored)} 个")
    return {"restored": restored, "skipped": skipped}


def art_trash_purge(ids=None):
    """彻底删掉回收站里的文件。不传 ids 就清空整个回收站。"""
    man = _art_trash_manifest()
    if not man.is_file():
        return {"purged": 0, "freed": 0}
    want = set(ids or [])
    try:
        lines = [ln for ln in man.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as e:
        return {"error": f"读不了回收站清单：{e}"}

    purged, freed, keep = 0, 0, []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if want and r.get("id") not in want:
            keep.append(ln)
            continue
        p = Path(r.get("trashed") or "")
        try:
            if p.is_file():
                sz = p.stat().st_size
                p.unlink()
                purged += 1
                freed += sz
        except OSError:
            keep.append(ln)
            continue
    try:
        man.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except OSError:
        pass
    if purged:
        log_action("彻底删除产物", purged, None, f"从回收站清除 {purged} 个，释放 {freed // 1024} KB")
    return {"purged": purged, "freed": freed}


def scan_trash(limit=400):
    """列出被删除的会话，并判断还能不能救。

    WorkBuddy 删会话只把 deleted_at 填上，正文文件不一定当场清掉，
    所以删完一段时间内是能捞回来的。能不能救只看一件事：
    projects 下还有没有对应的 .jsonl。

    正文不在预期目录的，按会话 id 全盘找一次——工作目录改过、搬过盘的
    情况很常见，文件常常躺在旧目录里。
    """
    con = connect()
    rows = [dict(r) for r in con.execute("""
        SELECT id, cwd, COALESCE(custom_title, title) t,
               deleted_at, created_at, updated_at, status
        FROM sessions WHERE deleted_at IS NOT NULL
        ORDER BY deleted_at DESC LIMIT ?""", (limit,)).fetchall()]
    con.close()

    proj = REAL_DB.parent / "projects"
    items = []
    for r in rows:
        cwd = r["cwd"] or ""
        want = find_esc_dir(cwd) / f"{r['id']}.jsonl"
        src = want if want.exists() else None
        moved_from = ""
        if src is None:
            hits = list(proj.glob(f"*/{r['id']}.jsonl"))
            if hits:
                src = hits[0]
                moved_from = src.parent.name
        size = src.stat().st_size if src else 0
        items.append({
            "id": r["id"],
            "title": r["t"] or "（没有标题）",
            "cwd": cwd,
            "dir": rules.tail_dir(cwd) if cwd else "（无目录）",
            "deleted_at": r["deleted_at"],
            "deleted_h": _ms_to_str(r["deleted_at"]),
            "created_h": _ms_to_str(r["created_at"]),
            "status": r["status"],
            "file_ok": bool(src),
            "size": size,
            "size_h": human_size(size) if src else "—",
            "moved_from": moved_from,
            "line_count": None,
        })
    ok = sum(1 for x in items if x["file_ok"])
    return {"items": items, "count": len(items), "recoverable": ok,
            "lost": len(items) - ok}


def inspect_trash(sid):
    """看一条被删会话的具体内容，帮助判断值不值得救。

    只读文件前若干行，不解析整份——有些会话几 MB，全读会卡。
    """
    con = connect()
    r = con.execute("""SELECT id, cwd, COALESCE(custom_title, title) t,
                              deleted_at, created_at, status
                       FROM sessions WHERE id LIKE ? AND deleted_at IS NOT NULL""",
                    (sid + "%",)).fetchone()
    con.close()
    if r is None:
        return {"error": "找不到这条待恢复的会话"}
    sid = r["id"]

    proj = REAL_DB.parent / "projects"
    f = find_esc_dir(r["cwd"] or "") / f"{sid}.jsonl"
    if not f.exists():
        hits = list(proj.glob(f"*/{sid}.jsonl"))
        f = hits[0] if hits else None
    if f is None:
        return {"error": "正文文件已经没了，这条救不回来", "id": sid, "title": r["t"]}

    lines = 0
    first_user = ""
    first_ai = ""
    last_ai = ""
    n_user = 0
    n_ai = 0
    n_tool = 0
    try:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lines += 1
                if len(line) > 400000:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                typ = d.get("type")
                if typ == "function_call":
                    n_tool += 1
                role = d.get("role")
                if role not in ("user", "assistant"):
                    continue
                texts = []
                for c in d.get("content") or []:
                    if isinstance(c, dict) and c.get("type") in ("input_text", "output_text"):
                        t = (c.get("text") or "").strip()
                        if t:
                            texts.append(t)
                if not texts:
                    continue
                joined = "\n".join(texts).strip()
                if role == "user":
                    n_user += 1
                    # 系统注入的块（<system-reminder>）、续接语都不是用户问的话
                    skip = ("<", "Please continue", "This session is being continued",
                            "Continue with", "Continue from")
                    if not first_user and not joined.startswith(skip):
                        first_user = joined[:300]
                else:
                    n_ai += 1
                    if not first_ai:
                        first_ai = joined[:260]
                    last_ai = joined[:200]
    except OSError:
        pass

    return {
        "id": sid,
        "title": r["t"] or "",
        "cwd": r["cwd"] or "",
        "dir": rules.tail_dir(r["cwd"]),
        "deleted_h": _ms_to_str(r["deleted_at"]),
        "created_h": _ms_to_str(r["created_at"]),
        "size_h": human_size(f.stat().st_size),
        "lines": lines,
        "file": str(f),
        "first_user": first_user or "（这次没有你的提问，是自动任务或续接出来的会话）",
        "first_ai": first_ai or "（没有可读的回复）",
        "last_ai": last_ai,
        "n_user": n_user,
        "n_ai": n_ai,
        "n_tool": n_tool,
        "auto": n_user <= 1,
    }


def restore_trash(ids, target_cwd=None):
    """把删掉的会话救回来。

    只做两件事：把 deleted_at 清空、把正文放回该在的位置。
    标题、正文内容、状态一概不动。

    正文躺在别的目录的（改过工作目录、搬过盘），会先搬回来再恢复，
    不搬的话恢复出来是空会话。
    """
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {"error": "没有选中要恢复的会话"}

    snap = snapshot("before-restore-trash")
    con = connect()
    info = {}
    for i in ids:
        r = con.execute("""SELECT id, cwd, COALESCE(custom_title, title) t
                           FROM sessions WHERE id=? AND deleted_at IS NOT NULL""",
                        (i,)).fetchone()
        if r is not None:
            info[i] = dict(r)
    con.close()

    if not info:
        return {"error": "这些会话已经不在待恢复列表里了（可能已经恢复过）",
                "changed": 0}

    proj = REAL_DB.parent / "projects"
    restored, blocked, moved = [], [], 0
    con = connect(write=True)
    try:
        with con:
            for sid, r in info.items():
                want = rules.norm_path((target_cwd or r["cwd"] or "").strip())
                if not want:
                    blocked.append({"id": sid, "title": r["t"],
                                    "why": "这条没记过目录，先给个目标目录再恢复"})
                    continue

                dst_dir = find_esc_dir(want)
                dst = dst_dir / f"{sid}.jsonl"

                src = dst if dst.exists() else None
                if src is None:
                    hits = list(proj.glob(f"*/{sid}.jsonl"))
                    src = hits[0] if hits else None
                if src is None:
                    blocked.append({"id": sid, "title": r["t"],
                                    "why": "正文文件已经没了，恢复出来是空会话"})
                    continue

                if src != dst:
                    if dst.exists():
                        blocked.append({"id": sid, "title": r["t"],
                                        "why": "目标位置已有同名文件，不覆盖"})
                        continue
                    try:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                        # 工具输出目录跟着走，否则历史里的附件打不开
                        sub = src.parent / sid
                        if sub.is_dir():
                            tsub = dst_dir / sid
                            if not tsub.exists():
                                shutil.move(str(sub), str(tsub))
                        moved += 1
                    except OSError as e:
                        blocked.append({"id": sid, "title": r["t"],
                                        "why": f"搬正文失败：{e}"})
                        continue

                con.execute("UPDATE sessions SET deleted_at=NULL, cwd=? WHERE id=?",
                            (want, sid))
                restored.append(sid)
    finally:
        con.close()

    log_action("恢复已删除会话", len(restored), snap,
               f"{len(blocked)} 条救不回" if blocked else "")
    return {"changed": len(restored), "restored": restored,
            "blocked": blocked, "moved_files": moved, "snapshot": snap}


def list_archive():
    """列出当前归档的会话。

    last_activity_at 是新版 WorkBuddy 才有的列，旧库没有——
    先用 PRAGMA 探测列在不在，不在就降级只按 updated_at 排，别直接崩。
    """
    con = connect()
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    order_col = "last_activity_at" if "last_activity_at" in cols else None
    sel = f", last_activity_at" if order_col else ""
    order = (f"ORDER BY COALESCE(last_activity_at, updated_at) DESC" if order_col
             else "ORDER BY updated_at DESC")
    rows = [dict(r) for r in con.execute(
        f"SELECT id, COALESCE(custom_title, title) t, cwd, updated_at{sel} "
        f"FROM sessions WHERE deleted_at IS NULL AND LOWER(status)='archived' "
        f"{order}").fetchall()]
    con.close()
    orig = _load_orig()
    for r in rows:
        r["orig_status"] = orig.get(r["id"], "completed")
    return {"items": rows, "count": len(rows)}


def archive_sessions(ids, archive=True):
    """归档 / 取消归档。

    ⛔ 只用 status 字段，绝不用 deleted_at：
       WorkBuddy 的保留策略会把 deleted_at 非空的会话当删除候选真删掉，
       而 status='archived' 的会被明确跳过（archivedSkipped）——归档反而受保护。

    改完要重启 WorkBuddy 才看得到效果：它不监听外部改库。
    """
    if not ids:
        return {"error": "没有选中会话", "changed": 0}

    snap = snapshot("before-archive" if archive else "before-unarchive")
    orig = _load_orig()
    con = connect(write=True)
    changed = 0
    blocked = []
    try:
        with con:
            for sid in ids:
                row = con.execute(
                    "SELECT status, deleted_at FROM sessions WHERE id=?", (sid,)).fetchone()
                if row is None:
                    blocked.append({"id": sid, "why": "数据库里没有这条会话"})
                    continue
                if row["deleted_at"] is not None:
                    blocked.append({"id": sid, "why": "这条已在回收站，先恢复再归档"})
                    continue
                cur = (row["status"] or "").lower()

                if archive:
                    if cur == "archived":
                        continue
                    orig[sid] = row["status"] or "completed"
                    con.execute("UPDATE sessions SET status='archived', updated_at=? "
                                "WHERE id=?", (_now_ms(), sid))
                else:
                    if cur != "archived":
                        continue
                    back = orig.pop(sid, None) or "completed"
                    con.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                                (back, _now_ms(), sid))
                changed += 1
    finally:
        con.close()

    _save_orig(orig)
    log_action("归档" if archive else "取消归档", changed, snap,
               f"{len(blocked)} 条被跳过" if blocked else "")
    return {"changed": changed, "snapshot": snap, "blocked": blocked,
            "need_restart": changed > 0}


def organize_all(dry_run=False):
    """一键整理：把「规则有把握」的建议标题一次性应用。

    只动有把握的，三类跳过：
    - 需确认（规则没认出的）——留给你人工或 AI 起名
    - 正在使用的——文件 3 分钟内被写过
    - 已归档的——归档的东西不折腾
    apply_titles 内部会先自动快照，改坏可撤。
    dry_run=True 时只返回「将要改什么」，不写任何东西——给检查窗口用。
    """
    items = load_sessions()
    # 定时任务明确排除，不靠 changed 间接挡：它在 WorkBuddy 那边有独立分类，
    # 标题也由那边维护。工具一旦碰它，就会变成两边各改一套。
    picked = [
        x for x in items
        if x["changed"] and not x["conflict"] and not x["active"]
        and not x["archived"] and not x["auto"]
    ]
    if dry_run:
        return {
            "dry_run": True,
            "pairs": [{"id": x["id"], "current": x["current"],
                       "suggest": x["suggest"], "type": x["type"]} for x in picked],
            "skipped_confirm": sum(1 for x in items if x["conflict"] and not x["archived"]),
            "skipped_active": sum(1 for x in items if x["active"]),
            "skipped_archived": sum(1 for x in items if x["archived"]),
            "skipped_auto": sum(1 for x in items if x["auto"] and not x["archived"]),
        }
    pairs = [{"id": x["id"], "title": x["suggest"]} for x in picked]
    skipped_confirm = sum(1 for x in items if x["conflict"] and not x["archived"])
    skipped_active = sum(1 for x in items if x["active"])
    skipped_auto = sum(1 for x in items if x["auto"] and not x["archived"])
    if not pairs:
        return {"changed": 0, "snapshot": None, "pairs": [],
                "skipped_confirm": skipped_confirm, "skipped_active": skipped_active,
                "skipped_auto": skipped_auto}
    r = apply_titles(pairs)
    return {"changed": r.get("changed", 0), "snapshot": r.get("snapshot"),
            "skipped_confirm": skipped_confirm, "skipped_active": skipped_active,
            "skipped_auto": skipped_auto, "need_restart": True}


def backups_cleanup(keep=20):
    """一键把快照清到最近 keep 份。逐份删，失败的收集原因不中断——
    Windows 下文件被占用或权限不足都可能删不掉，如实报告给用户。"""
    rows = list_backups()          # 已按时间倒序
    try:
        keep = int(keep or 20)
    except (TypeError, ValueError):
        keep = 20
    keep = max(1, min(200, keep))
    victims = rows[keep:]
    deleted, failed = [], []
    for v in victims:
        try:
            (BACKUPS / v["name"]).unlink()
            deleted.append(v["name"])
        except OSError as e:
            failed.append({"name": v["name"], "why": f"{type(e).__name__}: {e}"})
    if deleted:
        log_action("清理旧快照", len(deleted), None,
                   f"保留最近 {keep} 份" + (f"，{len(failed)} 份没删掉" if failed else ""))
    return {"deleted": len(deleted), "failed": failed, "left": len(list_backups())}


def rename_workspace(path, new_name):
    """给闲置的工作空间文件夹改个看得懂的名字（纯 mv，内容一个字不动）。

    只允许改「默认工作空间里、没有存活会话在用」的文件夹，
    新名字不能和已有目录重名。改名不影响 WorkBuddy 数据——
    闲置空间没有会话还指着原路径。
    """
    new_name = (new_name or "").strip()
    if not new_name or any(c in new_name for c in '\\/:*?"<>|'):
        return {"error": "新名字不能为空，也不能带 \\ / : * ? \" < > | 这些字符"}
    try:
        d = Path(path).resolve()
        root = _ws_root().resolve()
        d.relative_to(root)
    except (OSError, ValueError):
        return {"error": "只能改默认工作空间里的文件夹"}
    if not d.is_dir() or d == root:
        return {"error": "文件夹不存在或是根目录"}
    pat_variants = {str(d), d.as_posix()}
    con = connect()
    n = 0
    for pat in pat_variants:
        n += con.execute("SELECT COUNT(*) c FROM sessions "
                         "WHERE deleted_at IS NULL AND cwd LIKE ?",
                         (pat + "%",)).fetchone()["c"]
    con.close()
    if n:
        return {"error": f"还有 {n} 条会话放在这个目录里，先移走再改名"}
    target = d.parent / new_name
    if target.exists():
        return {"error": "同名文件夹已经存在"}
    try:
        d.rename(target)
    except OSError as e:
        return {"error": f"改名失败：{e}"}
    log_action("空间改名", 1, None, f"{d.name} → {new_name}")
    return {"ok": True, "new_path": str(target)}


def purge_deleted_files(ids=None):
    """清理「已删除会话」在磁盘上的残留文件（正文 + 同名附件目录）。

    ⛔ 只删数据库里 deleted_at 非空的会话的文件。活跃会话碰都不碰。
    这些文件留着没有任何作用——会话已经被删，WorkBuddy 不会再读它们。
    删之前 UI 会弹确认框，这里再校验一遍删除标记，双保险。
    """
    con = connect()
    rows = [dict(r) for r in con.execute(
        "SELECT id, cwd FROM sessions WHERE deleted_at IS NOT NULL").fetchall()]
    con.close()
    want = {r["id"] for r in rows}
    targets = [r for r in rows if ids is None or r["id"] in set(ids)]

    removed = 0
    freed = 0
    skipped = []
    for r in targets:
        f = session_file(r["id"], r["cwd"])
        if not f.exists():
            continue
        try:
            size = f.stat().st_size
            f.unlink()
            removed += 1
            freed += size
            # 同名附件目录（存工具输出等），有就一起删
            att = f.parent / f.stem
            if att.is_dir():
                import shutil as _sh
                for g in att.rglob("*"):
                    if g.is_file():
                        freed += g.stat().st_size
                _sh.rmtree(att, ignore_errors=True)
        except OSError as e:
            skipped.append({"id": r["id"], "why": str(e)})
    if removed:
        log_action("清理已删会话残留", removed, None, f"释放 {human_size(freed)}")
    return {"removed": removed, "freed": human_size(freed),
            "left": len(want) - removed, "skipped": skipped}


_FIT_CACHE = {}


def rule_fit():
    """拿磁盘上真实存在的目录，反查「路径→目录名」规则还准不准。

    这个规则是从实测反推出来的（C:\\a\\b → C-a-b），不是官方文档。
    别的用户环境、别的 WorkBuddy 版本都可能不一样。若规则不对还硬搬，
    会话会在界面上打不开——所以这里先自检，对不上就停用搬家功能。

    返回 {checked, matched, samples}
    """
    if _FIT_CACHE:
        return _FIT_CACHE
    proj = REAL_DB.parent / "projects"
    if not proj.is_dir():
        _FIT_CACHE.update({"checked": 0, "matched": 0, "samples": []})
        return _FIT_CACHE
    try:
        con = connect()
        rows = con.execute(
            "SELECT id, cwd FROM sessions WHERE deleted_at IS NULL LIMIT 60").fetchall()
        con.close()
    except sqlite3.Error:
        _FIT_CACHE.update({"checked": 0, "matched": 0, "samples": []})
        return _FIT_CACHE

    checked = matched = 0
    samples = []
    for r in rows:
        cwd = r["cwd"] or ""
        if not cwd:
            continue
        want = esc_dir_name(cwd)
        # 先看「按规则算出来的位置」有没有这份文件。有就算规则对——
        # 同一个会话可能在别处还留着历史副本，不能拿副本反推规则。
        if (proj / want / f"{r['id']}.jsonl").exists():
            checked += 1
            matched += 1
            continue
        hits = list(proj.glob(f"*/{r['id']}.jsonl"))
        if not hits:
            continue          # 文件都不在，无从判断，不计入样本
        checked += 1
        if len(samples) < 3:
            samples.append({"cwd": cwd, "want": want, "real": hits[0].parent.name})
    _FIT_CACHE.update({"checked": checked, "matched": matched, "samples": samples})
    return _FIT_CACHE


def rule_trusted():
    """规则是否可信：样本够、且八成以上对得上。样本太少时按可信处理（不拦新手）。"""
    fit = rule_fit()
    if fit["checked"] < 5:
        return True, ""
    if fit["matched"] >= fit["checked"] * 0.8:
        return True, ""
    s = fit["samples"][0] if fit["samples"] else {}
    return False, (f"目录命名规则和这台电脑的实际情况对不上"
                   f"（{fit['matched']}/{fit['checked']} 条相符"
                   + (f"，例如 {s.get('cwd','')} 实际是 {s.get('real','')}" if s else "")
                   + "）。搬错会让会话打不开，所以搬家功能已停用；改名、归档不受影响。")


def bad_target(new_cwd: str) -> str:
    """目标路径的合法性检查。返回非空表示不能搬，内容是给用户看的原因。"""
    if not new_cwd:
        return "没填目标路径"
    if new_cwd.startswith("\\\\"):
        return "网络路径（\\\\服务器\\共享）的目录转义规则没有验证过，容易搬坏，请在 WorkBuddy 里操作"
    if not re.match(r"^[A-Za-z]:\\", new_cwd):
        return "要写完整路径，例如 D:\\项目\\新空间"
    if len(new_cwd) <= 3:          # 只有一个盘符根，如 "D:\"
        return "不能直接搬到盘符根目录"
    if len(new_cwd) > 180:         # 加上 projects/<转义名>/<id>.jsonl 会顶到系统 260 上限
        return "路径太长，搬过去可能打不开，换个浅一点的位置"
    return ""


def preview_move(pairs):
    """只算不执行：把每条会话会怎么搬、会不会被拦，全列出来给用户确认。

    搬目录是这个工具里唯一会动磁盘文件的动作，所以先出对照表，
    用户看清「哪条、从哪到哪、文件多大」之后再决定，不搞一键闷头执行。
    """
    if not IS_WIN:
        return {"items": [], "can_move": 0, "blocked": len(pairs),
                "wb_running": wb_running(),
                "error": "移到空间只在 Windows 上验证过。当前系统的目录转义规则没把握，"
                         "算错会让会话打不开，所以这边不提供。改标题和归档不受影响。"}

    trust, why_not = rule_trusted()
    if not trust:
        return {"items": [], "can_move": 0, "blocked": len(pairs),
                "wb_running": wb_running(), "error": why_not, "rule_unfit": True}

    con = connect()
    out = []
    for p in pairs:
        sid = p.get("id")
        new_cwd = rules.norm_path((p.get("cwd") or "").strip())
        row = con.execute(
            "SELECT cwd, COALESCE(custom_title, title) t FROM sessions WHERE id=?",
            (sid,)).fetchone()
        if row is None:
            out.append({"id": sid, "title": "（会话不存在）", "status": "跳过",
                        "why": "数据库里找不到这条会话"})
            continue
        old_cwd = row["cwd"] or ""
        src = find_esc_dir(old_cwd) / f"{sid}.jsonl"
        dst_dir = find_esc_dir(new_cwd)
        dst = dst_dir / f"{sid}.jsonl"

        status, why = "可搬", ""
        if rules.norm_path(old_cwd) == new_cwd:
            status, why = "跳过", "目录没变"
        elif bad_target(new_cwd):
            status, why = "跳过", bad_target(new_cwd)
        elif len(str(dst)) >= 250:
            status, why = "跳过", "完整路径太长（Windows 有 260 字符上限），换个浅一点的位置"
        elif is_active(src):
            status, why = "跳过", "正在使用中，等它闲下来"
        elif not src.exists():
            status, why = "跳过", "找不到正文文件，搬了会丢内容"
        elif dst.exists():
            status, why = "跳过", "目标已有同名文件，不覆盖"

        size = src.stat().st_size if src.exists() else 0
        out.append({
            "id": sid, "title": row["t"] or "",
            "from": old_cwd, "to": new_cwd,
            "from_dir": src.parent.name, "to_dir": dst_dir.name,
            "file": str(src), "size": size,
            "new_dir": not dst_dir.is_dir(),   # 目标目录还不存在，搬的时候会新建
            # 真实文件夹（不是 WorkBuddy 的存储目录）是否已存在：不存在说明是「新建一个空间」
            "create_real_dir": bool(new_cwd) and not Path(new_cwd).is_dir(),
            # 目标目录在侧栏里有没有现成的空间。没有的话搬过去会顺手建一个
            "space_exists": bool(space_for_cwd(con, new_cwd, create=False)),
            "size_h": f"{size/1024/1024:.1f} MB" if size > 1024*1024 else f"{size/1024:.0f} KB",
            "status": status, "why": why,
        })
    con.close()

    ok = sum(1 for x in out if x["status"] == "可搬")
    return {"items": out, "can_move": ok, "blocked": len(out) - ok,
            "wb_running": wb_running()}


def scan_path_styles():
    """找出同一个物理目录被写成几种样子。

    WorkBuddy 侧栏是按 cwd 字符串分组的，F:\\AI\\x 和 F:/AI/x 会被当成两个目录，
    侧栏就裂成两组。合并它们只需要把字符串统一，不用搬任何文件——
    因为两种写法转义出的目录名是一样的（先 norm 再转义）。
    """
    items = load_sessions()
    groups = {}
    for i in items:
        groups.setdefault(i["cwd_norm"].lower(), []).append(i)

    out = []
    for key, arr in groups.items():
        styles = {i["cwd"] for i in arr}
        if len(styles) > 1:
            out.append({
                "cwd_norm": key,
                "styles": sorted(styles),
                "count": len(arr),
                "target": arr[0]["cwd_norm"],
            })
    return {"groups": out, "total_groups": len(out),
            "total_sessions": sum(g["count"] for g in out)}


def unify_paths(keys=None):
    """把 cwd 统一成规范写法。只改数据库字符串，不动文件。

    安全前提：esc(任意写法) 结果相同，所以改完 WorkBuddy 找正文还去同一个目录。
    这个前提只在 Windows 上验证过，别的系统不提供。
    """
    if not IS_WIN:
        return {"changed": 0, "groups": 0,
                "error": "路径写法统一只在 Windows 上验证过，当前系统不提供。"}
    scan = scan_path_styles()
    want = {k.lower() for k in keys} if keys else None
    todo = [g for g in scan["groups"] if not want or g["cwd_norm"].lower() in want]
    if not todo:
        return {"changed": 0, "groups": 0}

    targets = {g["cwd_norm"].lower(): g["target"] for g in todo}

    snap = snapshot("before-unify")
    con = connect(write=True)
    changed = 0
    detail = []
    try:
        with con:
            rows = con.execute(
                "SELECT id, cwd FROM sessions WHERE deleted_at IS NULL").fetchall()
            per_group = {}
            for r in rows:
                cur = r["cwd"] or ""
                key = rules.norm_path(cur).lower()
                if key not in targets:
                    continue
                per_group.setdefault(key, 0)
                per_group[key] += 1
                if cur == targets[key]:
                    continue
                con.execute("UPDATE sessions SET cwd=? WHERE id=?",
                            (targets[key], r["id"]))
                changed += 1
            for g in todo:
                detail.append({"cwd": g["target"], "styles": g["styles"],
                               "sessions": per_group.get(g["cwd_norm"].lower(), 0)})
    finally:
        con.close()
    log_action("统一路径写法", changed, snap, f"合并 {len(todo)} 组")
    return {"changed": changed, "groups": len(todo), "snapshot": snap, "detail": detail}


def move_sessions(pairs):
    """pairs: [{id, cwd}]。改会话所在目录，并同步搬迁会话正文文件。

    关键：WorkBuddy 按 projects/<转义cwd>/<会话id>.jsonl 找正文，
    只改数据库里的 cwd 而不搬文件，会话在界面上就打不开了。所以两件事必须一起做。

    三道保护：
    1. 正在使用的会话（文件 3 分钟内被写过）一律不动，防止搬走对方正在写的文件。
    2. 目标位置已有同名文件则跳过，绝不覆盖。
    3. 源文件不在则跳过，不凭空改数据库。
    """
    if not pairs:
        return {"changed": 0, "snapshot": None, "blocked": []}

    if not IS_WIN:
        return {"error": "移到空间只在 Windows 上验证过，当前系统不支持。"
                         "改标题和归档不受影响。", "changed": 0}

    trust, why_not = rule_trusted()
    if not trust:
        return {"error": why_not, "changed": 0, "rule_unfit": True}

    running = wb_running()
    snap = snapshot("before-move")
    con = connect(write=True)
    changed = 0
    moved = []
    blocked = []
    try:
        with con:
            for p in pairs:
                sid = p.get("id")
                new_cwd = rules.norm_path((p.get("cwd") or "").strip())
                if not sid or not new_cwd:
                    continue
                row = con.execute("SELECT cwd FROM sessions WHERE id=?", (sid,)).fetchone()
                if row is None:
                    continue
                old_cwd = row["cwd"] or ""
                if rules.norm_path(old_cwd) == new_cwd:
                    continue

                bad = bad_target(new_cwd)
                if bad:
                    blocked.append({"id": sid, "why": bad})
                    continue

                src = find_esc_dir(old_cwd) / f"{sid}.jsonl"
                dst_dir = find_esc_dir(new_cwd)
                dst = dst_dir / f"{sid}.jsonl"

                if is_active(src):
                    blocked.append({"id": sid, "why": "这条会话正在使用中，等它闲下来再搬"})
                    continue
                if not src.exists():
                    blocked.append({"id": sid, "why": "找不到会话正文文件，跳过以免丢内容"})
                    continue
                if dst.exists():
                    blocked.append({"id": sid, "why": "目标位置已有同名文件，跳过不覆盖"})
                    continue

                size_before = src.stat().st_size
                try:
                    # 目标空间对应的真实文件夹也建出来：只改数据库会出现
                    # 「侧栏有分组、磁盘上没有这个目录」的错位，打开会话时路径是空的
                    Path(new_cwd).mkdir(parents=True, exist_ok=True)
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                except OSError as e:
                    blocked.append({"id": sid, "why": f"搬文件失败：{e}"})
                    continue

                if not dst.exists() or dst.stat().st_size != size_before:
                    blocked.append({"id": sid, "why": "搬后校验不一致，已跳过数据库改动"})
                    continue

                # 空间一起换。新版侧栏按 space_id 分组，只改 cwd 会让会话
                # 继续挂在旧空间下，而文件已经搬到新目录了。
                # 老库没有空间表时返回 None，就还是只改 cwd。
                new_space = space_for_cwd(con, new_cwd)
                if new_space:
                    con.execute("UPDATE sessions SET cwd=?, space_id=? WHERE id=?",
                                (new_cwd, new_space, sid))
                else:
                    con.execute("UPDATE sessions SET cwd=? WHERE id=?", (new_cwd, sid))
                changed += 1
                moved.append({"id": sid, "from": src.parent.name, "to": dst_dir.name})
    finally:
        con.close()

    # 源目录空了就一并清掉，免得侧栏留着空分组
    cleaned = []
    for name in {m["from"] for m in moved}:
        d = REAL_DB.parent / "projects" / name
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                cleaned.append(name)
        except OSError:
            pass

    log_action("移到空间", changed, snap,
               f"{len(blocked)} 条被跳过" if blocked else "")
    return {"changed": changed, "snapshot": snap, "blocked": blocked,
            "moved": moved, "cleaned_dirs": cleaned,
            "wb_running": running}


def restore(name):
    """把数据库还原到某份快照的状态。

    关键：目录也要一起还原。WorkBuddy 是按 projects/<转义cwd>/<id>.jsonl
    找正文的，只把 cwd 改回旧路径、文件却还留在新目录，会话就会打不开——
    这是最容易被当成「数据丢了」的故障，所以这里先搬文件、再改库。
    """
    p = BACKUPS / name
    if not p.exists():
        return {"error": "快照不存在"}
    d = json.loads(p.read_text(encoding="utf-8"))
    snap = snapshot("before-restore")

    # 第一步：算出哪些会话的目录和快照里不一致，先把正文搬回去
    con = connect()
    now = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, cwd FROM sessions").fetchall()}
    con.close()

    back = []
    for r in d.get("rows") or []:
        cur = now.get(r["id"])
        if not cur:
            continue
        if rules.norm_path(cur["cwd"] or "") != rules.norm_path(r.get("cwd") or ""):
            back.append({"id": r["id"], "cwd": r.get("cwd") or ""})

    moved, blocked, moved_ids = 0, [], set()
    if back:
        if not IS_WIN:
            return {"error": "还原会连目录一起搬，这个功能只在 Windows 上可用。"
                             "标题和状态可以单独改。"}
        mv = move_sessions(back)
        if mv.get("error"):
            return {"error": mv["error"]}
        moved = mv.get("changed", 0)
        blocked = mv.get("blocked") or []
        moved_ids = {m["id"] for m in (mv.get("moved") or [])}

    # 搬不动的会话：保留当前目录，只还原标题和状态，绝不让 cwd 指向空目录
    keep_cwd = {b["id"] for b in blocked}

    con = connect(write=True)
    changed, touched = 0, set()
    try:
        with con:
            for r in d["rows"]:
                cur = con.execute(
                    "SELECT custom_title, cwd, status FROM sessions WHERE id=?",
                    (r["id"],)).fetchone()
                if cur is None:
                    continue
                want_cwd = cur["cwd"] if r["id"] in keep_cwd else (r["cwd"] or "")
                title_diff = (cur["custom_title"] or "") != (r["custom_title"] or "")
                cwd_diff = (cur["cwd"] or "") != want_cwd
                # 老快照没有 status 字段，这时只还原标题和目录，不动状态
                want_status = r.get("status")
                status_diff = (want_status is not None
                               and (cur["status"] or "") != want_status)
                if not (title_diff or cwd_diff or status_diff):
                    continue
                # 目录变了就连空间一起换（新版侧栏按 space_id 分组）。
                # 目录没变时不动空间——用户可能在 WorkBuddy 里手动挪过空间，
                # 那属于人家自己的选择，还原标题不该顺手改回去。
                new_space = space_for_cwd(con, want_cwd) if cwd_diff else None
                if new_space and status_diff:
                    con.execute(
                        "UPDATE sessions SET custom_title=?, cwd=?, status=?,"
                        " space_id=? WHERE id=?",
                        (r["custom_title"], want_cwd, want_status, new_space, r["id"]))
                elif new_space:
                    con.execute(
                        "UPDATE sessions SET custom_title=?, cwd=?, space_id=?"
                        " WHERE id=?",
                        (r["custom_title"], want_cwd, new_space, r["id"]))
                elif status_diff:
                    con.execute(
                        "UPDATE sessions SET custom_title=?, cwd=?, status=? WHERE id=?",
                        (r["custom_title"], want_cwd, want_status, r["id"]))
                else:
                    con.execute(
                        "UPDATE sessions SET custom_title=?, cwd=? WHERE id=?",
                        (r["custom_title"], want_cwd, r["id"]))
                changed += 1
                touched.add(r["id"])
    finally:
        con.close()
    log_action("还原快照", changed, snap, f"从 {name} 还原")
    return {"changed": len(touched | moved_ids), "snapshot": snap,
            "restored_from": name,
            "moved_files": moved, "blocked": blocked, "need_restart": True}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            touch()   # 有请求就说明界面还活着
            if path in ("/", "/index.html"):
                self._send(200, UI.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif path == "/api/ping":
                ping()
                self._send(200, {"ok": True})
            elif path == "/api/state":
                items = load_sessions()
                self._send(200, {"stats": stats(items), "items": items,
                                 "db": str(REAL_DB), "db_exists": REAL_DB.exists(),
                                 "platform": "windows" if IS_WIN else sys.platform,
                                 "platform_ok": IS_WIN,
                                 "backups": list_backups()})
            elif path == "/api/backups":
                self._send(200, {"backups": list_backups()})
            elif path == "/api/naming/export":
                self._send(200, export_naming())
            elif path == "/api/paths":
                self._send(200, scan_path_styles())
            elif path == "/api/auto/check":
                # 定时任务标题被叠重复的清单，只读，给界面显示用
                self._send(200, fix_auto_titles(dry_run=True))
            elif path == "/api/storage":
                self._send(200, storage_info())
            elif path == "/api/archive/list":
                self._send(200, list_archive())
            elif path == "/api/history":
                self._send(200, list_history())
            elif path == "/api/trash":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, scan_trash())
            elif path == "/api/trash/inspect":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, inspect_trash((qs.get("id") or [""])[0]))
            elif path == "/api/workspaces":
                self._send(200, scan_workspaces())
            elif path == "/api/rehome/plan":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, plan_rehome((qs.get("root") or [""])[0] or None))
            elif path == "/api/disks":
                self._send(200, scan_disks())
            elif path == "/api/artifacts":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, scan_artifacts(force="1" in (qs.get("force") or [])))
            elif path == "/api/artifacts/trash":
                self._send(200, {"trash": art_trash_list(),
                                 "dir": str(_art_trash_dir())})
            elif path == "/api/types":
                self._send(200, {"types": [
                    {"emoji": e, "name": n, "keys": k} for e, n, k in rules.TYPE_RULES
                ]})
            elif path == "/api/config":
                self._send(200, public_config())
            else:
                self._send(404, {"error": "not found"})
        except sqlite3.OperationalError as e:
            self._send(503, {"error": _db_busy_msg(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "请求格式不对：需要一个 JSON 对象"})
            return
        try:
            if path == "/api/bye":
                # 页面关闭时 sendBeacon 打过来，几秒后服务自己退
                say_bye()
                self._send(200, {"ok": True})
                return
            touch()
            if path == "/api/apply":
                self._send(200, apply_titles(body.get("pairs") or []))
            elif path == "/api/move":
                self._send(200, move_sessions(body.get("pairs") or []))
            elif path == "/api/move/preview":
                self._send(200, preview_move(body.get("pairs") or []))
            elif path == "/api/backup":
                self._send(200, {"snapshot": snapshot("manual")})
            elif path == "/api/auto/fix":
                self._send(200, fix_auto_titles(dry_run=False))
            elif path == "/api/backups/delete":
                self._send(200, delete_backup(body.get("name") or ""))
            elif path == "/api/backups/cleanup":
                self._send(200, backups_cleanup(body.get("keep") or 20))
            elif path == "/api/restore":
                self._send(200, restore(body.get("name") or ""))
            elif path == "/api/history/undo":
                # 撤回 = 先把目录/文件反向搬回去，再套快照里的标题与状态。
                # restore 内部会再存一份 before-restore 快照，撤错了还能再撤回来。
                snap = body.get("snapshot") or ""
                r = undo_snapshot(snap)
                if not r.get("error"):
                    log_action("撤回", r.get("changed", 0), r.get("snapshot"),
                               f"撤回 {snap}")
                self._send(200, r)
            elif path == "/api/trash/restore":
                self._send(200, restore_trash(body.get("ids") or [],
                                              body.get("target_cwd")))
            elif path == "/api/organize_all":
                self._send(200, organize_all(dry_run=bool(body.get("dry_run"))))
            elif path == "/api/trash/purge":
                self._send(200, purge_deleted_files(body.get("ids")))
            elif path == "/api/workspaces/clean":
                r = clean_workspaces(body.get("paths") or [])
                r["wb_running"] = wb_running()
                self._send(200, r)
            elif path == "/api/workspaces/rename":
                self._send(200, rename_workspace(body.get("path") or "",
                                                 body.get("new_name") or ""))
            elif path == "/api/workspaces/memory":
                self._send(200, read_workspace_memory(body.get("path") or ""))
            elif path == "/api/naming/import":
                self._send(200, import_naming(body))
            elif path == "/api/naming/clear":
                (NAMING / "latest.json").unlink(missing_ok=True)
                self._send(200, {"cleared": True})
            elif path == "/api/paths/unify":
                self._send(200, unify_paths(body.get("keys")))
            elif path == "/api/storage/clean":
                self._send(200, clean_storage())
            elif path == "/api/config":
                self._send(200, save_config(body))
            elif path == "/api/naming/auto":
                self._send(200, naming_auto(body.get("ids")))
            elif path == "/api/artifacts/open":
                self._send(200, art_open(body.get("ids") or []))
            elif path == "/api/artifacts/reveal":
                self._send(200, art_reveal(body.get("id") or ""))
            elif path == "/api/artifacts/trash":
                self._send(200, art_trash(body.get("ids") or [],
                                          bool(body.get("force"))))
            elif path == "/api/artifacts/restore":
                self._send(200, art_trash_restore(body.get("ids") or []))
            elif path == "/api/artifacts/purge":
                self._send(200, art_trash_purge(body.get("ids")))
            elif path == "/api/archive":
                self._send(200, archive_sessions(body.get("ids") or [],
                                                 bool(body.get("archive", True))))
            else:
                self._send(404, {"error": "not found"})
        except sqlite3.OperationalError as e:
            # 最常见的是 database is locked：翻译成用户能自己处理的说明
            self._send(503, {"error": _db_busy_msg(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


# ---------- 存活心跳：页面一关就自己退，不在后台白占内存 ----------
# 页面每 10 秒打一次 /api/ping；关闭窗口时用 sendBeacon 打 /api/bye。
# 收到 bye 后留 6 秒缓冲（刷新页面会先 bye 再 ping，靠这个缓冲救回来）。
# 浏览器把后台标签的定时器限流到每分钟一次，所以兜底阈值放到 180 秒。
_LIVE = {"last": time.time(), "bye": 0.0, "seen": False, "t0": time.time(),
         "lock": threading.Lock()}
IDLE_GRACE = 6
IDLE_LIMIT = 180
# 启动后一直没有页面连过来（浏览器没打开成功、地址没人用）时的兜底：
# 服务留着是等用户手动打开，但不能永远挂着——那会变成没有界面的幽灵进程。
NO_PAGE_LIMIT = 300


def touch() -> None:
    """普通请求：只把「最后一次活动」往后推。"""
    with _LIVE["lock"]:
        _LIVE["last"] = time.time()
        _LIVE["bye"] = 0.0


def ping() -> None:
    """页面心跳：从这一刻起才启用「关掉就退出」的判定。
    没人 ping 过（自测、脚本调用）就永远不自动退，避免误杀。"""
    with _LIVE["lock"]:
        _LIVE["last"] = time.time()
        _LIVE["bye"] = 0.0
        _LIVE["seen"] = True


def say_bye() -> None:
    with _LIVE["lock"]:
        if not _LIVE["bye"]:
            _LIVE["bye"] = time.time()


def idle_reason() -> str:
    """返回非空字符串表示该关掉服务了。"""
    with _LIVE["lock"]:
        now = time.time()
        if not _LIVE["seen"]:
            # 还没有页面连过：多半是浏览器没自动打开，用户可能正在手动开。
            # 给一段时间等；过了还没人来就退出，别留一个没有界面的进程。
            if now - _LIVE["t0"] >= NO_PAGE_LIMIT:
                mins = max(1, NO_PAGE_LIMIT // 60)
                return f"启动后 {mins} 分钟一直没打开页面"
            return ""      # 还没超时，继续等
        last, bye = _LIVE["last"], _LIVE["bye"]
    if bye and now - bye >= IDLE_GRACE and now - last >= IDLE_GRACE:
        return "页面已关闭"
    if now - last >= IDLE_LIMIT:
        return f"已经 {IDLE_LIMIT} 秒没有页面连着"
    return ""


def watch_idle(srv) -> None:
    while True:
        time.sleep(1.0)
        why = idle_reason()
        if why:
            print(f"\n[{why}]，服务自动退出")
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
            return


def running_url() -> str:
    """上次开着的服务还在不在。在就把地址给它，不重复起第二个。"""
    try:
        p = int((HERE / "port.txt").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return ""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/ping", timeout=1.5) as r:
            if r.status == 200:
                return f"http://127.0.0.1:{p}/"
    except Exception:  # noqa: BLE001
        return ""
    return ""


def open_app_window(url: str) -> bool:
    """优先 Edge/Chrome 的 --app 模式：没有地址栏和标签页，观感就是桌面应用。
    都找不到再退回系统默认浏览器。

    返回有没有真的打开。全都失败时调用方要把地址直接摆到用户面前——
    否则（尤其打包版无控制台）用户看到的是「双击没反应」，
    而进程还在后台开着，只能去任务管理器里找。
    """
    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for exe in candidates:
        if not exe or not Path(exe).exists():
            continue
        try:
            subprocess.Popen([exe, f"--app={url}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("已用应用窗口打开（浏览器 --app 模式）")
            return True
        except OSError:
            continue
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001  没有可用浏览器时会抛 webbrowser.Error
        return False


def _no_browser_hint(url: str) -> None:
    """一个浏览器都没打开成功：把地址直接给用户，别让人对着空气猜。

    这里不退出进程——服务本身是好的，用户手动打开地址就能用。
    所以不复用 _fatal()（那个会 sys.exit）。
    """
    msg = (
        "没能自动打开浏览器。\n\n"
        "请手动打开下面这个地址（复制到浏览器地址栏）：\n"
        f"{url}\n\n"
        "Edge 或 Chrome 都可以。页面一打开就能用；\n"
        "关掉页面后，这个程序几秒内会自己退出。"
    )
    print(msg)
    if getattr(sys, "frozen", False) and IS_WIN:
        try:
            import ctypes
            # MB_ICONINFORMATION | MB_SETFOREGROUND：好消息，不用红色错误图标
            ctypes.windll.user32.MessageBoxW(
                None, msg, "wb-manager 已启动", 0x40 | 0x10000)
        except Exception:  # noqa: BLE001
            pass


def pick_port(start, tries=20):
    """端口被占就往后找，避免撞上别的程序。"""
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    sys.exit(f"{start} 往后 {tries} 个端口都被占了，换 --port 指定一个")


def main():
    global REAL_DB, DB_SOURCE, BACKUPS, NAMING, HISTORY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", help="手动指定 workbuddy.db 路径")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--install-shortcut", action="store_true",
                    help="在桌面建快捷方式后退出，不起服务")
    # 允许把 workbuddy.db 直接拖到 exe 上：这时候文件路径是位置参数
    ap.add_argument("dropped", nargs="?", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # 装快捷方式不需要数据库、也不需要写权限检查，先处理掉
    if args.install_shortcut:
        sys.exit(install_desktop_shortcut())

    # 数据目录写不进去就别往下走了：备份、历史、port.txt 全靠它
    if not check_app_dir_writable():
        _fatal(
            "程序所在的文件夹不能写入：\n"
            f"  {HERE}\n\n"
            "配置、备份和操作历史都要存在这个文件夹里，写不进去就没法用。\n"
            "最常见的原因是放在了需要管理员权限的位置，比如\n"
            "  C:\\Program Files\\\n\n"
            "把 wb-manager.exe 挪到一个普通文件夹再双击，例如：\n"
            "  D:\\工具\\wb-manager\\\n"
            "  或者桌面新建一个文件夹"
        )

    REAL_DB, DB_SOURCE = resolve_db(args.db or (args.dropped or None))

    # --db / 拖进来的库 和启动时认定的不是同一个：备份、历史、命名也要跟着换。
    # 不换的话会往「默认库」的目录里写另一个库的快照，照样是串库。
    # 这里不做老结构迁移——老版本那个库才是默认库，迁移只归它。
    if REAL_DB != _BOOT_DB:
        BACKUPS, NAMING, HISTORY = _per_db_paths(REAL_DB)
        _ensure_dirs()

    if not REAL_DB.exists():
        _fatal(
            "没找到 WorkBuddy 的数据文件：\n"
            f"{REAL_DB}\n"
            f"（查找依据：{DB_SOURCE}）\n\n"
            "两种可能：\n"
            "1. 这台电脑还没装过 WorkBuddy，或还没用它开过对话\n"
            "2. WorkBuddy 的数据目录被改过（换了盘 / 换了账户）\n\n"
            "如果是第 2 种，把这个文件拖到 wb-manager.exe 上，"
            "或在命令行里指定：\n"
            '   wb-manager.exe --db "D:\\你的路径\\workbuddy.db"'
        )
    if not UI.exists():
        _fatal(f"安装包不完整：找不到界面文件\n{UI}\n\n请重新解压一份完整的压缩包。")

    # 启动自检：连不上就别起服务，直接把原因打出来
    try:
        con = connect()
        n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        con.close()
        print(f"[db] 连接正常，{n} 条会话")
    except Exception as e:  # noqa: BLE001
        _fatal(f"打不开数据库：{type(e).__name__}: {e}\n\n路径：{REAL_DB}\n"
               "如果 WorkBuddy 正开着，先退出它再试。")

    # 已经开着一个就只把窗口唤出来，绝不起第二个（--port 指定别的端口时不管，方便自测）
    if args.port == 8765:
        u = running_url()
        if u:
            print(f"已经在运行：{u}")
            if not args.no_browser and not open_app_window(u):
                _no_browser_hint(u)
            return

    port = pick_port(args.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    # 把端口写下来，WorkBuddy 会话里的 AI 靠它找到服务，不用猜
    NAMING.mkdir(exist_ok=True)
    (HERE / "port.txt").write_text(str(port), encoding="utf-8")

    print(f"wb-manager 已启动：{url}")
    if port != args.port:
        print(f"（{args.port} 被占用，自动换到 {port}）")
    print(f"数据库：{REAL_DB}  （来源：{DB_SOURCE}）")
    print("按 Ctrl+C 停止")

    # 页面一关就自动退出，不在后台常驻（没人连过页面时不会退，自测不受影响）
    threading.Thread(target=watch_idle, args=(srv,), daemon=True).start()
    if not args.no_browser:
        def _open():
            if not open_app_window(url):
                _no_browser_hint(url)
        threading.Timer(0.6, _open).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        try:
            (HERE / "port.txt").unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()

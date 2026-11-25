"""会话标题规则：判断类型、提炼主题、生成建议标题。

跟插件里的 plan.py 同一套规则，但这里是给独立管理器用的，读真库。
"""
import json
import re
import time

# 类型判定：按顺序命中即止
# 前 7 类是老分类（顺序别动，动老顺序会让已有会话换类型），
# 后面是扩展类：AI 日常可能做的其他事情。扩展类放在老类后面——
# 老类关键词更宽（比如"配置""功能"），先让老类兜住，兜不住再落扩展类。
TYPE_RULES = [
    ("🐛", "排查", ["报错", "调试", "异常", "黑屏", "卡顿", "闪退", "死机", "崩溃", "失败", "故障", "排查", "兼容", "掉帧", "耗电"]),
    ("🗂️", "整理", ["整理", "清理", "归档", "迁移", "盘点", "备份", "收纳", "恢复", "删除"]),
    ("🎬", "制作", ["剪辑", "出片", "素材", "镜头", "调色", "片头", "成片", "混剪", "字幕", "配音", "拍摄"]),
    ("📝", "文案", ["文案", "选题", "口播", "爆款", "笔记", "简介", "描述", "脚本内容", "短文案"]),
    ("💰", "事务", ["报销", "发票", "税务", "社保", "记账", "账单", "对账", "工资", "合同", "欠款",
                   "退款", "理赔", "押金", "缴费", "还款", "票务"]),
    ("🛠️", "开发", ["开发", "编程", "代码", "脚本", "插件", "接口", "api", "数据库", "前后端",
                    "框架", "编译", "部署", "调试", "函数", "组件", "重构", "环境", "依赖"]),
    ("🔍", "调研", ["调研", "资料", "查证", "科普", "背景", "原理", "由来", "行业", "市场"]),
    # ---- 扩展类型：AI 日常可能做的其他事情 ----
    ("✍️", "写作", ["写一", "帮我写", "文章", "作文", "周报", "日报", "总结", "汇报", "演讲",
                   "致辞", "公文", "通知", "邮件", "回信", "润色", "改写", "续写", "推文", "公众号"]),
    ("🌐", "翻译", ["翻译", "译成", "英译", "中译", "翻成"]),
    ("📊", "分析", ["数据", "统计", "报表", "透视", "图表", "分析", "复盘", "归因", "可视化"]),
    ("🎓", "学习", ["教程", "讲解", "学习", "入门", "教学", "课程", "题目", "作业", "论文", "文献", "概念"]),
    ("🎨", "设计", ["海报", "封面", "配图", "logo", "图标", "配色", "排版", "设计稿", "原型", "草图", "字体"]),
    ("🖥️", "系统", ["电脑", "驱动", "显卡", "内存", "硬盘", "网络", "wifi", "路由", "重装", "蓝屏", "越狱", "刷机"]),
    ("🏃", "健康", ["健身", "训练", "饮食", "热量", "蛋白", "睡眠", "体检", "用药", "症状", "康复",
                   "减脂", "增肌", "拉伸", "作息"]),
    ("🧠", "情绪", ["情绪", "焦虑", "内耗", "心理", "倾诉", "安慰", "emo", "压力"]),
    ("🛒", "消费", ["选购", "推荐", "比价", "测评", "避坑", "购物", "下单", "退换", "保修", "外设", "香水"]),
    ("✈️", "出行", ["旅行", "行程", "机票", "酒店", "路线", "签证", "通勤", "地铁"]),
    ("💼", "求职", ["简历", "面试", "求职", "跳槽", "offer", "竞业"]),
    ("⚖️", "法律", ["仲裁", "诉讼", "劳动法", "起诉", "律师", "立案", "调解", "申诉"]),
    ("📅", "生活", ["日程", "计划", "提醒", "待办", "习惯", "作息", "年假", "请假"]),
    ("📣", "运营", ["运营", "涨粉", "养号", "社群", "私域", "投放", "流量", "转化", "复购", "矩阵", "增长", "留存"]),
]

# 目录判定：按目录尾名命中。**这里不放个人目录名**——
# 把你自己的关键词写进 rules_local.py（本文件会自动加载它），格式同上：
#   TYPE_RULES / DIR_RULES / AUTOMATION_LABEL，想覆盖哪个就定义哪个。
DIR_RULES = []

# 已知的定时任务，给固定简称。个人化条目同样放 rules_local.py
AUTOMATION_LABEL = {}

# 加载个人配置（不存在就跳过，用上面的通用默认）
try:
    import rules_local as _LOCAL
    for _name in ("TYPE_RULES", "DIR_RULES", "AUTOMATION_LABEL"):
        if hasattr(_LOCAL, _name):
            globals()[_name] = getattr(_LOCAL, _name)
except ImportError:
    pass

# 自定义类型规则（界面上配置，存 config.json）。用户定义优先于内置规则。
_BASE_TYPE_RULES = None

def set_custom_rules(items):
    """用界面配置的自定义类型重置判定表。items: [{emoji,name,keys}]"""
    global TYPE_RULES, _BASE_TYPE_RULES
    if _BASE_TYPE_RULES is None:
        _BASE_TYPE_RULES = list(TYPE_RULES)
    TYPE_RULES = list(_BASE_TYPE_RULES)
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        keys = [str(k).strip().lower() for k in (it.get("keys") or []) if str(k).strip()]
        if name and keys:
            TYPE_RULES.insert(0, (str(it.get("emoji") or "").strip() or "🏷️", name, keys[:30]))

EMOJI_ANY = re.compile(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF][\uFE0F]? ")
SEP = "｜"


def norm_path(p):
    """统一 cwd 写法：盘符大写、反斜杠、去尾斜杠。"""
    if not p:
        return p
    s = str(p).replace("/", "\\")
    s = re.sub(r"\\{2,}", r"\\", s)
    if len(s) >= 2 and s[1] == ":":
        s = s[0].upper() + s[1:]
    if len(s) > 3 and s.endswith("\\"):
        s = s[:-1]
    return s


def tail_dir(cwd):
    return (norm_path(cwd or "").split("\\") or [""])[-1]


def classify(title, cwd):
    """返回 (emoji, 类型名, 依据)。标题优先，标题没命中看目录。"""
    low = (title or "").lower()
    for emoji, name, keys in TYPE_RULES:
        for k in keys:
            if k in low:
                return emoji, name, f"标题含「{k}」"
    tail = tail_dir(cwd).lower()
    for emoji, name, keys in DIR_RULES:
        for k in keys:
            # 只做精确匹配，不用 in——子串会误伤名字更长的目录
            if k.lower() == tail:
                return emoji, name, f"目录「{tail_dir(cwd)}」"
    return "💬", "问答", ""


def mmdd(ms):
    return time.strftime("%m-%d", time.localtime((ms or 0) / 1000))


def suggest(row, style="emoji"):
    """给一条会话算建议标题。

    row 需要: title, custom_title, cwd, created_at, is_background_automation
    style:   命名风格模板
             emoji = "🗂️ 整理｜主题"（默认）
             plain = "整理｜主题"（纯文字）
             date  = "08-11 主题"（日期打头，适合按时间翻）

    返回 dict: {after, emoji, type_name, why, conflict}
    """
    raw = re.sub(r"\s+", " ", (row.get("custom_title") or row.get("title") or "").strip())
    if style == "date":
        # 日期风格每次都往前面拼一个「MM-DD」。不先剥掉旧的，
        # 就会 10-27 主题 → 10-27 10-27 主题 → 越整理越长。
        raw = re.sub(r"^\d{2}-\d{2}\s+", "", raw).strip() or raw
    is_auto = bool(row.get("is_background_automation"))

    def _fmt(emoji, name):
        """按风格模板拼最终标题。"""
        if style == "plain":
            return name
        if style == "date":
            return f"{mmdd(row.get('created_at'))} {name}"
        return f"{emoji} {name}" if emoji else name

    if is_auto:
        # ⛔ 铁律（2026-11-24 用户定死）：is_background_automation=1 的会话一律只读，
        # 是「全部定时任务」，不是只放过美团那一条，也不是只放过今天这几条。
        # 以后看到有人想把这里改成「也参与改名以保持格式统一」——拒绝，理由是
        # 定时任务由 WorkBuddy 侧栏独立分类管理，两边各改一套必然打架。
        # 定时任务不归这个工具管。WorkBuddy 已经把这些会话收进独立的
        # 定时任务分类，标题也由它按日期自己生成。工具再插一手只会越改越乱：
        # 旧版在这里每次运行都往前面补一个 🔁 和一段日期，而且不先剥旧的，
        # 跑两次就成了「🔁 🔁 美团领券｜09-29｜09-29」，跑三次还会更长。
        # 所以这里直接返回原名：after == raw 时 changed 恒为 False，
        # 它永远不会出现在「待改」清单里，一键整理也碰不到。
        return {
            "after": raw,
            "emoji": "🔁", "type_name": "定时",
            "why": "定时任务：由 WorkBuddy 统一管理，这里保持原名不动",
            "conflict": False,
        }

    body = EMOJI_ANY.sub("", raw).strip() or raw
    emoji, tname, why = classify(body, row.get("cwd"))

    # 已经有正确前缀的，不动（仅 emoji 风格下；其他风格统一重排）
    if style == "emoji" and body == raw and EMOJI_ANY.match(raw) and SEP in raw:
        # 形如 "🛠️ 主题｜动作"，判断前缀是否合理
        cur_emoji = EMOJI_ANY.match(raw).group(0).strip()
        if cur_emoji in tuple(e for e, _, _ in TYPE_RULES) + ("💬", "🔁"):
            return {"after": raw, "emoji": cur_emoji, "type_name": tname,
                    "why": "已是规范格式", "conflict": False}

    after = _fmt(emoji, body)
    return {
        "after": after, "emoji": emoji, "type_name": tname,
        "why": why or "没认出这条在干什么，需要你人工定一下",
        "conflict": not why,
    }


# ---------- 读会话内容（给 AI 命名用，只读不改）----------

def read_digest(path, max_users=3, max_chars=180, max_tools=12):
    """从会话 jsonl 里提取一段摘要，用于让模型理解这次会话到底做了什么。

    只读，不改任何东西。读不动就返回空结构，绝不抛异常影响主流程。
    """
    out = {"first_user": "", "users": [], "tools": [], "ai_title": "",
           "messages": 0, "files": 0}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "ai-title":
                    out["ai_title"] = (d.get("aiTitle") or "").strip()
                elif t == "message":
                    out["messages"] += 1
                    if d.get("role") != "user":
                        continue
                    txt = _msg_text(d.get("content"))
                    if txt and len(out["users"]) < max_users:
                        out["users"].append(txt[:max_chars])
                elif t == "function_call":
                    name = d.get("name") or (d.get("function") or {}).get("name")
                    if name and name not in out["tools"] and len(out["tools"]) < max_tools:
                        out["tools"].append(name)
                elif t == "file-history-snapshot":
                    out["files"] += 1
    except OSError:
        pass
    out["first_user"] = out["users"][0] if out["users"] else ""
    return out


def _msg_text(content):
    """从 message.content 里取纯文本。

    实测 WorkBuddy 的类型是 input_text（用户）/ output_text（助手），
    不是通用的 text。system-reminder 是系统注入的上下文，不是用户说的话，要剔掉。
    """
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                if (c.get("type") or "") in ("text", "input_text", "output_text"):
                    parts.append(c.get("text") or "")
        return _clean(" ".join(parts))
    return ""


SYS_PREFIX = ("<system-reminder", "<user_info", "<identity_context", "<memory", "<user_memory")


def _clean(s):
    s = (s or "").strip()
    if not s:
        return ""
    # 系统注入的块：<system-reminder> <task-notification> <cb_summary> <user_info> ...
    # 真实提问极少以 < 开头，这类一律当噪音丢掉
    if s.startswith("<") and ">" in s[:60]:
        return ""
    return re.sub(r"\s+", " ", s).strip()

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
    ("🐛", "排查", ["报错", "调试", "异常", "黑屏", "卡顿", "闪退", "死机", "崩溃", "失败", "故障", "排查", "兼容", "掉帧", "耗电",
                   "修复", "修好", "不生效", "没反应", "打不开", "重复", "错乱", "失效"]),
    ("🗂️", "整理", ["整理", "清理", "归档", "迁移", "盘点", "备份", "收纳", "恢复", "删除"]),
    ("🎬", "制作", ["剪辑", "出片", "素材", "镜头", "调色", "片头", "成片", "混剪", "字幕", "配音", "拍摄"]),
    ("📝", "文案", ["文案", "选题", "口播", "爆款", "笔记", "简介", "描述", "脚本内容", "短文案"]),
    ("💰", "事务", ["报销", "发票", "税务", "社保", "记账", "账单", "对账", "工资", "合同", "欠款",
                   "退款", "理赔", "押金", "缴费", "还款", "票务"]),
    ("🛠️", "开发", ["开发", "编程", "代码", "脚本", "插件", "接口", "api", "数据库", "前后端",
                    "框架", "编译", "部署", "调试", "函数", "组件", "重构", "环境", "依赖",
                    "程序", "写个工具", "命令行"]),
    ("🔍", "调研", ["调研", "资料", "查证", "科普", "背景", "原理", "由来", "行业", "市场"]),
    # ---- 扩展类型：AI 日常可能做的其他事情 ----
    ("✍️", "写作", ["写一", "帮我写", "文章", "作文", "周报", "日报", "总结", "汇报", "演讲",
                   "致辞", "公文", "通知", "邮件", "回信", "润色", "改写", "续写", "推文", "公众号"]),
    ("🌐", "翻译", ["翻译", "译成", "英译", "中译", "翻成"]),
    ("📊", "分析", ["数据", "统计", "报表", "透视", "图表", "分析", "复盘", "归因", "可视化"]),
    ("🎓", "学习", ["教程", "讲解", "学习", "入门", "教学", "课程", "题目", "作业", "论文", "文献", "概念"]),
    ("🎨", "设计", ["海报", "封面", "配图", "logo", "图标", "配色", "排版", "设计稿", "原型", "草图", "字体"]),
    ("🖥️", "系统", ["电脑", "驱动", "显卡", "内存", "硬盘", "网络", "wifi", "路由", "重装", "蓝屏", "越狱", "刷机",
                    "电池", "充电", "主板", "发热", "温度", "风扇", "开机"]),
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


# WorkBuddy 自带、不该被整理的固定会话。
# 目前只认「助理」：它是 WorkBuddy 内置的助手对话，侧栏本来就有一个入口。
# 工具要是给它加 emoji、挪空间，侧栏里就会冒出两个「助理」，用户会以为工具改了系统。
# 判断前先剥掉前导 emoji 与符号，否则已经被改过一次的「🛠️ 助理」会漏判。
RESERVED_TITLES = {"助理"}


def _field(row, key):
    """取字段，兼容 dict 与 sqlite3.Row（后者没有 .get）。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def is_reserved(row):
    """这条会话是不是 WorkBuddy 自带、必须原样留着的那种。"""
    for k in ("custom_title", "title"):
        v = (_field(row, k) or "").strip()
        v = re.sub(r"^[^\w\u4e00-\u9fa5]+", "", v).strip()
        if v in RESERVED_TITLES:
            return True
    return False


def suggest(row, style="emoji", digest=None):
    """给一条会话算建议标题。

    row 需要: title, custom_title, cwd, created_at, is_background_automation
    style:   命名风格模板
             emoji = "🗂️ 整理｜主题"（默认）
             plain = "整理｜主题"（纯文字）
             date  = "08-11 主题"（日期打头，适合按时间翻）
    digest:  可选，read_digest_brief 的结果。用户没自己起名时，
             命名以对话内容为准，没有 digest 才退回看标题。

    返回 dict: {after, emoji, type_name, why, conflict, pending}
    pending=True 表示「对话信息不足，保留原名等你确认」，不进待改清单。
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

    if is_reserved(row):
        # 和定时任务同一个道理：直接返回原名，after == raw 时 changed 恒为 False，
        # 它就不会出现在待改清单里，一键整理也筛不到，写库层还有第二道闸。
        return {
            "after": raw,
            "emoji": "🛠️", "type_name": "内置",
            "why": "WorkBuddy 自带会话（如「助理」）：保持原名，不参与整理",
            "conflict": False,
        }

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

    cur = (row.get("custom_title") or "").strip()

    # 用户自己起过名的，尊重原样；没起过名的，名字必须从对话内容里定，
    # 不能拿「新建会话 2026-08-11 2030」加个 emoji 糊过去。
    if cur:
        body = EMOJI_ANY.sub("", raw).strip() or raw
        topic_src = "你自己起的名字"
    else:
        topic, topic_src = pick_topic(digest, row)
        if topic is None:
            # 信息不足就不硬造。保留原名，标成待命名，把判断依据说清楚，
            # 让用户知道「不是没处理，是这条确实看不出在干什么」。
            return {
                "after": raw,
                "emoji": "", "type_name": "待命名",
                "why": f"没法定名字：{topic_src}。保留原名，等你手动定一下",
                "conflict": False,
                "pending": True,
            }
        body = topic

    # 类型判断用「内容主题 + 原标题 + 目录」一起看：主题决定它是什么，
    # 标题和目录补上类型线索。emoji 只是最后贴上去的装饰，不参与命名。
    emoji, tname, why_cls = classify(f"{body} {row.get('title') or ''}", row.get("cwd"))
    if topic_src in ("WorkBuddy 生成的对话标题", "对话第一条消息"):
        why = f"读了{topic_src}，按实际做的事命名"
    else:
        why = why_cls or f"按{topic_src}整理"

    # 已经有正确前缀的，不动（仅 emoji 风格下；其他风格统一重排）
    if style == "emoji" and body == raw and EMOJI_ANY.match(raw) and SEP in raw:
        cur_emoji = EMOJI_ANY.match(raw).group(0).strip()
        if cur_emoji in tuple(e for e, _, _ in TYPE_RULES) + ("💬", "🔁"):
            return {"after": raw, "emoji": cur_emoji, "type_name": tname,
                    "why": "已是规范格式", "conflict": False}

    after = _fmt(emoji, body)
    return {
        "after": after, "emoji": emoji, "type_name": tname,
        "why": why,
        # 类型没认出来不算「需确认」——名字是从对话内容里定出来的，
        # 只是归不到哪个类型，不影响用。真要挂个「待确认」，用户会以为工具没干活。
        "conflict": False,
        "pending": False,
    }


# ---------- 读会话内容（给 AI 命名用，只读不改）----------

# ---------- 从对话内容里定主题 ----------
#
# 光看标题定不出「这条在干什么」：WorkBuddy 的默认标题是「新建会话 2026-08-11 2030」，
# 在这种标题后面加个 emoji，等于什么都没改。所以命名必须回到对话本身。
#
# 两个来源，按可信度排：
#   1. jsonl 里的 ai-title —— WorkBuddy 自己根据对话生成的主题，通常最准
#   2. 用户的第一条消息 —— 退而求其次，从原话里提炼
# 实测这两样都在文件前 2% 以内（29MB 的会话里 ai-title 出现在 1.9% 处），
# 所以只读文件头，不读完整份——全量读 35 条要 7.9 秒，只读头不到 0.4 秒。

# WorkBuddy 建的模板标题，这些名字本身不携带任何信息
TEMPLATE_TITLE = re.compile(
    r"^(新建会话|新会话|新对话|未命名|无标题|会话|新建任务|新任务|"
    r"untitled|new chat|new conversation|chat)\s*[\d\-:：\s]*$", re.I)

# 开头这些词是客套或指代，不构成主题。
# 按长度从长到短排，避免「帮我」先命中把「帮我看看」拆成两半。
LEAD_NOISE = ("帮我看看", "帮我看下", "帮我", "帮忙", "麻烦你", "麻烦一下", "麻烦",
              "我想问问", "我想问", "我想", "我要", "我需要", "请问", "你好",
              "现在", "那个", "这个", "看看", "看下", "看一下", "问一下",
              "能帮", "可以帮", "希望你", "希望", "需要你")


def is_template_title(s):
    """这个标题是不是「什么都没说」的模板名。"""
    s = (s or "").strip()
    if not s:
        return True
    if TEMPLATE_TITLE.match(s):
        return True
    # 纯日期 / 纯数字，同样没有信息
    return bool(re.fullmatch(r"[\d\s\-:：./]+", s))


# 这些词不承载主题，去掉不影响语义，但留着会白占标题长度、
# 把后面真正有信息的部分（比如「失败」）挤出截断范围。
FILLER_RE = re.compile(r"(为什么|怎么样|一直|总是|老是|到底|究竟|请问)")
# 截断后可能刚好停在半个疑问词上，再收一次尾
TAIL_NOISE = re.compile(
    r"(为什么|怎么样|怎么回事|怎么回|怎么|如何|一直|总是|到底是|是不是|是|的|吗|呢|啊)$")


def clean_topic(text, limit=18):
    """把用户原话收成一个短主题。

    不做「猜意图」那种事，只做三件事：去掉客套、去掉不承载主题的填充词、
    超长时在标点处断。宁可短一点、粗一点，也不要把整句原话搬过来当标题。
    """
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if not s:
        return ""
    # 只取第一句：主题一般在开头，后面都是背景交代
    s = re.split(r"[。！？!?\n]", s)[0].strip()
    changed = True
    while changed and s:
        changed = False
        for w in LEAD_NOISE:
            if s.startswith(w):
                s = s[len(w):].strip(" ，,、：:")
                changed = True
    s = FILLER_RE.sub("", s).strip(" ，,、：:")
    # 一句话里第一个逗号前通常就是主题，后面是「帮我彻底检查一下」这类补充。
    # 前段够长就直接用它，比硬截断干净。
    head = re.split(r"[，,、；;]", s)[0].strip()
    if len(head) >= 6 and len(head) < len(s):
        s = head
    if len(s) > limit:
        parts = re.split(r"[，,、；;：:]", s)
        acc = ""
        for seg in parts:
            seg = seg.strip()
            if not seg:
                continue
            if len(acc) + len(seg) > limit:
                break
            acc += ("，" if acc else "") + seg
        if acc:
            s = acc
        else:
            cut = s[:limit]
            # 断点后紧跟的还是汉字，说明切在词中间，退一个字。
            # 不退会出现「进行了一波调整升」这种半截词，比短一点难看得多。
            if len(s) > limit and re.match(r"[\u4e00-\u9fa5]", s[limit]):
                cut = cut[:-1]
            s = cut
    while True:
        t = TAIL_NOISE.sub("", s).strip(" ，,、。！？：:;；")
        # 删到只剩一两个字就停：再删就不成一个主题了
        if t == s or len(t) < 2:
            break
        s = t
    return s.strip(" ，,。！？、：:;；")


def read_digest_brief(path, max_bytes=2 * 1024 * 1024):
    """只读文件头，取会话主题。读不动返回空结构，绝不抛异常。

    上限设 2MB 是为了对付前面塞满 teammate-message / system-reminder 的会话：
    那些块会被 _clean 丢掉，真实提问可能排在很后面。
    好在绝大多数会话在前几十 KB 就两样都拿到了，会提前 break。
    """
    out = {"ai_title": "", "first_user": "", "users": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            used = 0
            for line in fh:
                used += len(line)
                if used > max_bytes:
                    break
                if '"ai-title"' not in line and '"message"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "ai-title":
                    v = (d.get("aiTitle") or "").strip()
                    if v:
                        out["ai_title"] = v     # 取最后一条：越往后越贴近真实内容
                elif t == "message" and d.get("role") == "user":
                    txt = _msg_text(d.get("content"))
                    if txt and len(out["users"]) < 2:
                        out["users"].append(txt[:400])
                # 两样都拿到了就没必要继续读
                if out["users"] and out["ai_title"]:
                    break
    except OSError:
        pass
    out["first_user"] = out["users"][0] if out["users"] else ""
    return out


def pick_topic(digest, row):
    """定出这条会话在干什么。返回 (主题, 来源说明)；定不出来返回 (None, 原因)。

    顺序是「AI 标题 → 用户第一句 → 原标题」，谁先给出可用主题就用谁。
    """
    digest = digest or {}
    # AI 生成的标题本身已经是提炼过的，可以长一点；从用户原话里抠出来的要收紧，
    # 否则会拖一串背景交代进来。硬截断会把「修复会话标题重复叠加」切成半句，
    # 反而比不改还糟，所以这两档长度必须分开。
    candidates = [
        (digest.get("ai_title"), "WorkBuddy 生成的对话标题", 32),
        (digest.get("first_user"), "对话第一条消息", 18),
        (row.get("title"), "原标题", 18),
    ]
    for text, src, lim in candidates:
        if not text:
            continue
        if is_template_title(text):
            continue
        topic = clean_topic(text, limit=lim)
        # 太短的（一个字、两个字）也当没信息，撑不起一个能区分的名字
        if len(topic) >= 2:
            return topic, src
    return None, "标题是模板名，对话里也没有可用的第一条消息"


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


# 会话里被注入的上下文块。用户的真实提问经常跟这些块拼在同一条消息里：
#   [{'type':'input_text','text':'<system-reminder><user_info>OS Version…</user_info></system-reminder>帮我看看电池…'}]
# 直接按「以 < 开头就丢掉」处理，会把用户真正说的话一起扔掉——
# 实测 45MB 的会话里 37 条消息全是这样，结果一条主题都提不出来。
INJECT_TAGS = ("system-reminder", "user_info", "identity_context", "user_memory",
               "memory", "project_context", "current-expert", "connector-status",
               "additional-data", "task-notification", "teammate-message",
               "cb_summary", "wecom_wedrive_references", "expert_selection")


def _strip_inject(s):
    """把注入块整段剥掉，留下用户自己写的那部分。"""
    for tag in INJECT_TAGS:
        s = re.sub(r"<%s\b[^>]*>.*?</%s>" % (tag, tag), " ", s, flags=re.S | re.I)
    # 剥完还剩未闭合的残标签，一起清掉
    s = re.sub(r"</?[a-zA-Z][\w-]*(?:\s[^>]*)?>", " ", s)
    return s


def _clean(s):
    s = (s or "").strip()
    if not s:
        return ""
    if "<" in s:
        s = _strip_inject(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

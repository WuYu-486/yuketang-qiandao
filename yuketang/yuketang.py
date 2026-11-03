import asyncio
import base64
import websockets
import json
import requests
import os
import time
import traceback
from util import *
from random import*
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qywxbot.send import *
try:
    from ai_answer import solve_problem as ai_solve_problem, extract_image_urls
except ImportError:          # 万一以包的形式被导入
    from yuketang.ai_answer import solve_problem as ai_solve_problem, extract_image_urls

# 异常重试间隔（秒）。上游靠 start_time<6000 兜底退出，改成常驻监听后
# 必须自己限速，否则登录态失效或断网时会变成不间隔的死循环猛刷服务端。
RETRY_INTERVAL = 5
# 单次请求超时（秒）。上游一个 timeout 都没设，弱网时整个监听会无声卡死，
# 界面还一直显示“运行中”，所以这里统一补上。
REQUEST_TIMEOUT = 15
# 课内监听的心跳间隔（秒）：这么久没收到任何服务端事件就打印一行心跳，
# 让界面和日志知道进程还活着。
HEARTBEAT_INTERVAL = 300
# 交答案前随机等待的秒数区间。大模型本身还要思考几秒，这里只留一点缓冲，
# 等太久容易撞上题目关闭时间。想调就改这一行。
ANSWER_DELAY_RANGE = (3, 5)
# 题目图片下载的总预算（秒）。requests 的 timeout 只约束单次 socket 读写，
# 图片源如果一点点吐数据，单张图能拖好几分钟，最后把大模型请求也拖到超时。
# 超预算就少带一张图，先保证整道题能答上。
IMAGE_DOWNLOAD_BUDGET = 20
# 单张图片的总下载时长上限（秒）
IMAGE_DOWNLOAD_TIMEOUT = 15

# 支持的大模型服务商（都是 OpenAI 兼容接口）
AI_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-max",
    },
}
# 依次尝试的环境变量名（Windows 上变量名带中划线也能正常读到）
AI_ENV_KEYS = (
    ("DEEPSEEK_API_KEY", "deepseek"),
    ("DEEPSEEK-API-KEY", "deepseek"),
    ("DEEPSEEK_KEY", "deepseek"),
    ("DASHSCOPE_API_KEY", "dashscope"),
    ("DASHSCOPE-API-KEY", "dashscope"),
)
DEFAULT_AI_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_AI_MODEL = "deepseek-chat"

# 签到请求里的 source：雨课堂用它来记录这条考勤的“签到方式”。
# source 是官方前端从 URL 的 ?source= 读出来再放进请求体的（lesson 页里
# this.source = query.source || 5），雨课堂没有公开过枚举含义，
# 下面是社区实践整理出来的对照，仅供参考。
CHECKIN_SOURCE_NAMES = {
    1: "微信/扫二维码",
    5: "公众号“正在上课”提示 / 网页默认",
    14: "PC / Web",
    21: "二维码",
    22: "暗号",
    23: "APP 点击课堂",
    91: "旁听（观摩）",
}
# 默认按“微信/扫二维码”进课堂
DEFAULT_CHECKIN_SOURCE = 1


class LoginExpired(Exception):
    """cookie 已经失效，需要重新扫码登录"""


def _provider_hint(env_name):
    """从环境变量名猜是哪家的 Key"""
    upper = env_name.upper()
    if "DEEPSEEK" in upper:
        return "deepseek"
    if "DASHSCOPE" in upper or "QWEN" in upper or "BAILIAN" in upper:
        return "dashscope"
    return ""


def _guess_image_mime(data):
    """图片服务器没给 Content-Type 时，按文件头猜一个"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


def _has_content(value):
    """判断答案字段是不是真的有内容：[]、[[]]、""、{} 都算空"""
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(_has_content(item) for item in value)
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    return str(value).strip() != ""


# 不同版本的接口 / 不同题型，答案可能藏在这些字段里
ANSWER_FIELDS = ("answers", "answer", "answerList", "answer_list",
                 "correctAnswers", "correct_answers", "correctAnswer", "keys")

# 雨课堂的题型编号
PROBLEM_TYPE_NAMES = {
    1: "单选题",
    2: "多选题",
    3: "投票题",
    4: "填空题",
    5: "主观题",
    6: "判断题",
}


def extract_server_answers(problem):
    """从雨课堂下发的题目数据里找答案，找不到就返回 None"""
    for field in ANSWER_FIELDS:
        value = problem.get(field)
        if _has_content(value):
            return value

    # 选项里自带“正确答案”标记的版本
    flagged = []
    for option in problem.get("options") or []:
        if not isinstance(option, dict):
            continue
        if (option.get("isAnswer") or option.get("is_answer")
                or option.get("isCorrect") or option.get("correct")):
            if option.get("key") is not None:
                flagged.append(option.get("key"))
    if flagged:
        return flagged

    # 填空题的答案可能挂在每个 blank 上
    blanks = problem.get("blanks")
    if isinstance(blanks, list) and blanks:
        texts, found = [], False
        for blank in blanks:
            if isinstance(blank, dict):
                for field in ANSWER_FIELDS:
                    value = blank.get(field)
                    if _has_content(value):
                        texts.append(value[0] if isinstance(value, list) else value)
                        found = True
                        break
                else:
                    texts.append("")
            elif _has_content(blank):
                texts.append(blank)
                found = True
            else:
                texts.append("")
        if found:
            return texts
    return None


def option_key_list(problem):
    """题目选项的 key 列表（选择 / 判断 / 投票题提交的就是这些 key）"""
    keys = []
    for index, option in enumerate(problem.get("options") or []):
        if isinstance(option, dict):
            key = option.get("key")
            if key is None:
                key = option.get("Key")
            if key is None:
                key = index
        else:
            key = index
        keys.append(key)
    return keys


# 答案里常见的装饰性字符，比较时忽略掉
_ANSWER_NOISE = " \t\r\n.,，。、:：;；!！?？\"'“”‘’()（）[]【】{}<>《》"


def _norm_text(value):
    """去掉空白和常见标点、把全角字母转半角，方便宽松比较"""
    out = []
    for char in str(value):
        if char in _ANSWER_NOISE:
            continue
        if "Ａ" <= char <= "Ｚ":
            char = chr(ord(char) - 0xFEE0)
        elif "ａ" <= char <= "ｚ":
            char = chr(ord(char) - 0xFEE0)
        out.append(char)
    return "".join(out).strip().upper()


def _answer_items(answers):
    """把答案拍平成列表，兼容 dict、嵌套 list 这些写法"""
    if answers is None:
        return []
    if isinstance(answers, dict):
        for field in ("answers", "answer", "key", "keys", "value", "result", "text"):
            if field in answers:
                return _answer_items(answers[field])
        return [item for item in answers.values() if _has_content(item)]
    if isinstance(answers, (list, tuple)):
        out = []
        for item in answers:
            out.extend(_answer_items(item))
        return out
    if isinstance(answers, (str, int, float)):
        return [answers] if _has_content(answers) else []
    return []


def _match_option_key(value, keys, problem):
    """把模型 / 服务端给的答案映射成选项的 key。

    模型经常不老实照抄，会出现 "B."、"（B）"、"答案：B"、"B 共模信号"
    甚至直接把选项文字抄回来这些写法，这里逐层放宽匹配。
    """
    text = str(value).strip()
    if not text:
        return None
    options = problem.get("options") or []
    norm = _norm_text(text)
    if not norm:
        return None

    # 1) 和选项 key 直接比
    for key in keys:
        if norm == _norm_text(key):
            return key
    # 2) 和选项文字直接比
    for key, option in zip(keys, options):
        body = option.get("value") if isinstance(option, dict) else option
        if body is not None and _norm_text(body) == norm:
            return key

    # 3) 开头就是选项字母，比如 "B."、"（B）"、"B、共模信号"
    head = norm[:1]
    if head.isalpha() and "A" <= head <= "Z":
        tail = norm[1:2]
        if len(norm) == 1 or not tail.isalpha():
            index = ord(head) - ord("A")
            if 0 <= index < len(keys):
                return keys[index]
    # 4) 纯数字：先按 1 开始，再按 0 开始
    if norm.isdigit():
        number = int(norm)
        if 1 <= number <= len(keys):
            return keys[number - 1]
        if 0 <= number < len(keys):
            return keys[number]
    # 5) 答案里夹着选项原文（比如 "我选共模信号"）
    for key, option in zip(keys, options):
        body = option.get("value") if isinstance(option, dict) else option
        if body is None:
            continue
        body_norm = _norm_text(body)
        if len(body_norm) >= 2 and (body_norm in norm or (len(norm) >= 2 and norm in body_norm)):
            return key
    # 6) 整句里只出现了一个选项字母，比如 "我选B"
    if keys and all(isinstance(key, str) and key.isalpha() for key in keys):
        hit = [key for key in keys if _norm_text(key) in norm]
        if len(hit) == 1:
            return hit[0]
    return None


def normalize_result(problem, answers):
    """把答案整理成 /lesson/problem/answer 需要的 result 格式。

    选择题提交的是选项 key 列表；填空题是每空一个字符串；主观题是一段文本。
    整理不出有效答案时返回 None（上层就会记成“跳过作答”，而不是提交空答案）。
    """
    items = _answer_items(answers)
    if not items:
        return None

    problem_type = int(problem.get("problemType") or 0)
    if problem_type == 5:            # 主观题
        text = "\n".join(str(item).strip() for item in items)
        return {"content": text, "pics": [{"pic": "", "thumb": ""}]}

    if problem_type == 4:            # 填空题：每个空一个字符串
        blanks = problem.get("blanks")
        count = len(blanks) if isinstance(blanks, list) and blanks else len(items)
        result = [str(item).strip() for item in items]
        while len(result) < count:
            result.append("")
        return result[:count]

    keys = option_key_list(problem)
    can_split = bool(keys) and all(isinstance(key, str) and key.isalpha() for key in keys)
    key_letters = {_norm_text(key) for key in keys}
    result = []
    for item in items:
        key = _match_option_key(item, keys, problem) if keys else item
        if key is not None:
            if key not in result:
                result.append(key)
            continue
        # 多选题模型常写成 "BC"、"A,C"、"A和C"，整体匹配不上就拆成单个选项
        if can_split:
            letters = [char for char in _norm_text(item) if "A" <= char <= "Z"]
            if len(letters) >= 2 and all(char in key_letters for char in letters):
                for char in dict.fromkeys(letters):
                    single = _match_option_key(char, keys, problem)
                    if single is not None and single not in result:
                        result.append(single)
    if problem_type in (1, 6):       # 单选 / 判断题只保留一个答案
        result = result[:1]
    return result or None


def api_ok(res):
    """雨课堂接口是否成功：HTTP 2xx 且响应体 code == 0。

    code==0 是这套接口的约定，见 check_cookie() 里对 basic-info 的判定
    （成功返回 {"code":0,"msg":"OK","data":...}）。
    """
    if res.status_code >= 400:
        return False
    try:
        body = res.json()
    except Exception:
        return True             # 2xx 但响应不是 JSON，就以 HTTP 状态为准
    if isinstance(body, dict) and body.get("code") is not None:
        return body.get("code") == 0
    return True


def api_reason(res):
    """失败时给一句人能看懂的说明，用于历史记录"""
    try:
        body = res.json()
        if isinstance(body, dict):
            for key in ("msg", "message", "errcode", "error"):
                if body.get(key):
                    return f"{body[key]}"
    except Exception:
        pass
    return f"HTTP {res.status_code}"


class yuketang:
    def __init__(self,name) -> None:
        self.name=name
        self.cookie_filename=f"cookie_{name}"
        self.cookie=""
        self.lessonId=""
        self.Auth=""
        self.Authorization=""
        self.userid=""
        self.presentation=""
        self.problemid=""
        self.problems={}
        self.problem_covers={}         # problemId -> 所在课件的整页图片，题目带图时交给模型
        self.lesson_filter=""          # 只签到课程名包含这个关键词的课（可留空）
        self.lesson_candidates=[]
        self._logged_candidates=None
        self._cookie_checked_at=0.0
        self.checkin_source=DEFAULT_CHECKIN_SOURCE   # 决定雨课堂记录下来的“签到方式”
        # 作答来源配置（run.py 会调用 configure_answer 覆盖）
        self.answer_source="server"    # server / ai / auto
        self.answer_api_key=""
        self.answer_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        self.answer_model=""
        self.answer_timeout=30
        self.answer_reasoning_effort=""
        self.answer_note=""
        self.answer_key_source=""
        self.answer_send_images=True   # 题目里的图片要不要一起发给多模态模型
        self._hint_ai_shown=False
        self._problem_deadlines={}     # problemId -> 作答截止时间戳（没有时限就是 None）
        self._answer_tasks=set()       # 正在等待/作答的题目任务，课程结束时统一取消
        self._answer_cancelled=False   # 课程结束后不再发起新的大模型作答

        self.debug=True
        self.wx=False
        self.msgmgr=MsgManager(debug=self.debug,wx=self.wx)
        # 修复：start_time 必须先于 getcookie() 初始化。
        # 否则首次运行时 getcookie() -> ws_controller() 会访问尚未赋值的
        # self.start_time 而抛 AttributeError，导致扫码登录永远走不通。
        self.start_time=time.time()
        self.getcookie()
        self.slides=""

    def configure_answer(self, answer_cfg):
        """设置课内题的作答来源，run.py 读 config.json 后调用"""
        cfg = dict(answer_cfg or {})
        self.answer_source = str(cfg.get("source") or "auto").strip().lower()
        self.answer_api_key = str(cfg.get("api_key") or "").strip()
        provider = ""
        self.answer_key_source = "config.json" if self.answer_api_key else ""

        # 没写在 config.json 里就去找环境变量：DEEPSEEK-API-KEY 这种带中划线的也能读到
        if not self.answer_api_key:
            wanted = str(cfg.get("api_key_env") or "").strip()
            names = ([(wanted, "")] if wanted else []) + list(AI_ENV_KEYS)
            for env_name, hint in names:
                value = (os.environ.get(env_name) or "").strip()
                if value:
                    self.answer_api_key = value
                    provider = hint or _provider_hint(env_name)
                    self.answer_key_source = f"环境变量 {env_name}"
                    break
        if not self.answer_api_key:
            key_path = os.path.join(os.getcwd(), "dashscope_key.txt")
            try:
                with open(key_path, "r", encoding="utf-8") as f:
                    self.answer_api_key = f.read().strip()
                if self.answer_api_key:
                    self.answer_key_source = "dashscope_key.txt"
            except OSError:
                pass
        # 接口地址和模型：config.json 里写了就用写的，否则按 Key 的来源自动选
        preset = AI_PROVIDERS.get(provider, {})
        self.answer_base_url = str(cfg.get("base_url")
                                   or preset.get("base_url") or DEFAULT_AI_BASE_URL).strip()
        self.answer_model = str(cfg.get("model")
                                or preset.get("model") or DEFAULT_AI_MODEL).strip()
        # 推理强度：DeepSeek 的 deepseek-flash 默认是 high，同一个带图题实测
        # 能生成几千个推理 token、单次要 60 秒上下，很容易撞上超时；课内题
        # 时间紧，所以默认降到 low。其他服务商不支持这个参数，默认不发。
        effort = str(cfg.get("reasoning_effort") or "").strip().lower()
        if effort not in ("low", "high", "max"):
            effort = ""
        if not effort and "deepseek.com" in self.answer_base_url.lower():
            effort = "low"
        self.answer_reasoning_effort = effort
        try:
            self.answer_timeout = int(cfg.get("timeout") or 90)
        except (TypeError, ValueError):
            self.answer_timeout = 90
        send_images = cfg.get("send_images")
        if isinstance(send_images, str):
            send_images = send_images.strip().lower() not in ("", "0", "false", "no", "off")
        self.answer_send_images = True if send_images is None else bool(send_images)

    def configure_checkin(self, source=None):
        """设置签到用的 source，决定雨课堂把这条考勤记成哪种“签到方式”"""
        try:
            value = int(source)
        except (TypeError, ValueError):
            value = DEFAULT_CHECKIN_SOURCE
        if value <= 0:
            value = DEFAULT_CHECKIN_SOURCE
        self.checkin_source = value
        return value

    def checkin_source_desc(self):
        name = CHECKIN_SOURCE_NAMES.get(self.checkin_source, "未知来源")
        return f"source={self.checkin_source}（{name}）"

    def answer_source_desc(self):
        """给日志用的一句话说明，告诉用户答题答案是从哪来的"""
        if self.answer_source == "server":
            return "只使用雨课堂下发的答案（拿不到就跳过作答）"
        if not self.answer_api_key:
            return "雨课堂下发的答案，拿不到就跳过作答（没有找到大模型 API Key）"
        mode = "优先雨课堂下发的答案，拿不到就用 AI" if self.answer_source == "auto" else "只用 AI"
        where = f"，Key 来自{self.answer_key_source}" if self.answer_key_source else ""
        effort = f"，推理强度={self.answer_reasoning_effort}" if self.answer_reasoning_effort else ""
        return f"{mode}（{self.answer_base_url} / {self.answer_model}{effort}{where}）"

    def getcookie(self):
        if not os.path.exists(self.cookie_filename):
            self.msgmgr.sendMsg(f"正在第一次获取登录{self.name} cookie，请注意扫码",self.name)
            self.ws_controller(self.ws_login)
        with open(self.cookie_filename,"r")as f:
            self.cookie=f.read()
        if not self.check_cookie():
            self.msgmgr.sendMsg("cookie已失效，请重新扫码",self.name)
            self.ws_controller(self.ws_login)
        else:
            self.msgmgr.sendMsg(f"{self.name} cookie有效",self.name)

    def weblogin(self,UserID,Auth):
        url="https://www.yuketang.cn/pc/web_login"
        data={
            "UserID":UserID,
            "Auth":Auth
        }
        headers={
            "referer":"https://www.yuketang.cn/web?next=/v2/web/index&type=3",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Content-Type":"application/json"
        }
        res=requests.post(url=url,headers=headers,json=data,timeout=REQUEST_TIMEOUT)
        cookies = res.cookies
        self.cookie=""
        for k,v in cookies.items():
            self.cookie += f'{k}={v};'
        if not self.cookie:
            raise RuntimeError("登录接口没有返回 cookie，扫码登录没有成功")
        # 先写临时文件再替换，避免写到一半被关掉后留下半个 cookie
        tmp=self.cookie_filename+".tmp"
        with open(tmp,"w")as f:
            f.write(self.cookie)
        os.replace(tmp,self.cookie_filename)

    def check_cookie(self):
        info=self.get_basicinfo()
        if info.get("code")==0:
            return True
        return False

    def cookie_ok(self):
        """返回 True/False/None：None 表示这次没查出来（网络问题），不要误判成失效"""
        if not self.cookie:
            return False
        try:
            info=self.get_basicinfo()
        except Exception:
            return None
        if not isinstance(info,dict):
            return None
        return info.get("code")==0

    def cookie_invalid(self,interval=300):
        """限速版 cookie 检查：最多每 interval 秒真的查一次服务端"""
        now=time.time()
        if now-self._cookie_checked_at<interval:
            return False
        self._cookie_checked_at=now
        return self.cookie_ok() is False
    
    def setAuthorization(self,res):
        if res.headers.get("Set-Auth") is not None:
            self.Authorization="Bearer "+res.headers.get("Set-Auth")

    def get_basicinfo(self):
        url="https://www.yuketang.cn/api/v3/user/basic-info"
        headers={
            "referer":"https://www.yuketang.cn/web?next=/v2/web/index&type=3",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie":self.cookie
        }
        res=requests.get(url=url,headers=headers,timeout=REQUEST_TIMEOUT).json()
        return res
    
    def getlesson(self):
        """查询正在进行的课。

        返回 True 时 self.lessonId 已经是选中的那一节；
        一个账号可能同时有多节课在进行，这里会把候选都记下来，
        并在 config.json 里配置了 lesson_filter 时按课程名挑选。
        """
        url="https://www.yuketang.cn/api/v3/classroom/on-lesson-upcoming-exam"
        headers={
            "referer":"https://www.yuketang.cn/web?next=/v2/web/index&type=3",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie":self.cookie
        }
        res=requests.get(url=url,headers=headers,timeout=REQUEST_TIMEOUT).json()
        rooms=((res.get("data") or {}).get("onLessonClassrooms")) or []
        rooms=[room for room in rooms
               if isinstance(room,dict) and room.get("lessonId")]
        self.lesson_candidates=rooms
        if not rooms:
            self.lessonId=""
            self._logged_candidates=None
            return False

        chosen=rooms[0]
        if self.lesson_filter:
            for room in rooms:
                name=str(room.get("courseName") or room.get("classroomName") or "")
                if self.lesson_filter in name:
                    chosen=room
                    break
        self.lessonId=chosen["lessonId"]

        # 多节课同时进行时，把候选写进日志，方便判断有没有选错
        key=tuple(str(room.get("lessonId")) for room in rooms)
        if len(rooms)>1 and key!=self._logged_candidates:
            self._logged_candidates=key
            desc="；".join(
                f"{room.get('courseName') or room.get('classroomName') or '未命名'}"
                f"(lessonId={room.get('lessonId')})" for room in rooms)
            self.msgmgr.sendMsg(
                f"发现 {len(rooms)} 节课同时在进行，本次处理 "
                f"{chosen.get('courseName') or chosen.get('classroomName') or '未命名'}：{desc}",
                user=self.name)
            self.dump_lessons(rooms)
        return True

    def dump_lessons(self,rooms):
        """多节课同时进行时，把候选原样落盘，方便之后做更准的选择"""
        path=os.path.join(os.getcwd(),f"debug_lessons_{self.name}.json")
        try:
            with open(path,"w",encoding="utf-8") as f:
                json.dump(rooms,f,ensure_ascii=False,indent=2)
        except Exception:
            pass
    
    def lesson_checkin(self,notify_failure=True):
        """发起签到。

        notify_failure=False 时不在这里发“签到失败”消息，交给上层的
        重试循环统一记录，避免一直重试时把历史记录刷屏。
        """
        url="https://www.yuketang.cn/api/v3/lesson/checkin"
        headers={
            # 官方前端会把 source 一路带进 referer 和请求体，这里保持一致
            "referer":f"https://www.yuketang.cn/lesson/fullscreen/v3/{self.lessonId}?source={self.checkin_source}",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Content-Type":"application/json; charset=utf-8",
            "cookie":self.cookie
        }
        data={
            "source":self.checkin_source,
            "lessonId":self.lessonId
        }
        res=requests.post(url=url,headers=headers,json=data,timeout=REQUEST_TIMEOUT)
        self.setAuthorization(res)
        # 原来这里直接取 res.json()['data']['lessonToken']，失败就抛 KeyError，
        # 而界面上只留下"已发起签到"一句，看不出到底签到成没成。现在明确报一句。
        if not api_ok(res):
            reason=api_reason(res)
            if notify_failure:
                self.msgmgr.sendMsg(f"签到失败：{reason}",user=self.name)
            raise RuntimeError(f"签到失败：{reason}")
        try:
            info=res.json()['data']
            self.Auth=info['lessonToken']
            self.userid=info['identityId']
            if self.debug:
                fields="、".join(sorted(str(key) for key in info.keys()))
                print(f"[雨课堂] 签到返回字段: {fields}")
        except Exception as e:
            message="签到失败：响应里没有 lessonToken/identityId"
            if notify_failure:
                self.msgmgr.sendMsg(message,user=self.name)
            raise RuntimeError(message) from e
        self.msgmgr.sendMsg(f"签到成功：已进入课堂，签到方式 {self.checkin_source_desc()}",user=self.name)

    
    def fetch_presentation(self):
        if not self.presentation:
            return False
        url=f"https://www.yuketang.cn/api/v3/lesson/presentation/fetch?presentation_id={self.presentation}"
        headers={
            "referer":f"https://www.yuketang.cn/lesson/fullscreen/v3/{self.lessonId}?source=5",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie":self.cookie,
            "Authorization":self.Authorization
        }
        res=requests.get(url=url,headers=headers,timeout=REQUEST_TIMEOUT)
        self.setAuthorization(res)
        try:
            info=res.json()
            slides=info['data']['slides']    #获得幻灯片列表
        except Exception:
            self.msgmgr.sendMsg(f"获取课件失败：{api_reason(res)}",user=self.name)
            return False
        for slide in slides:
            problem=slide.get("problem")
            if not isinstance(problem,dict):
                continue
            # 这页课件的整页图片（带 token 的签名地址），题目里有图时能派上用场
            cover=""
            for field in ("cover","coverAlt","thumbnail"):
                if isinstance(slide.get(field),str) and slide[field].strip():
                    cover=slide[field].strip()
                    break
            # 服务端在 unlockproblem 里下发的是 problemId，上游只按 slide id 存，
            # 两者不一致时就找不到题目；这里两个 key 都存一份（统一按字符串）。
            for key in (problem.get("problemId"),slide.get("id")):
                if key is not None:
                    self.problems[str(key)]=problem
                    if cover:
                        self.problem_covers[str(key)]=cover
        if slides!=self.slides:
            self.slides=slides
            self.msgmgr.sendMsg("成功获取幻灯片",user=self.name)
        return True
        

    def dump_problem(self,problem,problem_id=None):
        """把题目原始数据落盘，方便判断雨课堂到底下发了哪些字段"""
        problem_id=self.problemid if problem_id is None else problem_id
        path=os.path.join(os.getcwd(),f"debug_problem_{self.lessonId}_{problem_id}.json")
        try:
            with open(path,"w",encoding="utf-8") as f:
                json.dump(problem,f,ensure_ascii=False,indent=2)
        except Exception:
            return ""
        keys="、".join(sorted(str(key) for key in problem.keys()))
        print(f"[雨课堂] 题目原始字段: {keys}")
        return path

    def download_image(self,url,max_bytes=4*1024*1024,deadline=None,
                       total_timeout=IMAGE_DOWNLOAD_TIMEOUT):
        """下载题目 / 课件里的图片，返回 data URL；失败 / 超时返回空串。

        requests 的 timeout 只约束单次 socket 读写：图片源如果隔一会儿吐一点
        数据，单张图能拖好几分钟，最后把整道题的大模型请求也拖到超时。这里
        改成流式下载，同时限制总时长和体积，超了就放弃这张图。
        """
        if deadline is not None and time.time()>=deadline:
            return ""
        if url.startswith("//"):
            url="https:"+url
        elif url.startswith("/"):
            url="https://www.yuketang.cn"+url
        if not url.lower().startswith("http"):
            return ""
        headers={
            "referer":f"https://www.yuketang.cn/lesson/fullscreen/v3/{self.lessonId}?source={self.checkin_source}",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie":self.cookie
        }
        started=time.monotonic()
        try:
            res=requests.get(url,headers=headers,timeout=(5,10),stream=True)
        except Exception as e:
            print(f"[雨课堂] 图片下载失败：{type(e).__name__}: {e}")
            return ""
        try:
            if res.status_code>=400:
                print(f"[雨课堂] 图片下载失败：HTTP {res.status_code}")
                return ""
            length=res.headers.get("Content-Length")
            try:
                if length is not None and int(length)>max_bytes:
                    print(f"[雨课堂] 图片太大（{int(length)//1024} KB），已跳过")
                    return ""
            except (TypeError,ValueError):
                pass
            data=bytearray()
            for chunk in res.iter_content(64*1024):
                # 先查总时长再处理数据：空 keep-alive 块也要算进预算
                if time.monotonic()-started>total_timeout:
                    print(f"[雨课堂] 图片下载超时（超过 {total_timeout} 秒，"
                          f"已下载 {len(data)//1024} KB），已跳过")
                    return ""
                if deadline is not None and time.time()>=deadline:
                    print(f"[雨课堂] 图片下载预算已用完，停止下载")
                    return ""
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data)>max_bytes:
                    print(f"[雨课堂] 图片太大（超过 {max_bytes//1024} KB），已跳过")
                    return ""
            if not data:
                print(f"[雨课堂] 图片下载失败：响应为空")
                return ""
            mime=(res.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not mime.startswith("image/"):
                mime=_guess_image_mime(bytes(data))
            return f"data:{mime};base64,{base64.b64encode(bytes(data)).decode()}"
        finally:
            res.close()

    def collect_images(self,problem,problem_id,limit=3,max_bytes=4*1024*1024,deadline=None):
        """题目会带图片时，把题干里的图片和这页课件的整页图一起准备给模型。

        整道题的图片下载共用一个时间预算：宁可少带一张图，也不能因为图片源
        太慢把大模型请求拖到超时（11-03 早上 aa 账号的答题失败就是这么来的）。
        """
        if not self.answer_send_images:
            return []
        urls=extract_image_urls(problem)
        cover=self.problem_covers.get(str(problem_id))
        if cover and cover not in urls:
            urls.append(cover)
        if not urls:
            return []
        started=time.time()
        download_deadline=started+IMAGE_DOWNLOAD_BUDGET
        if deadline is not None:
            download_deadline=min(download_deadline,deadline)
        images=[]
        for url in urls[:limit]:
            if self._answer_cancelled:
                break
            if time.time()>=download_deadline:
                print(f"[雨课堂] 图片准备超时（超过 {IMAGE_DOWNLOAD_BUDGET} 秒），"
                      f"题目 {problem_id} 后续图片已跳过")
                break
            remaining=max(1.0,download_deadline-time.time())
            data=self.download_image(url,max_bytes,deadline=download_deadline,
                                     total_timeout=min(IMAGE_DOWNLOAD_TIMEOUT,remaining))
            if data:
                images.append(data)
        elapsed=time.time()-started
        if elapsed>=2:
            print(f"[雨课堂] 题目 {problem_id} 图片准备耗时 {elapsed:.1f} 秒"
                  f"（{len(images)}/{len(urls[:limit])} 张）")
        return images

    def _compute_answer_deadline(self,problem,received_at=None):
        """根据题目下发的 limit / sendTime 算作答截止时间。

        limit 是作答时限（秒；有些接口给毫秒，会按 1 小时阈值自动换算），
        0 表示没有时限。sendTime 可能是秒或毫秒，
        为 0 / 明显异常时退回本题的接收时间。
        """
        if not isinstance(problem,dict):
            return None
        try:
            limit=float(problem.get("limit") or 0)
        except (TypeError,ValueError):
            return None
        if limit<=0:
            return None
        try:
            sent=float(problem.get("sendTime") or 0)
        except (TypeError,ValueError):
            sent=0.0
        send_is_ms=sent>1e12
        if send_is_ms:
            sent/=1000.0
        # 有些版本给的是毫秒（如 60000 = 60 秒）；课堂随堂题一般不超过 1 小时
        if limit>3600 or (send_is_ms and limit>=1000):
            limit/=1000.0
        now=time.time() if received_at is None else received_at
        if sent<=0 or sent>now+60:  # 没有 sendTime 或明显异常，按收到题目的时间算
            sent=now
        return sent+limit

    def _answer_expired(self,problem_id=None,deadline=None):
        """题目是否已经不能再作答（课程结束也算）。"""
        if self._answer_cancelled:
            return True
        if deadline is None and problem_id is not None:
            deadline=self._problem_deadlines.get(str(problem_id))
        return deadline is not None and time.time()>=deadline

    def _cancel_answer_tasks(self):
        """课程结束时取消所有还没开始（或还在等延迟）的作答任务。"""
        self._answer_cancelled=True
        pending=[task for task in self._answer_tasks if not task.done()]
        for task in pending:
            task.cancel()
        self._answer_tasks.clear()
        if pending:
            print(f"[雨课堂] 课程结束，取消 {len(pending)} 个未完成的答题任务")

    def answer(self,problem_id=None):
        problem_id=self.problemid if problem_id is None else problem_id
        # 用 get 而不是 []：本地没有这道题（比如课件还没同步过来）时，
        # 原来会抛 KeyError 把整个监听打断，现在记一笔明确的失败。
        problem=self.problems.get(str(problem_id))
        if problem is None:
            self.msgmgr.sendMsg(f"答题失败：本地没有题目 {problem_id} 的数据（课件可能还没同步）",user=self.name)
            return

        deadline=self._problem_deadlines.get(str(problem_id))
        if self._answer_cancelled:
            print(f"[雨课堂] 课程已结束，跳过题目 {problem_id} 的作答")
            return
        if self._answer_expired(problem_id,deadline):
            self.msgmgr.sendMsg(f"跳过作答：答题时间已结束（题目 {problem_id}）",user=self.name)
            return

        problem_type=int(problem.get("problemType") or 0)
        type_name=PROBLEM_TYPE_NAMES.get(problem_type,f"题型{problem_type}")
        self.answer_note=""

        # 先看雨课堂有没有下发答案；没有就按配置决定要不要交给大模型
        answers=extract_server_answers(problem)
        source="雨课堂下发"
        if answers is None and self.answer_source in ("ai","auto"):
            if self._answer_cancelled:
                print(f"[雨课堂] 课程已结束，跳过题目 {problem_id} 的作答")
                return
            if self._answer_expired(problem_id,deadline):
                self.msgmgr.sendMsg(
                    f"跳过作答：答题时间已结束，未调用大模型（题目 {problem_id}）",
                    user=self.name)
                return
            images=self.collect_images(problem,problem_id,deadline=deadline)
            if self._answer_cancelled:
                print(f"[雨课堂] 课程已结束，跳过题目 {problem_id} 的作答")
                return
            if self._answer_expired(problem_id,deadline):
                self.msgmgr.sendMsg(
                    f"跳过作答：答题时间已结束，未调用大模型（题目 {problem_id}）",
                    user=self.name)
                return
            if images:
                print(f"[雨课堂] 题目 {problem_id} 带 {len(images)} 张图片，一起交给模型识别")
            effort=self.answer_reasoning_effort or "默认"
            print(f"[雨课堂] 题目 {problem_id} 请求大模型（推理强度={effort}，"
                  f"超时={self.answer_timeout} 秒）")
            started=time.time()
            answers,note=ai_solve_problem(problem,self.answer_api_key,self.answer_base_url,
                                          self.answer_model,self.answer_timeout,
                                          images=images,deadline=deadline,
                                          reasoning_effort=self.answer_reasoning_effort)
            elapsed=time.time()-started
            print(f"[雨课堂] 题目 {problem_id} 大模型作答耗时 {elapsed:.1f} 秒")
            if answers is None:
                self.answer_note=f"{note}（耗时 {elapsed:.1f} 秒）"
            else:
                source=f"{note}，耗时 {elapsed:.1f} 秒"

        if self._answer_cancelled:
            print(f"[雨课堂] 课程已结束，跳过题目 {problem_id} 的作答")
            return
        if self._answer_expired(problem_id,deadline):
            self.msgmgr.sendMsg(f"跳过作答：答题时间已结束（题目 {problem_id}）",user=self.name)
            return

        result=normalize_result(problem,answers)
        if result is None:
            # 关键修复：拿不到有效答案就明确说“跳过作答”，
            # 不再把空答案当成作答成功提交上去。
            if answers is None:
                reason=self.answer_note or "雨课堂没有下发这道题的答案"
                extra=""
            else:
                # 把模型原样给的答案也记下来，方便回头查是哪一步对不上
                reason="答案解析不出有效内容"
                extra=f"，拿到的答案为 {answers!r}"
            detail=f"题型={type_name}{extra}"
            saved=self.dump_problem(problem,problem_id)
            if saved:
                detail+=f"，原始数据已存 {os.path.basename(saved)}"
            self.msgmgr.sendMsg(f"跳过作答：{reason}（{detail}）",user=self.name)
            if not self.answer_api_key and self.answer_source in ("ai","auto") and not self._hint_ai_shown:
                self._hint_ai_shown=True
                self.msgmgr.sendMsg(
                    "提示：雨课堂已不再下发课内题答案，想自动作答请在 config.json 的 "
                    "answer.api_key 里填上大模型 API Key（默认用阿里云百炼）。",
                    user=self.name)
            return

        # 有些答案对不上任何选项（模型写错 / 服务端格式变了），提示一下
        dropped=0
        if isinstance(answers,(list,tuple)) and problem_type not in (4,5):
            dropped=len([item for item in answers if _has_content(item)])-len(result)

        url="https://www.yuketang.cn/api/v3/lesson/problem/answer"
        headers={
            "referer":f"https://www.yuketang.cn/lesson/fullscreen/v3/{self.lessonId}?source=5",
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie":self.cookie,
            "Content-Type":"application/json",
            "Authorization":self.Authorization
        }
        data={
             "dt":int(time.time()*1000),
             "problemId":problem_id,
            "problemType":problem_type,
            "result":result
        }
        try:
            res=requests.post(url=url,headers=headers,json=data,timeout=REQUEST_TIMEOUT)
        except Exception as e:
            self.msgmgr.sendMsg(
                f"答题失败：请求出错 {type(e).__name__}: {e}（题型={type_name} 答案={result}）",
                user=self.name)
            return
        self.setAuthorization(res)
        # 成败都报，并保留题型 / 答案 / 来源，方便复盘
        detail=f"题型={type_name} 答案={result}（来源={source}）"
        if dropped>0:
            detail+=f"（有 {dropped} 个答案对不上选项，已忽略）"
        if api_ok(res):
            self.msgmgr.sendMsg(f"答题成功：{detail}",user=self.name)
        else:
            self.msgmgr.sendMsg(f"答题失败：{api_reason(res)}（{detail}）",user=self.name)




    def ws_controller(self,func,loop=None):
        if loop is None:
            loop=asyncio.get_event_loop()
        # 去掉 start_time<6000 的 100 分钟总时长上限：一直重试，直到正常结束
        # （func 正常返回，比如课程结束）或进程被手动关闭。
        failures=0
        while True:
            try:
                loop.run_until_complete(func())
                break
            except KeyboardInterrupt:
                # 裸 except 会把 Ctrl+C 一起吞掉，这里单独放过去
                raise
            except Exception:
                print(traceback.format_exc())
                failures+=1
                # 连续失败时顺手验一次 cookie：登录态过期就没必要一直重连了
                if self.cookie and failures%3==0 and self.cookie_ok() is False:
                    raise LoginExpired("cookie 已失效")
                delay=min(RETRY_INTERVAL*failures,60)     # 退避，最多 60 秒一次
                print(f"出现异常，{delay} 秒后重试（第 {failures} 次，不手动关闭会一直重试）")
                time.sleep(delay)


    async def ws_login(self):
        uri = "wss://www.yuketang.cn/wsapp/"
        async with websockets.connect(uri) as websocket:
            # 发送 "hello" 消息以建立连接
            hello_message = {
                "op":"requestlogin",
                "role":"web",
                "version":1.4,
                "type":"qrcode",
                "from":"web"
            }
            await websocket.send(json.dumps(hello_message))

            server_response = await recv_json(websocket)
            qrcode_url=server_response['ticket']

            download_qrcode(qrcode_url,self.name)
            get_token()
            send_image(upload_file(f"qrcode_{self.name}.jpg"),self.name)

            server_response = await asyncio.wait_for(recv_json(websocket),timeout=120)
            self.weblogin(server_response['UserID'],server_response['Auth'])



    
    async def ws_lesson(self):
        uri = "wss://www.yuketang.cn/wsapp/"
        loop=asyncio.get_event_loop()
        self._answer_cancelled=False
        async with websockets.connect(uri,ping_interval=20,ping_timeout=20) as websocket:
            # 发送 "hello" 消息以建立连接
            hello_message = {
                "op": "hello",
                "userid": self.userid,
                "role": "student",
                "auth": self.Auth,
                "lessonid": self.lessonId
            }
            flag=1
            await websocket.send(json.dumps(hello_message))

            # 同样去掉 100 分钟上限：本节内一直听，直到 lessonfinished 或连接断开
            while True:
                try:
                    server_response = await asyncio.wait_for(recv_json(websocket),
                                                             timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    # 长时间没有事件时打印一行心跳，界面据此判断进程没有卡死
                    print(f"[雨课堂] 心跳：连接正常，正在等课程事件（{time.strftime('%H:%M:%S')}）")
                    continue
                op=server_response.get('op')
                if op=="showpresentation" or op=="presentationupdated" or op=="presentationcreated":
                    if server_response.get('presentation'):
                        self.presentation=server_response['presentation']
                        # 取课件是阻塞请求，丢到线程池里，别按住事件循环；
                        # 取失败只记一笔，不要因为一次网络抖动把整节课断掉
                        try:
                            await loop.run_in_executor(None,self.fetch_presentation)
                        except Exception as e:
                            self.msgmgr.sendMsg(f"获取课件失败：{type(e).__name__}: {e}",user=self.name)
                        flag=0
                elif op=="slidenav" and flag==1:
                    slide=server_response.get('slide') or {}
                    if slide.get('pres'):
                        self.presentation=slide['pres']
                        try:
                            await loop.run_in_executor(None,self.fetch_presentation)
                        except Exception as e:
                            self.msgmgr.sendMsg(f"获取课件失败：{type(e).__name__}: {e}",user=self.name)
                        flag=0
                elif op=="unlockproblem":
                    problem=server_response.get('problem') or {}
                    if problem.get('prob'):
                        self.problemid=problem['prob']
                        now=time.time()
                        deadline=self._compute_answer_deadline(problem,now)
                        if deadline is None:
                            stored=self.problems.get(str(self.problemid)) or {}
                            deadline=self._compute_answer_deadline(stored,now)
                        self._problem_deadlines[str(self.problemid)]=deadline
                        if deadline is not None:
                            print(f"[雨课堂] 题目 {self.problemid} 作答时间还剩 "
                                  f"{max(int(deadline-now),0)} 秒")
                        task=asyncio.ensure_future(self.delayed_answer(self.problemid))
                        self._answer_tasks.add(task)
                        task.add_done_callback(self._answer_tasks.discard)
                elif op=="lessonfinished":
                    self.msgmgr.sendMsg("课程结束了",user=self.name)
                    self._cancel_answer_tasks()
                    self._problem_deadlines.clear()
                    break

    async def delayed_answer(self,problem_id):
        """等一小会儿再提交答案。

        上游原本用阻塞式 time.sleep 等这段延迟：同步阻塞会把整个 websocket
        事件循环按住，服务端心跳超时后就把连接踢掉（日志里那些
        keepalive ping timeout 就是这么来的）。改成 asyncio.sleep，
        再把阻塞的作答请求丢进线程池。

        题目带作答时限（limit）时，到点后直接跳过，不再调用大模型。
        """
        deadline=self._problem_deadlines.get(str(problem_id))
        if self._answer_cancelled:
            print(f"[雨课堂] 课程已结束，跳过题目 {problem_id} 的作答")
            return
        if self._answer_expired(problem_id,deadline):
            self.msgmgr.sendMsg(f"跳过作答：答题时间已结束（题目 {problem_id}）",user=self.name)
            return

        delay=randint(*ANSWER_DELAY_RANGE)
        # 连随机延迟都等不完就没必要开始作答了，直接跳过省 token
        if deadline is not None and time.time()+delay>=deadline:
            self.msgmgr.sendMsg(f"跳过作答：答题时间已结束（题目 {problem_id}）",user=self.name)
            return
        print(f"[雨课堂] {delay} 秒后提交题目 {problem_id} 的答案")
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            print(f"[雨课堂] 课程结束，取消题目 {problem_id} 的作答")
            raise
        if self._answer_cancelled:
            print(f"[雨课堂] 课程已结束，跳过题目 {problem_id} 的作答")
            return
        if self._answer_expired(problem_id,deadline):
            self.msgmgr.sendMsg(f"跳过作答：答题时间已结束（题目 {problem_id}）",user=self.name)
            return
        loop=asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None,self.answer,problem_id)
        except Exception:
            traceback.print_exc()


def ykt_user(name):
    count=0
    ykt=yuketang(name)
    while not ykt.getlesson():
        time.sleep(30)
        count+=1
        if count>200:
            return
    ykt.lesson_checkin()
    ykt.ws_controller(ykt.ws_lesson)
    

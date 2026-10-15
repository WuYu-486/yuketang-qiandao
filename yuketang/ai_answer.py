# -*- coding: utf-8 -*-
"""可选的“AI 作答”模块。

雨课堂已经不再向学生端下发课内题的正确答案（presentation 接口里的
answers 字段是空的），所以本地拿不到答案时，可以把题干和选项交给大模型
生成一份答案。

默认不启用：只有 config.json 里 answer.source 设成 "ai" / "auto"，
并且能找到 API Key（config.json、环境变量或 dashscope_key.txt）时才会
被调用。任何一步失败都只是返回 None，由上层记录成“跳过作答”，
不会打断整个监听。

接口部分是 OpenAI 兼容的 /chat/completions，默认按 DeepSeek 配置，
也可以用 config.json 里的 answer.base_url / answer.model 换成别的服务。
"""

import json
import re

import requests

PROBLEM_TYPE_NAMES = {
    1: "单选题",
    2: "多选题",
    3: "投票题",
    4: "填空题",
    5: "主观题",
    6: "判断题",
}

_TAG_RE = re.compile(r"<[^>]+>")
_JSON_RE = re.compile(r"\{.*\}", re.S)
_IMG_RE = re.compile(r"<img[^>]*src\s*=\s*[\"']?([^\"'\s>]+)", re.I)


def plain_text(value):
    """把带 HTML 标签的题干 / 选项压成一行可读文本"""
    if value is None:
        return ""
    text = str(value)
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def option_items(problem):
    """把题目里的选项整理成 [(key, 文本)]，key 原样保留"""
    items = []
    for index, option in enumerate(problem.get("options") or []):
        if isinstance(option, dict):
            key = option.get("key")
            if key is None:
                key = option.get("Key")
            if key is None:
                key = index
            body = option.get("value")
            if body is None:
                body = option.get("text")
            if body is None:
                body = option.get("content")
            items.append((str(key), plain_text(body)))
        else:
            items.append((str(index), plain_text(option)))
    return items


def blank_count(problem):
    blanks = problem.get("blanks")
    if isinstance(blanks, list) and blanks:
        return len(blanks)
    return 1


def extract_image_urls(problem):
    """找出题干 / 选项里引用的图片地址，按出现顺序去重"""
    texts = [problem.get("body")]
    for option in problem.get("options") or []:
        if isinstance(option, dict):
            texts.append(option.get("value") or option.get("text") or option.get("content"))
        else:
            texts.append(option)

    urls = []
    for text in texts:
        if not text:
            continue
        for match in _IMG_RE.finditer(str(text)):
            url = match.group(1).strip()
            if url:
                urls.append(url)

    # 有的版本把图片单独放在这些字段里
    for field in ("pics", "images", "image", "pic"):
        value = problem.get(field)
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, dict):
                item = (item.get("url") or item.get("src")
                        or item.get("pic") or item.get("value"))
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())

    seen, result = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def build_messages(problem, images=None):
    """拼出给大模型的提示词；images 是已经准备好的图片 data URL 列表"""
    problem_type = int(problem.get("problemType") or 0)
    type_name = PROBLEM_TYPE_NAMES.get(problem_type, "其它题型")

    lines = [f"题型：{type_name}", f"题干：{plain_text(problem.get('body'))}"]
    options = option_items(problem)
    if options:
        lines.append("选项：")
        for key, text in options:
            lines.append(f"  {key}: {text}")

    if problem_type == 4:
        count = blank_count(problem)
        lines += [f"这是一道填空题，一共 {count} 个空。",
                  f'只输出 JSON，answers 里按空序放 {count} 个答案文本：{{"answers": ["第一空", "第二空"]}}',
                  "要求：",
                  "1. answers 的个数必须等于空的个数，按空序排列；",
                  "2. 答案是文本本身，不要写“第1空”这类提示，不要带序号、括号或句号。"]
    elif problem_type == 5:
        lines += ["这是一道主观题，答案写简短、直接。",
                  '只输出 JSON：{"answers": ["你的答案"]}']
    elif options:
        keys_text = "、".join(str(key) for key, _ in options)
        lines += [f"选项标识只有这些：{keys_text}",
                  '只输出 JSON：{"answers": ["选项标识"]}',
                  "要求：",
                  f"1. answers 里只能放上面列出的标识本身（{keys_text}），其它值一律不行；",
                  '2. 不要带选项原文、序号、括号、句号或说明，不要写成 "B."、"（B）"、'
                  '"答案：B"、"B 共模信号" 这类形式；']
        if problem_type in (2, 3):
            lines += ['3. 多选的正确项要全部列出，每项单独一个字符串，'
                      '例如 {"answers": ["B", "C"]}，不要写成 "BC"；',
                      "4. 不确定也要选，不要留空、不要回答“不知道”。"]
        else:
            lines += ['3. 只放一个标识，例如 {"answers": ["B"]}；',
                      "4. 不确定也要选一个最可能的，不要留空、不要回答“不知道”。"]
    else:
        lines += ['只输出 JSON：{"answers": ["你的答案"]}',
                  "不确定也要给出一个最可能的答案，不要留空。"]

    system = ("你是一个正在上课的学生，需要作答雨课堂课内题。"
              "任何情况下都只输出一个 JSON 对象：不要解释、不要复述题目、"
              "不要用 Markdown 代码块，也不要在 JSON 前后加任何文字或标点。")
    text = "\n".join(lines)

    if images:
        # 有图片时用多模态格式：一段文字 + 若干图片
        system += ("题干或选项里的图片已经一并给你，请结合图片内容作答；"
                   "如果选项只出现在图片里，按图片中的选项标识作答。")
        content = [{"type": "text", "text": text}]
        content += [{"type": "image_url", "image_url": {"url": image}} for image in images]
        user_message = {"role": "user", "content": content}
    else:
        user_message = {"role": "user", "content": text}
    return [{"role": "system", "content": system}, user_message]


def parse_json_object(text):
    """从模型输出里抠出 JSON 对象，容忍 ```json 代码块和多余解释"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def solve_problem(problem, api_key, base_url, model, timeout=30, images=None):
    """调用大模型生成答案。

    images 是图片的 data URL 列表（可为空）：带了图片就是多模态请求，
    如果模型不接受图片，会自动退回纯文字再问一次。
    返回 (answers, note)：
      answers 为 None 表示这次没做出答案，note 是给日志用的原因 / 说明。
    """
    if not api_key:
        return None, "没有配置 AI 作答的 API Key"
    if not model:
        return None, "没有配置 AI 作答的模型名"

    url = (base_url or "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def post(with_images, json_mode=True):
        payload = {
            "model": model,
            "messages": build_messages(problem, with_images),
            "temperature": 0.2,
        }
        if json_mode:
            # 让接口层强制输出 JSON，模型就没法在前后加解释
            payload["response_format"] = {"type": "json_object"}
        return requests.post(url, headers=headers, json=payload, timeout=timeout)

    note_suffix = ""
    try:
        res = post(images if images else None)
        if res.status_code >= 400:
            # 有些服务不支持 response_format，去掉这个参数再试一次
            res = post(images if images else None, json_mode=False)
        if res.status_code >= 400 and images:
            # 模型 / 接口不接受图片时退回纯文字，至少别把这道题整个丢掉
            note_suffix = "，图片没被接受已按纯文字作答"
            res = post(None, json_mode=False)
    except Exception as e:
        return None, f"请求大模型失败：{type(e).__name__}: {e}"

    if res.status_code >= 400:
        return None, f"大模型返回 HTTP {res.status_code}：{res.text[:160]}"
    try:
        message = res.json()["choices"][0]["message"]
    except Exception:
        return None, "大模型的响应格式看不懂"
    # 推理型模型偶尔把内容放在 reasoning_content 里
    content = message.get("content") or message.get("reasoning_content") or ""

    data = parse_json_object(content)
    if not isinstance(data, dict):
        return None, "大模型没有返回可用的 JSON"
    answers = data.get("answers")
    if answers is None:
        answers = data.get("answer")
    if answers is None:
        return None, "大模型返回的 JSON 里没有 answers 字段"
    if not isinstance(answers, list):
        answers = [answers]
    return answers, f"AI 作答（{model}{note_suffix}）"

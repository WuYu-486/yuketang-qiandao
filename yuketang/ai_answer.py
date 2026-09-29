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


def build_messages(problem):
    """拼出给大模型的提示词"""
    problem_type = int(problem.get("problemType") or 0)
    type_name = PROBLEM_TYPE_NAMES.get(problem_type, "其它题型")

    lines = [f"题型：{type_name}", f"题干：{plain_text(problem.get('body'))}"]
    options = option_items(problem)
    if options:
        lines.append("选项：")
        for key, text in options:
            lines.append(f"  {key}: {text}")

    if problem_type == 4:
        lines.append(f"这是一道填空题，一共有 {blank_count(problem)} 个空。")
        lines.append('请只输出 JSON：{"answers": ["第一空的答案", "第二空的答案"]}')
    elif problem_type == 5:
        lines.append("这是一道主观题，答案请写得简短、直接。")
        lines.append('请只输出 JSON：{"answers": ["你的答案"]}')
    elif options:
        lines.append("answers 里必须原样照抄上面选项的 key，不要改写、不要只写序号。")
        lines.append('请只输出 JSON：{"answers": ["选项的 key"]}，多选题把正确的 key 都列出来。')
    else:
        lines.append('请只输出 JSON：{"answers": ["你的答案"]}')

    system = ("你是一个正在上课的学生，需要作答雨课堂课内题。"
              "只输出 JSON，不要解释，不要用 Markdown 代码块。")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)}]


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


def solve_problem(problem, api_key, base_url, model, timeout=30):
    """调用大模型生成答案。

    返回 (answers, note)：
      answers 为 None 表示这次没做出答案，note 是给日志用的原因 / 说明。
    """
    if not api_key:
        return None, "没有配置 AI 作答的 API Key"
    if not model:
        return None, "没有配置 AI 作答的模型名"

    url = (base_url or "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": build_messages(problem),
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        return None, f"请求大模型失败：{type(e).__name__}: {e}"

    if res.status_code >= 400:
        return None, f"大模型返回 HTTP {res.status_code}：{res.text[:160]}"
    try:
        content = res.json()["choices"][0]["message"]["content"]
    except Exception:
        return None, "大模型的响应格式看不懂"

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
    return answers, f"AI 作答（{model}）"

# 配置说明

`config.json` 首次运行时自动生成（模板见 `config.example.json`），只保存在本地、不参与提交；改完重启界面生效。

```json
{
  "yuketang": {
    "name": "me",
    "wx": false,
    "debug": true,
    "open_qrcode": true,
    "lesson_filter": "",
    "checkin_source": 1,
    "answer": {
      "source": "auto",
      "api_key": "",
      "api_key_env": "",
      "base_url": "",
      "model": "",
      "send_images": true,
      "reasoning_effort": "high",
      "share_answers": true,
      "timeout": 120
    }
  }
}
```

## 字段

| 字段 | 说明 |
| --- | --- |
| `name` | 默认账号标识，命令行不传 `--name` 时用它 |
| `wx` | 是否把消息推到企业微信，默认 `false` |
| `debug` | 是否输出调试信息，默认 `true` |
| `open_qrcode` | 扫码后是否自动用看图软件打开二维码；图形界面会自动设为 `false` |
| `lesson_filter` | 同时有多节课时，只签到课程名包含该关键词的课；留空就取第一节 |
| `checkin_source` | 签到方式，默认 `1`（微信/扫二维码），见下节 |
| `answer.source` | `server` 只用雨课堂下发的答案 / `ai` 只用大模型 / `auto` 先看雨课堂、拿不到再用大模型 |
| `answer.api_key` | 大模型 API Key；留空则依次找 `api_key_env` → 常见环境变量 → 根目录 `dashscope_key.txt` |
| `answer.api_key_env` | 指定用哪个环境变量取 Key，例如 `DEEPSEEK-API-KEY` |
| `answer.base_url` / `answer.model` | 接口地址与模型名；留空时按 Key 来源自动选（DeepSeek 用 `https://api.deepseek.com/v1` + `deepseek-chat`） |
| `answer.send_images` | 题目带图片时是否一起发给多模态模型，默认 `true` |
| `answer.reasoning_effort` | 推理强度：`low` / `high` / `max`；DeepSeek 默认 `high`（准确率优先），其他服务商留空即可 |
| `answer.share_answers` | 是否允许同一堂课、同一道题只调用一次 AI，多个账号共享结果；默认 `true`，关闭后各账号独立作答 |
| `answer.timeout` | 单次调用大模型的总超时秒数，默认 `120`；题目本身有作答时限时会自动取剩余时间 |

API Key 有三种写法，任选其一：填 `answer.api_key`、放进 `DEEPSEEK-API-KEY` 之类的环境变量、或写进根目录的 `dashscope_key.txt`。找不到 Key 时课内题会跳过作答，其余功能照旧。

## 账号级 AI 开关

除了全局的 `answer.source`，每个账号还有一个独立的 AI 作答开关，保存在 `accounts.json` 的 `ai_answer` 里，例如：

```json
{
  "accounts": ["me", "aa"],
  "ai_answer": {"me": true, "aa": false}
}
```

- 图形界面里选中账号，勾选/取消顶部的「AI 作答」即可，会自动写回 `accounts.json`。
- 关闭后该账号只使用雨课堂下发的答案，拿不到就跳过，不会调用大模型。
- 正在监听的账号切换开关后，下一道题开始生效，不需要停止再重新开始。
- 老版本 `accounts.json` 没有 `ai_answer` 字段时默认开启，保持原来的行为。

## AI 答案共享

`answer.share_answers` 默认开启。多个账号同时监听同一堂课、遇到同一道题时，只会有一个账号真正调用大模型，其他账号等待并复用结果，然后各自提交答案。

- 识别方式：`lessonId + problemId + 题目内容指纹 + 模型/推理强度`，自动完成，不需要配置班级或账号关系。
- 共享缓存保存在项目根目录的 `answer_cache.sqlite3`，不会提交到 git，默认保留 24 小时。
- 抢到计算权的进程持有 SQLite 租约并定时续期；其他进程只等待，不会重复调用 AI。计算进程退出或租约过期后，下一个账号才会接管。
- 提交答案仍然是每个账号各发一次；`LESSON_PROBLEM_FINISHED` 重试、`extendtime` 延长等逻辑不受影响。
- 如果某个账号的「AI 作答」开关关闭，它既不使用共享 AI 答案，也不参与共享计算。
- 想完全回到各账号独立作答，把 `share_answers` 改成 `false`。

## 签到方式

签到方式由签到请求里的 `source` 决定：官方前端会把它从页面 URL 的 `?source=` 读出来，再放进请求体。雨课堂没有公开这个枚举，下面的对照来自社区整理，仅供参考：

| `checkin_source` | 记录出来的签到方式 |
| --- | --- |
| `1` | 微信 / 扫二维码（默认） |
| `5` | 通过公众号"正在上课"提示进入课堂 |
| `14` | PC / Web |
| `21` | 二维码 |
| `22` | 暗号 |
| `23` | APP 点击课堂 |
| `91` | 旁听（观摩） |

想换成别的入口改这个数字即可，程序启动时会打印当前用的值。如果记录出来的方式和预期不符，用手机扫一次老师屏幕上的课堂二维码，在微信里点「···」→ 复制链接，链接里 `source=` 后面的数字就是抄二维码入口要用的值。

## 企业微信推送（可选）

自己建一个企业微信应用，把 `qywxbot/send.py` 顶部的 `AgentId` / `Secret` / `CompanyId` 填好，再把 `wx` 改成 `true`。三个常量还是 `xxx` 时会自动降级为只输出到控制台，不会报错。

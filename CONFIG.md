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
      "timeout": 60
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
| `answer.timeout` | 单次调用大模型的超时秒数，默认 `60` |

API Key 有三种写法，任选其一：填 `answer.api_key`、放进 `DEEPSEEK-API-KEY` 之类的环境变量、或写进根目录的 `dashscope_key.txt`。找不到 Key 时课内题会跳过作答，其余功能照旧。

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

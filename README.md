# 雨课堂助手（自动签到 + 自动答题）

带图形界面的雨课堂自动化小工具：自动等课、自动签到进课堂、实时监听题目并自动作答，支持多账号同时挂机。

> 核心逻辑整理自 [timeflykai/lazytool](https://github.com/timeflykai/lazytool) 的雨课堂部分。仅供个人学习与研究使用，请遵守所在学校的规定，使用风险自负。

## 功能

- **自动等课**：每 30 秒查一次有没有课正在进行，检测到就自动签到进课堂。
- **自动答题**：websocket 实时监听课堂事件，题目下发后随机延迟 10–20 秒作答；答案优先用雨课堂下发的，拿不到时可交给大模型生成。
- **图形界面**：二维码直接显示在窗口里，支持多账号、一键强制换新码、日志与历史记录。
- **掉线看得见**：登录失效会立刻提示并自动弹出新二维码；等课与跟课都有心跳，界面显示「最近事件 X 分钟前」，长时间没有输出会提示可能卡住。
- **可选的微信推送**：配置企业微信后可以把状态和二维码推到手机（默认关闭）。

## 环境要求

- 系统：Windows（图形界面基于系统自带的 tkinter）
- Python：3.8 及以上
- 依赖：`requests`、`websockets`、`pillow`
- 网络：能访问 `www.yuketang.cn`

## 安装

在仓库根目录执行：

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`py` 命令不可用时，换成你本机 Python 的完整路径即可。装好后所有命令都用 `.venv\Scripts\python.exe` 运行，不会污染本机环境。

## 使用

**图形界面（推荐）**：双击 `gui.bat`，选中左侧账号 → 点「刷新登录二维码」→ 手机微信扫码 → 程序自动开始监听，有课就签到、有题就作答。左侧可「添加账号」挂多个人的号，各账号彼此独立。

**命令行**：

| 命令 | 说明 |
| --- | --- |
| `run.bat` | 打开交互菜单 |
| `run.py check` | 环境自检：依赖 + 雨课堂连通性 |
| `run.py login` | 强制扫码登录，换一张新二维码 |
| `run.py status` | 查看登录状态、当前有没有课 |
| `run.py ykt` | 开始监听：等课 → 签到 → 自动作答 |

以上命令都支持 `--name 账号标识`，不同 `name` 使用互相独立的 `cookie_名称`、`qrcode_名称.jpg`。

## 配置

首次运行会生成 `config.json`（模板见 `config.example.json`）：

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

### 签到方式

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

### 答题的答案从哪来

雨课堂已经不再向学生端下发课内题的正确答案（`answers` 字段一直是空的），所以程序改成：

1. 先看雨课堂下发的答案，真有内容才提交；
2. 没有答案时按 `answer.source` 决定是否交给大模型生成；
3. 两边都拿不到就记「跳过作答」，并把题目原始数据存成 `debug_problem_*.json`，绝不提交空答案。

题干或选项里带图片的题目，会把图片和这道题所在课件的整页图一起发给多模态模型（最多 3 张，实测 `deepseek-flash` 可用）；模型不接受图片时会自动退回纯文字，不会因为图片丢掉整道题。不想发图片就把 `answer.send_images` 设为 `false`。

启用 AI 作答只需让程序能找到 API Key，三种写法任选：填 `answer.api_key`、放进 `DEEPSEEK-API-KEY` 之类的环境变量、或写进根目录的 `dashscope_key.txt`。找不到 Key 时只使用雨课堂下发的答案，其余功能照旧。

### 企业微信推送（可选）

自己建一个企业微信应用，把 `qywxbot/send.py` 顶部的 `AgentId` / `Secret` / `CompanyId` 填好，再把 `config.json` 里的 `wx` 改成 `true`。三个常量还是 `xxx` 时会自动降级为只输出到控制台，不会报错。

## 常见问题

- **二维码扫不了或提示过期**：一张二维码约 2 分钟失效，点「刷新登录二维码」换一张，程序自身也会自动换新。
- **界面报「没找到 .venv」**：还没装依赖，按上面的「安装」一节建好虚拟环境即可。
- **双击 gui.bat 没反应**：手动跑 `.venv\Scripts\python.exe gui.py` 看报错；启动失败时根目录会留下 `gui_error.log`。
- **窗口里只有文字没有二维码**：没装 pillow，执行 `.venv\Scripts\python.exe -m pip install pillow` 即可。
- **能帮别人挂账号吗**：可以。每个账号单独添加、各自扫码一次，然后各自点「开始监听」；别关窗口，关窗口会一并停止所有监听。

## 已知限制

- 不配置大模型 API Key 时，课内题只能跳过作答（雨课堂已不再下发答案）。
- 依赖雨课堂接口与 websocket 协议，对方一旦调整就可能失效。
- 图形界面只在 Windows 桌面环境验证过。

## 注意

`cookie_账号`、`qrcode_账号.jpg`、`dashscope_key.txt` 等本地文件包含隐私，已在 `.gitignore` 中排除，不要外传或提交；日志、历史记录、账号列表等运行产物同样只在本地生成。

## 致谢

雨课堂核心逻辑来自 [timeflykai/lazytool](https://github.com/timeflykai/lazytool)，感谢原作者的分享。

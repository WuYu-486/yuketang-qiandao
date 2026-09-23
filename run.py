# -*- coding: utf-8 -*-
"""
雨课堂自动签到 / 自动作答（命令行入口）

用法:
    python run.py                     # 交互菜单
    python run.py check               # 环境自检：依赖 + 网络连通性
    python run.py ykt                 # 等课 -> 自动签到 -> 监听题目并自动作答
    python run.py login               # 强制重新扫码登录，换一张新二维码
    python run.py status              # 查看登录状态、当前有没有课
    python run.py ykt --name 张三      # 多账号：每个 name 各存一份 cookie

图形界面见 gui.py（双击 gui.bat），界面上的按钮最终调用的就是这里的命令。
"""

import argparse
import asyncio
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")

_YKT_DIR = os.path.join(ROOT, "yuketang")
if os.path.isdir(_YKT_DIR) and _YKT_DIR not in sys.path:
    sys.path.insert(0, _YKT_DIR)

DEFAULT_CONFIG = {
    "yuketang": {
        "name": "me",
        "wx": False,
        "debug": True,
        "open_qrcode": True,
    },
}

YKT_API = "https://www.yuketang.cn/api/v3"
YKT_HEADERS_BASE = {
    "referer": "https://www.yuketang.cn/web",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"),
}

NET_TARGETS = [
    ("雨课堂主站", "https://www.yuketang.cn/web"),
    ("雨课堂接口", "https://www.yuketang.cn/api/v3/user/basic-info"),
]


# ---------------------------------------------------------------- 配置

def deep_merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    """读取 config.json，缺失时按模板生成一份"""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[配置] 已生成模板: {CONFIG_PATH}")
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except Exception as e:
        print(f"[配置] config.json 解析失败: {e}")
        return dict(DEFAULT_CONFIG)
    return deep_merge(DEFAULT_CONFIG, user_cfg)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def filled(value):
    return value not in (None, "", [], {})


# ---------------------------------------------------------------- 自检

def cmd_check(cfg):
    print("=" * 60)
    print("环境自检")
    print("=" * 60)
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"项目目录    : {ROOT}")

    ok = True
    for mod, hint in (
        ("requests", "pip install requests"),
        ("websockets", "pip install websockets"),
        ("PIL", "pip install pillow（只看命令行日志可以不装）"),
    ):
        try:
            __import__(mod)
            print(f"依赖 {mod:<11}: OK")
        except Exception as e:
            if mod == "PIL":
                print(f"依赖 {mod:<11}: 缺失（图形界面显示二维码需要它）")
            else:
                ok = False
                print(f"依赖 {mod:<11}: 缺失 ({e}) -> {hint}")

    print("-" * 60)
    print("网络连通性（8 秒超时）")
    try:
        import requests
    except Exception:
        requests = None
    for label, url in NET_TARGETS:
        if requests is None:
            print(f"  {label:<12}: 跳过（requests 缺失）")
            continue
        try:
            res = requests.get(url, timeout=8)
            print(f"  {label:<12}: HTTP {res.status_code}")
        except Exception as e:
            ok = False
            print(f"  {label:<12}: 失败 {type(e).__name__}: {e}")

    print("-" * 60)
    ykt = cfg["yuketang"]
    name = ykt["name"] or "me"
    cookie_path = os.path.join(ROOT, f"cookie_{name}")
    print(f"默认账号标识: {name}")
    print(f"登录状态文件: {cookie_path}")
    print(f"企业微信推送: {'开启' if ykt['wx'] else '关闭'}")
    print(f"扫码后自动开图: {'是' if ykt['open_qrcode'] else '否（图形界面会显示在窗口里）'}")
    print("=" * 60)
    print("自检完成" if ok else "自检发现问题，见上面标红的项")
    return 0 if ok else 1


# ---------------------------------------------------------------- 登录状态

def cmd_status(cfg, name=None):
    """查看 cookie 是否还有效、当前有没有课在进行"""
    import requests

    name = name or cfg["yuketang"]["name"] or "me"
    cookie_path = os.path.join(ROOT, f"cookie_{name}")
    if not os.path.exists(cookie_path):
        print(f"[状态] 没找到 {cookie_path}，说明还没登录过")
        return 1

    with open(cookie_path, "r", encoding="utf-8") as f:
        cookie = f.read().strip()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(cookie_path)))
    print(f"[状态] cookie 文件: {cookie_path}（{len(cookie)} 字节，最后修改 {stamp}）")

    headers = dict(YKT_HEADERS_BASE, cookie=cookie)
    try:
        info = requests.get(f"{YKT_API}/user/basic-info", headers=headers, timeout=15).json()
    except Exception as e:
        print(f"[状态] 请求失败: {type(e).__name__}: {e}")
        return 1

    ok = info.get("code") == 0
    if ok:
        data = info.get("data") or {}
        who = data.get("name") or data.get("userName") or data.get("user_name") or "未知"
        print(f"[状态] 登录有效，当前账号: {who}")
        if data.get("schoolName"):
            print(f"[状态] 学校: {data['schoolName']}")
    else:
        print("[状态] cookie 已失效，需要重新扫码")
        print(f"[状态] 服务端返回: {json.dumps(info, ensure_ascii=False)[:200]}")

    try:
        lesson = requests.get(f"{YKT_API}/classroom/on-lesson-upcoming-exam",
                              headers=headers, timeout=15).json()
        rooms = ((lesson.get("data") or {}).get("onLessonClassrooms")) or []
        if rooms:
            for room in rooms:
                print(f"[状态] 正在上课: lessonId={room.get('lessonId')} "
                      f"课程={room.get('courseName') or room.get('classroomName') or '未知'}")
        else:
            print("[状态] 当前没有进行中的课")
    except Exception as e:
        print(f"[状态] 课程查询失败: {type(e).__name__}: {e}")
    return 0 if ok else 1


# ---------------------------------------------------------------- 企业微信（可选）

def befriend_wecom(ykmod, cfg_ykt):
    """不用企业微信时，把推送和上传打桩成本地动作，避免登录流程依赖外部配置"""
    if cfg_ykt["wx"]:
        return

    def show_qrcode(media_id, user):
        path = os.path.join(os.getcwd(), f"qrcode_{user}.jpg")
        print(f"[二维码] 登录二维码已保存: {path}")
        if cfg_ykt["open_qrcode"]:
            try:
                os.startfile(path)      # 仅 Windows，自动弹图方便扫码
            except Exception as e:
                print(f"[二维码] 自动打开失败（手动打开上面的路径即可）: {e}")

    ykmod.get_token = lambda: None
    ykmod.upload_file = lambda path: os.path.abspath(path)
    ykmod.send_image = show_qrcode


def wecom_configured():
    send_path = os.path.join(ROOT, "qywxbot", "send.py")
    try:
        with open(send_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("AgentId", "Secret", "CompanyId")):
            value = stripped.split("=", 1)[-1].strip().strip("'\"")
            if not value or set(value) == {"x"}:
                return False
    return True


# ---------------------------------------------------------------- 扫码登录

def cmd_login(cfg, name=None):
    """强制走一次微信扫码登录：不看本地 cookie 是否有效，直接拿一张新二维码。

    上游把 cookie 校验写在 __init__ 里，所以这里用 __new__ 跳过构造函数，
    手动补齐 ws_login 需要的属性，其余逻辑完全复用上游，不改上游代码。
    扫码成功才会覆盖 cookie_<name>，中途放弃不会破坏原来的登录态。
    """
    import yuketang as ykmod

    cfg_ykt = cfg["yuketang"]
    name = name or cfg_ykt["name"] or "me"
    befriend_wecom(ykmod, {**cfg_ykt, "wx": False, "open_qrcode": False})

    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass

    obj = ykmod.yuketang.__new__(ykmod.yuketang)
    obj.name = name
    obj.cookie_filename = f"cookie_{name}"
    obj.cookie = ""
    obj.msgmgr = ykmod.MsgManager(debug=bool(cfg_ykt["debug"]), wx=False)
    obj.start_time = time.time()

    print(f"[登录] 开始扫码登录 name={name}（二维码约 2 分钟失效，过期会自动换新）")
    obj.ws_controller(obj.ws_login)

    cookie_path = os.path.abspath(obj.cookie_filename)
    if os.path.exists(obj.cookie_filename) and os.path.getsize(obj.cookie_filename) > 0:
        print(f"[登录] 登录成功，cookie 已保存到 {cookie_path}")
        return 0
    print("[登录] 没有拿到 cookie，登录未完成")
    return 1


# ---------------------------------------------------------------- 监听与自动答题

def cmd_ykt(cfg, name=None):
    import yuketang as ykmod

    cfg_ykt = cfg["yuketang"]
    name = name or cfg_ykt["name"] or "me"
    wx = bool(cfg_ykt["wx"])
    if wx and not wecom_configured():
        print("[雨课堂] 配置里开了 wx，但 qywxbot/send.py 里 AgentId/Secret/CompanyId 还是占位符")
        print("[雨课堂] 本次改为关闭企业微信推送，只输出到控制台")
        wx = False
    befriend_wecom(ykmod, {**cfg_ykt, "wx": wx})

    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass

    cookie_path = os.path.join(os.getcwd(), "cookie_" + name)
    print(f"[雨课堂] 启动 name={name} 推送={'企业微信' if wx else '控制台'} cookie={cookie_path}")
    ykt = ykmod.yuketang(name)
    ykt.msgmgr.debug = bool(cfg_ykt["debug"])
    ykt.msgmgr.wx = wx

    print("[雨课堂] 等待有课进行中（每 30 秒查一次，不会自动退出）...")
    waited = 0
    # 常驻监听：去掉原来 count>200（约 100 分钟）的等课超时，
    # 外层再套一层循环，一节课结束后回去等下一节课，直到手动关闭。
    while True:
        try:
            while not ykt.getlesson():
                time.sleep(30)
                waited += 30
                if waited % 600 == 0:      # 每 10 分钟报一次，免得看起来像卡死
                    print(f"[雨课堂] 仍在等课（已等 {waited // 60} 分钟），"
                          f"不手动关闭就一直等")
            print(f"[雨课堂] 检测到课程 lessonId={ykt.lessonId}，开始签到")
            waited = 0
            ykt.lesson_checkin()
            ykt.ws_controller(ykt.ws_lesson)
        except KeyboardInterrupt:
            raise
        except Exception:
            # getlesson()/lesson_checkin() 上游都没设超时也没容错，网络抖一下
            # 原来会直接结束进程；改成常驻后这里兜住，睡 30 秒继续等课
            traceback.print_exc()
            print("[雨课堂] 本轮出错，30 秒后重试（不会退出）")
            time.sleep(30)
            continue
        print("[雨课堂] 本节监听结束，继续等下一节课 ..."
              "（Ctrl+C 或界面“停止”才会退出）")


# ---------------------------------------------------------------- 菜单

def menu(cfg):
    while True:
        print()
        print("=" * 60)
        print("雨课堂助手")
        print("=" * 60)
        print("  1. 开始监听（等课 + 自动签到 + 自动答题）")
        print("  2. 重新扫码登录（强制换一张新二维码）")
        print("  3. 查看登录状态 / 当前有没有课")
        print("  4. 环境自检")
        print("  0. 退出")
        choice = input("请选择 > ").strip()
        try:
            if choice == "1":
                cmd_ykt(cfg)
            elif choice == "2":
                cmd_login(cfg)
            elif choice == "3":
                cmd_status(cfg)
            elif choice == "4":
                cmd_check(cfg)
            elif choice == "0":
                return 0
            else:
                print("无效选项")
        except KeyboardInterrupt:
            print("\n已中断当前任务")
        except Exception:
            print("执行出错：")
            traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="雨课堂助手")
    parser.add_argument("action", nargs="?",
                        choices=["check", "ykt", "login", "status"],
                        help="check=自检 / ykt=开始监听 / login=重新扫码 / status=登录状态，留空打开菜单")
    parser.add_argument("--name", help="账号标识，用来区分不同账号的 cookie 文件")
    args = parser.parse_args()

    cfg = load_config()
    try:
        if args.action == "check":
            return cmd_check(cfg)
        if args.action == "ykt":
            return cmd_ykt(cfg, name=args.name)
        if args.action == "login":
            return cmd_login(cfg, name=args.name)
        if args.action == "status":
            return cmd_status(cfg, name=args.name)
        return menu(cfg)
    except (KeyboardInterrupt, EOFError):
        # 常驻监听之后，Ctrl+C 就是正常的手动关闭方式，别甩一堆 traceback
        print("\n已退出")
        return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())

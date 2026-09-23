# -*- coding: utf-8 -*-
"""
lazytool 雨课堂图形界面（tkinter + 标准库，可选 Pillow 用于显示二维码）

启动:
    gui.bat                                      双击启动（无控制台窗口）
    .venv\\Scripts\\python.exe gui.py             带控制台启动，方便看报错
    .venv\\Scripts\\python.exe gui.py --selftest  只构建界面做自检，不进事件循环

界面本身不碰网络，所有真实动作都交给 run.py 子进程执行，
这样停止监听只需要结束子进程，也不会因为界面卡住影响登录。
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    TK_OK = True
except Exception:
    TK_OK = False

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

ACCOUNTS_PATH = os.path.join(ROOT, "accounts.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
PY = sys.executable
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
MONO = ("Consolas", 9)

QR_TTL = 115             # 服务端二维码 120 秒失效，这里留几秒余量
QR_SIZE = 260
MAX_KEEP_LINES = 800

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def qr_file_ok(path):
    """二维码文件是否真的能扫：存在、够大、且确实是张完整的图。

    上游 download_qrcode 在 ticket 失效时会写出 0 字节文件并照常返回，
    只判断 os.path.exists 会把它当成可用二维码，点开自然是一张打不开的图。
    """
    try:
        if os.path.getsize(path) < 1024:    # 正常二维码约 39KB，几百字节的必是坏图
            return False
    except OSError:
        return False
    if not HAS_PIL:
        return True                          # 没有 PIL 只能退化成按大小判断
    try:
        with Image.open(path) as im:
            im.verify()                      # 校验完整性，不解码像素
        return True
    except Exception:
        return False


HISTORY_RULES = (
    ("登录成功", "扫码登录成功，cookie 已更新"),
    ("正在第一次获取登录", "需要微信扫码登录"),
    ("cookie有效", "登录状态：cookie 有效"),
    ("cookie 已失效", "登录状态：cookie 已失效，需要重新扫码"),
    ("cookie已失效", "登录状态：cookie 已失效，需要重新扫码"),
    ("检测到课程", "检测到正在上课，已发起签到"),
    ("课程结束了", "本节课程结束"),
    ("成功获取幻灯片", "获取到课件幻灯片"),
    ("出现异常", "运行异常，程序自动重试中"),
)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def run_capture(args, timeout=90):
    """跑一条命令并把输出全部拿回来，用于自检和登录状态查询"""
    try:
        res = subprocess.run(
            [PY, "-u"] + args, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return (res.stdout or "") + (res.stderr or "")
    except Exception as e:
        return f"[命令执行失败] {type(e).__name__}: {e}"


class Account:
    def __init__(self, name):
        self.name = name
        self.proc = None
        self.session = 0          # 每启动一次任务就 +1，用来丢弃被替换掉的旧进程输出
        self.mode = None          # listen / login
        self.auto_start = False   # 扫码登录成功后自动开始监听
        self.lines = []
        self.log_handle = None
        self.qr_mtime = None
        self.qr_accept_after = float("inf")   # 只认这之后生成的二维码，忽略历史遗留文件
        self.qr_photo = None
        self.qr_state = "empty"   # empty / waiting / fresh / broken / used
        self.start_at = None
        self.login_ok = None
        self.login_name = None
        self.lesson = None


class App:
    def __init__(self, root):
        self.root = root
        self.accounts = {}
        self.queue = queue.Queue()
        self.status_checked = {}
        self.cfg = self._prepare_config()

        root.title("lazytool · 雨课堂助手")
        root.geometry("1060x700")
        root.minsize(960, 640)
        self._style()
        self._build()
        self._load_accounts()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(150, self._drain)
        root.after(1000, self._tick)

    # -------------------------------------------------------- 初始化

    def _prepare_config(self):
        """让 run.py 的开关符合界面预期：界面自己显示二维码，所以不弹看图软件"""
        cfg = load_json(CONFIG_PATH, {})
        ykt = cfg.setdefault("yuketang", {})
        ykt.setdefault("name", "me")
        ykt["wx"] = False            # 不用企业微信推送
        ykt["debug"] = True          # 日志回显到界面
        ykt["open_qrcode"] = False   # 二维码显示在窗口里
        save_json(CONFIG_PATH, cfg)
        return cfg

    def _style(self):
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except Exception:
                continue
        style.configure(".", font=FONT)
        self.root.option_add("*Font", FONT)

    def _build(self):
        self.status = ttk.Label(self.root, text="就绪", anchor="w", padding=(12, 5))
        self.status.pack(side="bottom", fill="x")

        main = ttk.Frame(self.root, padding=(10, 8))
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, width=220)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        ttk.Label(left, text="账号列表", font=FONT_B).pack(anchor="w")
        self.tree = ttk.Treeview(left, columns=("state",), show="tree headings",
                                 height=14, selectmode="browse")
        self.tree.heading("#0", text="账号")
        self.tree.heading("state", text="状态")
        self.tree.column("#0", width=110, anchor="w")
        self.tree.column("state", width=90, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(4, 6))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Button(left, text="添加账号", command=self._add_account).pack(fill="x", pady=1)
        ttk.Button(left, text="移除账号", command=self._remove_account).pack(fill="x", pady=1)
        ttk.Button(left, text="环境自检", command=self._selfcheck).pack(fill="x", pady=(8, 1))

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        card = ttk.LabelFrame(right, text="当前账号", padding=(10, 6))
        card.pack(fill="x")
        self.lbl_account = ttk.Label(card, text="账号：-", font=FONT_B)
        self.lbl_account.grid(row=0, column=0, sticky="w", padx=(0, 20))
        self.lbl_login = ttk.Label(card, text="登录：未校验", font=FONT)
        self.lbl_login.grid(row=0, column=1, sticky="w", padx=(0, 20))
        self.lbl_watch = ttk.Label(card, text="监听：未运行", font=FONT)
        self.lbl_watch.grid(row=0, column=2, sticky="w", padx=(0, 20))
        self.lbl_lesson = ttk.Label(card, text="课程：-", font=FONT_SM)
        self.lbl_lesson.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        for col in range(4):
            card.columnconfigure(col, weight=1)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, pady=(8, 0))

        tab1 = ttk.Frame(nb, padding=10)
        nb.add(tab1, text="登录与监听")

        bar = ttk.Frame(tab1)
        bar.pack(fill="x", pady=(0, 8))
        self.btn_start = ttk.Button(bar, text="开始监听（首次会出二维码）",
                                    command=self._start_selected)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop_selected,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="刷新登录二维码",
                   command=self._start_login_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="校验登录状态",
                   command=lambda: self._check_login(force=True)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="放大二维码", command=self._zoom_qr).pack(side="left", padx=(6, 0))

        body = ttk.Frame(tab1)
        body.pack(fill="both", expand=True)

        qbox = ttk.Frame(body)
        qbox.pack(side="left", fill="y", padx=(0, 12))
        self.qr_label = tk.Label(qbox, width=QR_SIZE, height=QR_SIZE, background="#fafafa",
                                 text="首次监听会在这里\n显示微信登录二维码",
                                 font=FONT_SM, justify="center", relief="solid", bd=1)
        self.qr_label.pack()
        self.lbl_qr_hint = ttk.Label(qbox, text="", font=FONT_SM,
                                     wraplength=QR_SIZE, justify="center")
        self.lbl_qr_hint.pack(pady=(6, 0))

        lbox = ttk.Frame(body)
        lbox.pack(side="left", fill="both", expand=True)
        ttk.Label(lbox, text="运行日志", font=FONT_B).pack(anchor="w")
        self.log_text = self._make_text(lbox)

        tab2 = ttk.Frame(nb, padding=10)
        nb.add(tab2, text="历史记录")
        bar2 = ttk.Frame(tab2)
        bar2.pack(fill="x", pady=(0, 8))
        ttk.Button(bar2, text="刷新", command=self._load_history).pack(side="left")
        ttk.Button(bar2, text="清空本账号历史",
                   command=self._clear_history).pack(side="left", padx=6)
        ttk.Label(bar2, text="（记录保存在项目目录的 history_账号.log）",
                  font=FONT_SM).pack(side="left", padx=8)
        self.hist_text = self._make_text(tab2)

    def _make_text(self, parent):
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True, pady=(4, 0))
        text = tk.Text(box, wrap="word", font=MONO, background="#ffffff",
                       state="disabled", relief="solid", bd=1, height=8)
        scroll = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return text

    # -------------------------------------------------------- 账号

    def _load_accounts(self):
        data = load_json(ACCOUNTS_PATH, {})
        names = [n for n in (data.get("accounts") or []) if isinstance(n, str) and n.strip()]
        if not names:
            names = [self.cfg.get("yuketang", {}).get("name") or "me"]
        for name in names:
            self._new_account(name, select=False)
        self._refresh_tree()
        last = data.get("last")
        self._select(last if last in self.accounts else names[0])

    def _save_accounts(self):
        save_json(ACCOUNTS_PATH, {"accounts": list(self.accounts), "last": self._selected_name()})

    def _new_account(self, name, select=True):
        acc = Account(name)
        self.accounts[name] = acc
        if not self.tree.exists(name):
            self.tree.insert("", "end", iid=name, text=name, values=("未校验",))
        if select:
            self._select(name)
        return acc

    def _selected_name(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _select(self, name):
        if name in self.accounts:
            self.tree.selection_set(name)
            self.tree.focus(name)
            self._render_account(name)

    def _on_select(self, _event=None):
        name = self._selected_name()
        if name:
            self._render_account(name)
            self._save_accounts()

    def _render_account(self, name):
        """把选中账号的数据铺到面板上，并顺手校验一次登录状态"""
        acc = self.accounts.get(name)
        if acc is None:
            return
        self.lbl_account.configure(text=f"账号：{name}")
        self._update_card(acc)

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        if acc.lines:
            self.log_text.insert("end", "\n".join(acc.lines) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        self._refresh_qr_panel(acc)
        self._load_history()

        if time.time() - self.status_checked.get(name, 0) > 300:
            self._check_login()

    def _clear_panels(self):
        self.lbl_account.configure(text="账号：-")
        self.lbl_login.configure(text="登录：未校验", foreground="#666666")
        self.lbl_watch.configure(text="监听：未运行", foreground="#666666")
        self.lbl_lesson.configure(text="课程：-")
        for widget in (self.log_text, self.hist_text):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.qr_label.configure(image="", text="首次监听会在这里\n显示微信登录二维码")
        self.lbl_qr_hint.configure(text="")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="disabled")

    def _add_account(self):
        name = simpledialog.askstring(
            "添加账号",
            "给这个账号起个标识，用来区分各自的登录状态：",
            parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.accounts:
            messagebox.showinfo("提示", f"已经有叫「{name}」的账号了", parent=self.root)
            return
        if any(ch in name for ch in '\\/:*?"<>|'):
            messagebox.showerror("不能这么起名",
                                 '标识里不能包含 \\ / : * ? " < > | 这些字符', parent=self.root)
            return
        self._new_account(name)
        self._save_accounts()
        self._set_status(f"已添加账号 {name}")
        if messagebox.askyesno("添加成功",
                               f"「{name}」已添加。\n现在就开始监听并获取登录二维码吗？",
                               parent=self.root):
            self._start_login_selected(confirm=False)

    def _remove_account(self):
        name = self._selected_name()
        if not name:
            return
        acc = self.accounts[name]
        running = bool(acc.proc and acc.proc.poll() is None)
        tip = (f"「{name}」正在监听中，确定要停止并移除吗？\n"
               if running else f"确定移除账号「{name}」吗？\n")
        if not messagebox.askyesno("移除账号",
                                   tip + "（登录状态文件会保留，重新添加就能继续用）",
                                   parent=self.root):
            return
        if running:
            try:
                acc.proc.terminate()
            except Exception:
                pass
        self._close_log_file(name)
        if self.tree.exists(name):
            self.tree.delete(name)
        self.accounts.pop(name, None)
        self._save_accounts()
        if self.accounts:
            self._select(list(self.accounts)[0])
        else:
            self._clear_panels()
        self._set_status(f"已移除账号 {name}")

    # -------------------------------------------------------- 监听控制

    def _begin_process(self, name, args, mode, auto_start=False, banner=""):
        """所有子进程都从这里启动，统一处理会话号、日志文件和新旧二维码的界线"""
        acc = self.accounts[name]
        if acc.proc and acc.proc.poll() is None:
            return False

        acc.lines = []
        acc.lesson = None
        acc.qr_photo = None
        acc.qr_mtime = None
        acc.qr_state = "waiting"
        acc.mode = mode
        acc.auto_start = auto_start
        acc.start_at = time.time()
        acc.qr_accept_after = acc.start_at - 2   # 只认这一轮新生成的二维码
        acc.session += 1
        session = acc.session
        self._open_log_file(name)
        if banner:
            self._append_log(name, f"==== {banner} {time.strftime('%Y-%m-%d %H:%M:%S')} ====")

        try:
            proc = subprocess.Popen(
                [PY, "-u"] + args, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self._append_log(name, f"[启动失败] {type(e).__name__}: {e}")
            return False

        acc.proc = proc
        threading.Thread(target=self._pump, args=(proc, name, session), daemon=True).start()
        if name == self._selected_name():
            self._render_account(name)
        return True

    def _start_selected(self):
        """开始（或继续）监听：本地 cookie 还有效就不会再要求扫码"""
        name = self._selected_name()
        if not name:
            messagebox.showinfo("提示", "先添加一个账号", parent=self.root)
            return
        acc = self.accounts[name]
        if acc.proc and acc.proc.poll() is None:
            messagebox.showinfo("提示", f"「{name}」正在运行中，先点“停止”再开始", parent=self.root)
            return
        if self._begin_process(name, ["run.py", "ykt", "--name", name], "listen",
                               banner="开始监听"):
            self._record(name, "开始监听")
            self._set_status(f"{name} 已开始监听")

    def _start_login_selected(self, confirm=True):
        """刷新登录二维码：不管本地 cookie 有没有效，都强制拿一张新的二维码"""
        name = self._selected_name()
        if not name:
            messagebox.showinfo("提示", "先添加一个账号", parent=self.root)
            return
        acc = self.accounts[name]
        busy = bool(acc.proc and acc.proc.poll() is None)
        if confirm and (busy or acc.login_ok is True):
            state = "正在监听" if busy else "已登录"
            if not messagebox.askyesno(
                    "刷新登录二维码",
                    f"「{name}」当前{state}。\n"
                    f"{'会先停止当前任务，' if busy else ''}并且需要重新用微信扫码，确定吗？",
                    parent=self.root):
                return

        if busy:
            acc.session += 1      # 让旧进程后续的输出全部作废
            try:
                acc.proc.terminate()
            except Exception:
                pass
            acc.proc = None
            time.sleep(0.3)       # 给旧进程一点时间让出 cookie 文件
            self._close_log_file(name)

        if self._begin_process(name, ["run.py", "login", "--name", name], "login",
                               auto_start=True, banner="刷新登录二维码"):
            self._record(name, "开始扫码登录")
            self._set_status(f"{name} 正在获取新的登录二维码")

    def _auto_start_listener(self, name):
        acc = self.accounts.get(name)
        if acc is None or (acc.proc and acc.proc.poll() is None):
            return
        if self._begin_process(name, ["run.py", "ykt", "--name", name], "listen",
                               banner="扫码登录完成，开始监听"):
            self._record(name, "登录成功，自动开始监听")
            self._set_status(f"{name} 登录成功，已开始监听")

    def _stop_selected(self):
        name = self._selected_name()
        if not name:
            return
        acc = self.accounts[name]
        if acc.proc and acc.proc.poll() is None:
            acc.proc.terminate()
            self._append_log(name, f"==== 界面请求停止 {time.strftime('%H:%M:%S')} ====")
            self._set_status(f"{name} 正在停止")

    def _pump(self, proc, name, session):
        """子进程输出逐行丢进队列，界面主线程再取出来显示"""
        try:
            for line in proc.stdout:
                self.queue.put(("line", name, session, line.rstrip("\r\n")))
        except Exception as e:
            self.queue.put(("line", name, session, f"[读取输出失败] {type(e).__name__}: {e}"))
        try:
            code = proc.wait()
        except Exception:
            code = -1
        self.queue.put(("exit", name, session, code))

    def _drain(self):
        while True:
            try:
                kind, name, session, payload = self.queue.get_nowait()
            except queue.Empty:
                break

            acc = self.accounts.get(name)
            if acc is None:
                continue
            if kind in ("line", "exit") and session != acc.session:
                continue          # 属于已经被替换掉的旧进程，直接丢掉

            if kind == "line":
                self._append_log(name, payload)
            elif kind == "text":
                for line in str(payload).splitlines():
                    self._append_log(name, line)
            elif kind == "status":
                self._apply_status(name, payload)
            elif kind == "exit":
                acc.proc = None
                mode = acc.mode
                acc.mode = None
                self._close_log_file(name)
                if mode == "login":
                    if payload == 0:
                        acc.qr_state = "used"
                        acc.login_ok = True
                        self._append_log(name, "==== 扫码登录完成 ====")
                        self._record(name, "扫码登录成功")
                    else:
                        self._append_log(name, f"==== 扫码登录未完成（退出码 {payload}）====")
                        self._record(name, f"扫码登录未完成（退出码 {payload}）")
                else:
                    self._append_log(name, f"==== 监听结束（退出码 {payload}）====")
                    self._record(name, f"监听结束（退出码 {payload}）")
                self._set_status(f"{name} 任务已结束")
                if name == self._selected_name():
                    self._refresh_qr_panel(acc)
                if mode == "login" and payload == 0:
                    self._check_login(force=True, name=name)
                    if acc.auto_start:
                        acc.auto_start = False
                        self.root.after(800, lambda n=name: self._auto_start_listener(n))
        self._refresh_tree()
        self._update_current_card()
        self.root.after(150, self._drain)

    def _tick(self):
        for name, acc in list(self.accounts.items()):
            self._check_qr(name, acc)
        name = self._selected_name()
        if name:
            self._refresh_qr_panel(self.accounts.get(name))
        self._update_current_card()
        self._refresh_tree()
        self.root.after(1000, self._tick)

    def _update_current_card(self):
        name = self._selected_name()
        if name and name in self.accounts:
            self._update_card(self.accounts[name])

    def _update_card(self, acc):
        if acc.login_ok is True:
            who = f"（{acc.login_name}）" if acc.login_name else ""
            self.lbl_login.configure(text=f"登录：有效{who}", foreground="#1a7f37")
        elif acc.login_ok is False:
            self.lbl_login.configure(text="登录：未登录或已失效", foreground="#b42318")
        else:
            self.lbl_login.configure(text="登录：未校验", foreground="#666666")

        running = bool(acc.proc and acc.proc.poll() is None)
        used = int((time.time() - (acc.start_at or time.time())) / 60)
        if running and acc.mode == "login":
            self.lbl_watch.configure(
                text=f"等待微信扫码（已等 {used} 分钟，二维码约 2 分钟换一次）",
                foreground="#b26a00")
        elif running:
            self.lbl_watch.configure(
                text=f"监听：运行中（已运行 {used} 分钟，不会自动停止）",
                foreground="#1a7f37")
        else:
            self.lbl_watch.configure(text="监听：未运行", foreground="#666666")

        self.lbl_lesson.configure(text=f"课程：{acc.lesson or '当前没有进行中的课'}")
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _refresh_tree(self):
        for name, acc in self.accounts.items():
            if not self.tree.exists(name):
                continue
            if acc.proc and acc.proc.poll() is None:
                state = "监听中"
            elif acc.login_ok is True:
                state = "已登录"
            elif acc.login_ok is False:
                state = "未登录"
            else:
                state = "未校验"
            if self.tree.set(name, "state") != state:
                self.tree.item(name, values=(state,))

    # -------------------------------------------------------- 二维码 / 登录状态

    def _check_qr(self, name, acc):
        """只认这一轮任务开始之后生成的二维码，历史遗留的旧图一律忽略"""
        path = os.path.join(ROOT, f"qrcode_{name}.jpg")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime < acc.qr_accept_after:
            return
        if acc.qr_mtime is not None and mtime <= acc.qr_mtime:
            return

        acc.qr_mtime = mtime
        acc.qr_photo = None
        if not qr_file_ok(path):
            # 空文件/坏图：别冒充成能扫的二维码，等上层自动重试出新的一张
            acc.qr_state = "broken"
            if name == self._selected_name():
                self._refresh_qr_panel(acc)
            return

        acc.qr_state = "fresh"
        if name != self._selected_name():
            return
        if HAS_PIL:
            try:
                image = Image.open(path)
                image.load()
                image.thumbnail((QR_SIZE - 12, QR_SIZE - 12))
                acc.qr_photo = ImageTk.PhotoImage(image)
            except Exception:
                acc.qr_photo = None
        self._refresh_qr_panel(acc)

    def _refresh_qr_panel(self, acc):
        """统一决定二维码区域显示什么，不再把过期或历史二维码说成能扫的"""
        if acc is None:
            return

        if acc.qr_state == "used":
            who = f"（{acc.login_name}）" if acc.login_name else ""
            self.qr_label.configure(image="", text="登录完成\n无需扫码")
            self.lbl_qr_hint.configure(text=f"已登录{who}", foreground="#1a7f37")
            return

        if acc.qr_state == "broken":
            self.qr_label.configure(image="", text="二维码没下载成功\n正在自动重试 ...")
            self.lbl_qr_hint.configure(
                text="刚拿到的 ticket 无效（服务端返回空响应），会自动换一张",
                foreground="#b26a00")
            return

        age = None if acc.qr_mtime is None else time.time() - acc.qr_mtime
        if acc.qr_state == "fresh" and age is not None and age < QR_TTL:
            if acc.qr_photo is not None:
                self.qr_label.configure(image=acc.qr_photo, text="")
            else:
                self.qr_label.configure(image="", text="二维码已生成\n点“放大二维码”查看")
            self.lbl_qr_hint.configure(
                text=(f"请用微信扫码（{time.strftime('%H:%M:%S', time.localtime(acc.qr_mtime))}"
                      f" 生成，约 {max(int(QR_TTL - age), 0)} 秒后失效）"),
                foreground="#1a7f37")
            return

        self.qr_label.configure(image="")
        if acc.qr_mtime is not None:
            self.qr_label.configure(text="二维码已过期\n点“刷新登录二维码”换新")
            self.lbl_qr_hint.configure(text="一张二维码大约 2 分钟失效",
                                       foreground="#b26a00")
        elif acc.mode == "login":
            self.qr_label.configure(text="正在获取二维码 ...")
            self.lbl_qr_hint.configure(text="稍等一两秒", foreground="#666666")
        elif acc.login_ok is True:
            self.qr_label.configure(text="当前已登录\n无需扫码")
            self.lbl_qr_hint.configure(text="要重新扫码就点“刷新登录二维码”",
                                       foreground="#666666")
        else:
            self.qr_label.configure(text="还没有二维码")
            self.lbl_qr_hint.configure(text="点“刷新登录二维码”获取", foreground="#666666")

    def _zoom_qr(self):
        name = self._selected_name()
        if not name:
            return
        acc = self.accounts.get(name)
        path = os.path.join(ROOT, f"qrcode_{name}.jpg")
        usable = (acc is not None and acc.qr_state == "fresh"
                  and acc.qr_mtime is not None
                  and acc.qr_mtime >= acc.qr_accept_after
                  and qr_file_ok(path))
        if usable and hasattr(os, "startfile"):
            try:
                os.startfile(path)
                return
            except Exception:
                pass
        if acc is not None and acc.qr_state == "broken":
            messagebox.showinfo(
                "提示",
                "这张二维码没下载成功（服务端返回了空响应），程序正在自动重试。\n"
                "等几秒再点，或者点“刷新登录二维码”重新获取。",
                parent=self.root)
            return
        messagebox.showinfo("提示", "现在没有可用的二维码，先点“刷新登录二维码”",
                            parent=self.root)

    def _check_login(self, force=False, name=None):
        name = name or self._selected_name()
        if not name:
            return
        if not force and time.time() - self.status_checked.get(name, 0) < 30:
            return
        self.status_checked[name] = time.time()
        if name == self._selected_name():
            self.lbl_login.configure(text="登录：校验中 ...", foreground="#666666")
        threading.Thread(target=self._status_worker, args=(name,), daemon=True).start()

    def _status_worker(self, name):
        out = run_capture(["run.py", "status", "--name", name], timeout=45)
        self.queue.put(("status", name, 0, out))

    def _apply_status(self, name, out):
        acc = self.accounts.get(name)
        if acc is None:
            return
        text = str(out)
        if "登录有效" in text:
            acc.login_ok = True
            match = re.search(r"当前账号[:：]\s*(.+)", text)
            acc.login_name = match.group(1).strip() if match else None
        elif "cookie 已失效" in text or "没找到" in text:
            acc.login_ok = False
        else:
            acc.login_ok = None
        match = re.search(r"正在上课[:：]\s*(.+)", text)
        acc.lesson = match.group(1).strip() if match else None
        self._refresh_tree()
        self._update_current_card()
        if name == self._selected_name():
            self._refresh_qr_panel(acc)

    # -------------------------------------------------------- 日志 / 历史

    def _append_log(self, name, line):
        acc = self.accounts.get(name)
        if acc is None:
            return
        for raw in (str(line).splitlines() or [""]):
            acc.lines.append(raw)
        if len(acc.lines) > MAX_KEEP_LINES:
            acc.lines = acc.lines[-MAX_KEEP_LINES:]
        if acc.log_handle is not None:
            try:
                acc.log_handle.write(str(line) + "\n")
            except Exception:
                pass
        if name == self._selected_name():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", str(line) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self._maybe_record(name, str(line))

    def _open_log_file(self, name):
        """每轮任务单独写一份日志文件，方便事后排查"""
        self._close_log_file(name)
        acc = self.accounts.get(name)
        if acc is None:
            return
        try:
            acc.log_handle = open(os.path.join(ROOT, f"log_{name}.log"),
                                  "w", encoding="utf-8", buffering=1)
        except Exception:
            acc.log_handle = None

    def _close_log_file(self, name):
        acc = self.accounts.get(name)
        if acc is None or acc.log_handle is None:
            return
        try:
            acc.log_handle.close()
        except Exception:
            pass
        acc.log_handle = None

    def _history_path(self, name):
        return os.path.join(ROOT, f"history_{name}.log")

    def _maybe_record(self, name, line):
        for keyword, summary in HISTORY_RULES:
            if keyword in line:
                self._record(name, summary)
                break
        if '"problemId"' in line:
            detail = line[line.find("{"):] if "{" in line else line
            try:
                data = json.loads(detail)
                summary = (f"已提交作答：题型={data.get('problemType')} "
                           f"答案={data.get('result')}")
            except Exception:
                summary = f"已提交作答：{detail[:200]}"
            self._record(name, summary)

    def _record(self, name, text):
        entry = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {name}  {text}"
        try:
            with open(self._history_path(name), "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass
        if name == self._selected_name():
            self.hist_text.configure(state="normal")
            self.hist_text.insert("end", entry + "\n")
            self.hist_text.see("end")
            self.hist_text.configure(state="disabled")

    def _load_history(self):
        name = self._selected_name()
        self.hist_text.configure(state="normal")
        self.hist_text.delete("1.0", "end")
        if not name:
            self.hist_text.configure(state="disabled")
            return
        path = self._history_path(name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    self.hist_text.insert("end", "".join(f.readlines()[-300:]))
            except Exception as e:
                self.hist_text.insert("end", f"（读取失败：{e}）\n")
        else:
            self.hist_text.insert("end", "（还没有记录，开始监听后会自动写入）\n")
        self.hist_text.see("end")
        self.hist_text.configure(state="disabled")

    def _clear_history(self):
        name = self._selected_name()
        if not name:
            return
        if not messagebox.askyesno("清空历史",
                                   f"确定清空「{name}」的全部历史记录吗？此操作不可撤销",
                                   parent=self.root):
            return
        try:
            open(self._history_path(name), "w", encoding="utf-8").close()
        except Exception as e:
            messagebox.showerror("清空失败", str(e), parent=self.root)
            return
        self._load_history()
        self._set_status(f"{name} 的历史已清空")

    # -------------------------------------------------------- 其他

    def _selfcheck(self):
        name = self._selected_name() or (list(self.accounts)[0] if self.accounts else None)
        if not name:
            messagebox.showinfo("提示", "先添加一个账号", parent=self.root)
            return
        self._append_log(name, f"==== 环境自检 {time.strftime('%H:%M:%S')} ====")
        self._set_status("正在自检 ...")
        threading.Thread(target=self._selfcheck_worker, args=(name,), daemon=True).start()

    def _selfcheck_worker(self, name):
        self.queue.put(("text", name, 0, run_capture(["run.py", "check"], timeout=60)))

    def _set_status(self, text):
        self.status.configure(text=text)

    def _on_close(self):
        running = [acc for acc in self.accounts.values()
                   if acc.proc and acc.proc.poll() is None]
        if running and not messagebox.askyesno(
                "确认退出",
                f"还有 {len(running)} 个账号在监听中，退出会一并停止，确定吗？",
                parent=self.root):
            return
        for acc in running:
            try:
                acc.proc.terminate()
            except Exception:
                pass
        for name in list(self.accounts):
            self._close_log_file(name)
        self.root.destroy()


def _fatal(title, text):
    """没有控制台时也能把错误告诉用户"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text)[-1500:], title, 0x10)
    except Exception:
        pass


def main():
    selftest = "--selftest" in sys.argv
    if not TK_OK:
        print("缺少 tkinter，无法启动图形界面")
        return 1
    if not HAS_PIL:
        print("提示：没装 Pillow，二维码只能通过“放大二维码”按钮查看")

    root = tk.Tk()
    app = App(root)
    if selftest:
        root.update()
        name = app._selected_name()
        if name:
            app._render_account(name)
            app._load_history()
        root.update()
        root.destroy()
        print("GUI 自检通过")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        detail = traceback.format_exc()
        try:
            with open(os.path.join(ROOT, "gui_error.log"), "w", encoding="utf-8") as f:
                f.write(detail)
        except Exception:
            pass
        _fatal("lazytool 启动失败", detail)
        sys.exit(1)

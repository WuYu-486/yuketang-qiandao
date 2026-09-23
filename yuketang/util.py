import os
import tempfile

import requests
import json

# 图片来源的常见文件头，用来确认拿到的确实是张图
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",         # JPEG
    b"\x89PNG\r\n\x1a\n",    # PNG
    b"GIF87a",
    b"GIF89a",
)


def download_qrcode(url,name):
    """下载登录二维码并原子落盘。

    上游原实现是 requests.get 之后直接 f.write(res.content)，既不校验也不设超时：
    微信侧对已失效的 ticket 会返回 404 + 空响应体，requests 默认不会因此抛异常，
    于是这里会静默写出一个 0 字节文件，还照常打印“已保存”，
    图形界面随后把它当成可用二维码，点“放大二维码”自然打不开。
    现在校验失败直接抛异常，由上层 ws_controller 重试并换一张新 ticket。
    """
    path = f"qrcode_{name}.jpg"
    print("下载登录二维码中")
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    data = res.content
    if not data.startswith(_IMAGE_MAGIC):
        raise ValueError(
            f"二维码响应不是图片（HTTP {res.status_code}, "
            f"Content-Type={res.headers.get('Content-Type')}, {len(data)} 字节），"
            f"ticket 可能已失效"
        )
    # 先写临时文件再原子替换：避免界面轮询或看图软件读到写了一半的图
    fd, tmp = tempfile.mkstemp(dir=".", prefix=f".qrcode_{name}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"二维码已保存: {os.path.abspath(path)}（{len(data)} 字节）")

async def recv_json(websocket):
    server_response = await websocket.recv()
    # print(f"Received from server: {server_response}")
    info=json.loads(server_response)
    return info

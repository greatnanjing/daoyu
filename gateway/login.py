"""终端扫码登录：渲染二维码、轮询确认、token 落盘。服务器首次部署/无人值守重连用。

qrcode/pil 是可选依赖（qr extras）：缺失时降级为只打印二维码 URL——登录主流程不能崩。
"""
import asyncio
import time

import aiohttp

from common.config import load_config
from common.db import Database
from gateway.ilink import ILinkClient


async def terminal_login(db, ilink, base_url: str | None = None) -> dict:
    """扫码直至拿到 bot_token，写入 state（bot_token/bot_base_url/login_at）。"""
    current = db.get_state("bot_token") or ""
    data = await ilink.get_bot_qrcode([current] if current else [], base_url)
    qr_content = data.get("qrcode_img_content") or data["qrcode"]
    print("\n请用微信扫码登录（也可把此链接发到微信打开）：")
    print(qr_content)
    _safe_render(str(qr_content))

    current_base = base_url
    while True:
        result = await ilink.poll_login_status(data["qrcode"])
        if result.get("bot_token"):
            db.set_state("bot_token", result["bot_token"])
            db.set_state("bot_base_url", result.get("baseurl") or "")
            db.set_state("login_at", str(time.time()))
            print("[OK] 登录成功，token 已落盘")   # 不用 emoji：非 UTF-8 管道下 print 会崩
            return result
        if result.get("already_connected"):
            print("服务端提示已连接，沿用当前 token")
            return {"bot_token": db.get_state("bot_token")}
        if result.get("expired") or result.get("verify_code_blocked"):
            data = await ilink.get_bot_qrcode([], current_base)
            print("二维码已过期，已重新生成：", data.get("qrcode_img_content") or data["qrcode"])
            _safe_render(str(data.get("qrcode_img_content") or data["qrcode"]))
        if result.get("redirect_base"):
            current_base = result["redirect_base"]
        if result.get("need_verifycode"):
            verify = input("请输入手机微信显示的数字配对码: ").strip()
            result = await ilink.poll_login_status(data["qrcode"], verify)
            if result.get("bot_token"):
                db.set_state("bot_token", result["bot_token"])
                db.set_state("bot_base_url", result.get("baseurl") or "")
                db.set_state("login_at", str(time.time()))
                print("[OK] 登录成功，token 已落盘")
                return result
        await asyncio.sleep(1)


def _safe_render(content: str) -> None:
    _save_png(content)
    try:
        _render_qr(content)
    except Exception:   # 渲染失败不影响登录主流程（URL 已打印，链接可开）
        pass


def _save_png(content: str) -> None:
    """二维码存 PNG：终端 ASCII 难扫时直接打开图片；无人值守推送渠道也用它。"""
    try:
        from pathlib import Path

        import qrcode
        path = Path("data") / "qrcode.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        qrcode.make(content).save(path)
        print(f"二维码图片已保存: {path.resolve()}")
    except Exception as e:   # 展示性增强，失败不阻断登录
        print(f"(二维码 PNG 保存失败: {e})")


def _render_qr(content: str) -> None:
    if not content.startswith("http"):
        return
    try:
        import io
        import urllib.request

        from PIL import Image
        req = urllib.request.Request(content, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            img = Image.open(io.BytesIO(resp.read())).convert("L")
        w = min(72, img.width)
        # max(1, ...) 双防御：img.width=0 时既除零又零宽 resize
        img = img.resize((max(1, w), max(1, img.height * w // max(1, img.width))))
        for y in range(img.height):
            print("".join("██" if img.getpixel((x, y)) < 128 else "  "
                          for x in range(img.width)))
        return
    except Exception:
        pass   # 图片下载/PIL 解码失败 → 走本地 qrcode 生成兜底
    try:
        import qrcode
    except ImportError:
        return   # 无 PIL 且无 qrcode：URL 已打印，够了
    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    for row in qr.get_matrix():
        print("".join("██" if c else "  " for c in row))


def main() -> None:
    """daoyu-login 入口：独立扫码登录（写 state 后退出，不启动主服务）。"""
    cfg = load_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.ensure_schema()

    async def _run():
        async with aiohttp.ClientSession() as session:
            await terminal_login(db, ILinkClient(session),
                                 db.get_state("bot_base_url") or None)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

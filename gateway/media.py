"""iLink 媒体（图片）加解密与 CDN 上传/下载。

协议字段级依据：官方 npm 包 @tencent-weixin/openclaw-weixin v2.4.6 dist 源码
（cdn/aes-ecb.js、cdn/cdn-url.js、cdn/upload.js、cdn/cdn-upload.js、
media/media-download.js、cdn/pic-decrypt.js），详见 M3 spec §2。
CDN 上所有媒体经 AES-128-ECB + PKCS7 加密；上传 POST 密文、成功取响应头
x-encrypted-param；下载 GET 密文后解密。"""
import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from gateway.ilink import CdnClientError

log = logging.getLogger(__name__)

CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class MediaError(Exception):
    """媒体处理失败（格式不识别 / 密钥坏 / 超限 / 协议缺字段）。"""


def pkcs7_padded_size(n: int) -> int:
    """PKCS7(128) 填充后大小：恒补 ≥1 字节到 16 边界（整块输入再补一整块）。
    对齐官方 aesEcbPaddedSize: Math.ceil((size + 1) / 16) * 16。"""
    return (n + 16) // 16 * 16


def aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    if len(key) != 16:
        raise MediaError("AES-128 key 必须是 16 字节")
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    if len(key) != 16:
        raise MediaError("AES-128 key 必须是 16 字节")
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpadder = PKCS7(128).unpadder()
    try:
        padded = dec.update(ciphertext) + dec.finalize()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as e:
        raise MediaError(f"解密失败（密钥不对或数据损坏）: {e}") from e


def sniff_image(buf: bytes) -> str:
    """magic bytes 白名单 → 扩展名。不信扩展名/Content-Type（入站是外部输入）。"""
    if buf.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if buf.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if buf.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "webp"
    raise MediaError("不认识的图片格式（白名单：PNG/JPEG/GIF/WebP）")


def parse_inbound_aes_key(image_item: dict) -> bytes:
    """入站图片消息的 aeskey 解析（官方 media-download.js/pic-decrypt.js 行为）：
    ① image_item.aeskey（hex 字符串，优先）② media.aes_key = base64(raw16B)
    ③ media.aes_key = base64(hex32 ASCII)（文件/语音形态兼容）。"""
    hex_key = str(image_item.get("aeskey") or "").strip()
    if hex_key:
        try:
            key = bytes.fromhex(hex_key)
        except ValueError as e:
            raise MediaError(f"aeskey 非合法 hex: {e}") from e
        if len(key) == 16:
            return key
    b64 = str((image_item.get("media") or {}).get("aes_key") or "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            raise MediaError(f"aes_key 非合法 base64: {e}") from e
        if len(raw) == 16:
            return raw
        if len(raw) == 32:
            ascii_hex = raw.decode("ascii", "ignore")
            if len(ascii_hex) == 32:
                try:
                    return bytes.fromhex(ascii_hex)
                except ValueError as e:
                    raise MediaError(f"aes_key hex 形态损坏: {e}") from e
    raise MediaError("aes_key 无法解析为 16 字节密钥（缺字段或形态不符）")


# ---- 上传/下载编排（网络细节在 ilink 层，此处只编排协议流程）----

_UPLOAD_RETRIES = 3


@dataclass
class UploadedImage:
    filekey: str          # hex32（getuploadurl 用）
    download_param: str   # x-encrypted-param（sendmessage 的 encrypt_query_param）
    aes_key: bytes        # 原始 16B（CDN 密文用它加密；sendmessage 报 base64(hex32 ASCII)）
    size_cipher: int      # 密文大小（sendmessage 的 mid_size）


def build_cdn_download_url(encrypted_query_param: str) -> str:
    return f"{CDN_BASE_URL}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def build_cdn_upload_url(upload_param: str, filekey: str) -> str:
    return (f"{CDN_BASE_URL}/upload?encrypted_query_param={quote(upload_param, safe='')}"
            f"&filekey={quote(filekey, safe='')}")


async def upload_image(ilink, path: str, to_user: str,
                       token: str | None, base_url: str | None) -> UploadedImage:
    """官方五步流程（spec §2.2）：读文件 → md5/keygen → getuploadurl →
    POST 密文 → 返回引用。4xx（CdnClientError）立败；5xx/网络重试 ≤3。"""
    raw = Path(path).read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise MediaError(f"图片超 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限")
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    resp = await ilink.getuploadurl(
        filekey=filekey, media_type=1, to_user_id=to_user,
        rawsize=len(raw), rawfilemd5=hashlib.md5(raw).hexdigest(),
        filesize=pkcs7_padded_size(len(raw)), no_need_thumb=True,
        aeskey=aes_key.hex(), token=token, base_url=base_url)
    upload_full = str(resp.get("upload_full_url") or "").strip()
    upload_param = resp.get("upload_param")
    if not upload_full and not upload_param:
        raise MediaError(f"getuploadurl 未返回上传地址: {str(resp)[:200]}")
    ciphertext = aes_ecb_encrypt(raw, aes_key)
    url = upload_full or build_cdn_upload_url(str(upload_param), filekey)
    last_err: Exception | None = None
    for _ in range(_UPLOAD_RETRIES):
        try:
            param = await ilink.cdn_upload(url, ciphertext)
            return UploadedImage(filekey=filekey, download_param=param,
                                 aes_key=aes_key, size_cipher=len(ciphertext))
        except CdnClientError:
            raise                      # 4xx 客户端错误：立败不重试
        except Exception as e:         # 5xx / 网络：重试
            last_err = e
            log.warning("CDN 上传失败（将重试）: %r", e)
    raise MediaError(f"CDN 上传重试 {_UPLOAD_RETRIES} 次仍失败: {last_err!r}")


async def download_inbound_image(ilink, image_item: dict, dest_dir: Path) -> str:
    """入站图：full_url 优先否则拼 download URL → GET 密文 → 解密 → sniff 白名单
    → 随机名落盘。返回绝对路径。"""
    media = image_item.get("media") or {}
    full_url = str(media.get("full_url") or "").strip()
    eq = str(media.get("encrypt_query_param") or "")
    if not full_url and not eq:
        raise MediaError("图片消息缺 CDN 引用（无 full_url/encrypt_query_param）")
    encrypted = await ilink.cdn_download(full_url or build_cdn_download_url(eq))
    key = parse_inbound_aes_key(image_item)
    raw = aes_ecb_decrypt(encrypted, key)
    if len(raw) > MAX_IMAGE_BYTES:
        raise MediaError(f"图片超 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限")
    ext = sniff_image(raw)
    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"img-{secrets.token_hex(8)}.{ext}"
    dest.write_bytes(raw)
    log.info("入站图片已落盘: %s (%d bytes)", dest, len(raw))
    return str(dest)


def cleanup_expired_media(root: Path, retention_days: float,
                          protected: set[str]) -> int:
    """清理 data/media（根目录及 inbound/outbound 子目录）中超期的图片文件。

    删除条件 = 文件名 `img-*` 前缀（入/出站落盘命名 img-<16hex>.<ext>）**或**
    图片扩展名（png/jpg/jpeg/gif/webp，大小写不敏感——claude 用 Write/截图工具
    落盘的自定义名文件如 hermes-pw.png 同样清理，真机实证其堆在 media 根目录
    不受 img-* 规则覆盖）。非图片文件（claude 的 tmp-*.html 等工作产物）不碰
    ——不作猜测。protected（outbox 未终态行引用的 media_path，两侧 abspath
    归一化比较——approval_mcp 孙进程写的是绝对路径）一律保留，防重试/死信
    取证引用悬空。目录缺失/单文件删除失败容错继续，返回删除数。"""
    import time
    cutoff = time.time() - retention_days * 86400
    keep = {os.path.abspath(p) for p in protected}
    media = Path(root) / "data" / "media"
    targets = [media, media / "inbound", media / "outbound"]
    removed = 0
    for d in targets:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if not (f.name.startswith("img-")
                    or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                continue
            if os.path.abspath(f) in keep:
                continue
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                f.unlink()
                removed += 1
            except OSError as e:
                log.warning("media 清理跳过 %s: %r", f, e)   # Windows 占用等——继续
    return removed

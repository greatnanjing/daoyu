"""iLink 媒体（图片）加解密与 CDN 上传/下载。

协议字段级依据：官方 npm 包 @tencent-weixin/openclaw-weixin v2.4.6 dist 源码
（cdn/aes-ecb.js、cdn/cdn-url.js、cdn/upload.js、cdn/cdn-upload.js、
media/media-download.js、cdn/pic-decrypt.js），详见 M3 spec §2。
CDN 上所有媒体经 AES-128-ECB + PKCS7 加密；上传 POST 密文、成功取响应头
x-encrypted-param；下载 GET 密文后解密。"""
import base64
import hashlib
import logging
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

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
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    try:
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

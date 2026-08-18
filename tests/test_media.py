"""media.py 纯函数：AES-128-ECB/PKCS7（协议依据官方包 v2.4.6 cdn/aes-ecb.js）、
PKCS7 填充大小、magic bytes 白名单、入站 aeskey 双形态解析。"""
import base64
import secrets

import pytest

from gateway.media import (MAX_IMAGE_BYTES, MediaError, aes_ecb_decrypt,
                           aes_ecb_encrypt, parse_inbound_aes_key,
                           pkcs7_padded_size, sniff_image)

KEY = secrets.token_bytes(16)


def test_padded_size_matches_official_formula():
    # 官方：Math.ceil((size + 1) / 16) * 16（PKCS7 恒补 ≥1 字节，整块也补一块）
    for n in (0, 1, 15, 16, 17, 32, 1000):
        assert pkcs7_padded_size(n) == (n + 16) // 16 * 16
    assert pkcs7_padded_size(16) == 32   # 整块输入再补一整块
    assert pkcs7_padded_size(15) == 16


def test_aes_ecb_roundtrip_various_lengths():
    for n in (0, 1, 15, 16, 17, 1024):
        data = secrets.token_bytes(n)
        enc = aes_ecb_encrypt(data, KEY)
        assert len(enc) == pkcs7_padded_size(n)
        assert aes_ecb_decrypt(enc, KEY) == data


def test_aes_ecb_wrong_key_fails():
    enc = aes_ecb_encrypt(b"hello", KEY)
    with pytest.raises(Exception):
        aes_ecb_decrypt(enc, secrets.token_bytes(16))


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_sniff_image_whitelist():
    assert sniff_image(_PNG) == "png"
    assert sniff_image(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "jpg"
    assert sniff_image(b"GIF89a" + b"\x00" * 8) == "gif"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    for bad in (b"", b"\x00" * 16, b"RIFF\x00\x00\x00\x00XXXX", b"BMP\x00"):
        with pytest.raises(MediaError):
            sniff_image(bad)


def test_parse_inbound_aes_key_three_forms():
    # 形态 1：image_item.aeskey 顶层 hex（官方 media-download.js 优先路径）
    assert parse_inbound_aes_key({"aeskey": KEY.hex(), "media": {}}) == KEY
    # 形态 2：media.aes_key = base64(raw16B)（图片常见）
    b64 = base64.b64encode(KEY).decode()
    assert parse_inbound_aes_key({"aeskey": "", "media": {"aes_key": b64}}) == KEY
    # 形态 3：media.aes_key = base64(hex32 字符串)（官方 parseAesKey 兼容形态）
    b64hex = base64.b64encode(KEY.hex().encode()).decode()
    assert parse_inbound_aes_key({"media": {"aes_key": b64hex}}) == KEY
    # 顶层 hex 优先于 media
    other = secrets.token_bytes(16)
    assert parse_inbound_aes_key(
        {"aeskey": KEY.hex(), "media": {"aes_key": base64.b64encode(other).decode()}}) == KEY


def test_parse_inbound_aes_key_rejects_garbage():
    for item in ({}, {"media": {}}, {"media": {"aes_key": "!!!"}},
                 {"aeskey": "zz", "media": {}},   # 非 hex
                 {"aeskey": "abcd", "media": {}}):  # hex 但长度错
        with pytest.raises(MediaError):
            parse_inbound_aes_key(item)


def test_max_image_bytes_is_20mb():
    assert MAX_IMAGE_BYTES == 20 * 1024 * 1024

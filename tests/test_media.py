"""media.py 纯函数：AES-128-ECB/PKCS7（协议依据官方包 v2.4.6 cdn/aes-ecb.js）、
PKCS7 填充大小、magic bytes 白名单、入站 aeskey 双形态解析。"""
import base64
import secrets
from pathlib import Path

import pytest

from gateway.media import (CDN_BASE_URL, MAX_FILE_BYTES, MAX_IMAGE_BYTES,
                           MEDIA_TYPE_FILE, MEDIA_TYPE_VIDEO, MediaError,
                           UploadedMedia, aes_ecb_decrypt, aes_ecb_encrypt,
                           build_cdn_download_url, build_cdn_upload_url,
                           build_file_item, build_video_item,
                           download_inbound_image, download_inbound_media,
                           parse_inbound_aes_key, parse_media_aes_key,
                           pkcs7_padded_size, sniff_image, upload_media)

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


def test_aes_ecb_non_aligned_ciphertext_rejected():
    # 非 16 对齐密文也必须统一转 MediaError，不许裸 ValueError 逃逸
    with pytest.raises(MediaError):
        aes_ecb_decrypt(b"abc", KEY)


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


# ---- 上传/下载编排（ilink 用最小 fake，不 mock HTTP——ilink 层已有自己的测试）----

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(64)


class FakeUploadILink:
    """getuploadurl/cdn_upload/send 三件：行为按 self.fail_times 依次失败。"""

    def __init__(self, fail_times: int = 0, status: int = 503):
        self.fail_times = fail_times
        self.status = status          # 503=服务端（可重试）；403=客户端（立败）
        self.calls = 0

    async def getuploadurl(self, **kw):
        self.got = kw
        return {"upload_param": "UP-PARAM"}

    async def cdn_upload(self, url, ciphertext):
        self.calls += 1
        if self.calls <= self.fail_times:
            from gateway.ilink import CdnClientError, ILinkError
            exc = CdnClientError if self.status < 500 else ILinkError
            raise exc(f"CDN {self.status}")
        self.uploaded_url, self.uploaded_ct = url, ciphertext
        return "DL-PARAM"


def test_build_cdn_urls():
    dl = build_cdn_download_url("a b&c")
    assert dl.startswith(CDN_BASE_URL + "/download?encrypted_query_param=")
    assert "a%20b%26c" in dl     # 特殊字符必须 urlencode
    up = build_cdn_upload_url("P", "fk")
    assert up == f"{CDN_BASE_URL}/upload?encrypted_query_param=P&filekey=fk"


async def test_upload_media_success(tmp_path):
    import hashlib
    f = tmp_path / "shot.png"
    f.write_bytes(_PNG_BYTES)
    fake = FakeUploadILink()
    up = await upload_media(fake, str(f), "u@im.wechat", "TOKEN", None)
    assert up.download_param == "DL-PARAM"
    assert len(up.aes_key) == 16 and len(up.filekey) == 32
    assert up.size_cipher == pkcs7_padded_size(len(_PNG_BYTES))
    # getuploadurl 请求字段（spec §2.2）
    kw = fake.got
    assert kw["media_type"] == 1 and kw["to_user_id"] == "u@im.wechat"
    assert kw["rawsize"] == len(_PNG_BYTES)
    assert kw["rawfilemd5"] == hashlib.md5(_PNG_BYTES).hexdigest()
    assert kw["filesize"] == pkcs7_padded_size(len(_PNG_BYTES))
    assert kw["no_need_thumb"] is True and len(kw["aeskey"]) == 32
    # 上传的是密文且可用 aeskey 解回
    assert fake.uploaded_ct != _PNG_BYTES
    assert aes_ecb_decrypt(fake.uploaded_ct, up.aes_key) == _PNG_BYTES
    # upload_param 形态时 URL 是拼接的（spec §2.2 第 3 步）
    assert "upload" in fake.uploaded_url and "filekey=" in fake.uploaded_url


async def test_upload_media_retries_5xx_then_ok(tmp_path):
    f = tmp_path / "a.png"; f.write_bytes(_PNG_BYTES)
    fake = FakeUploadILink(fail_times=2, status=503)
    up = await upload_media(fake, str(f), "u", None, None)
    assert up.download_param == "DL-PARAM" and fake.calls == 3


async def test_upload_media_4xx_no_retry(tmp_path):
    from gateway.ilink import CdnClientError
    import pytest
    f = tmp_path / "a.png"; f.write_bytes(_PNG_BYTES)
    fake = FakeUploadILink(fail_times=1, status=403)
    with pytest.raises(CdnClientError):
        await upload_media(fake, str(f), "u", None, None)
    assert fake.calls == 1          # 立败，未重试


async def test_upload_media_image_too_large(tmp_path):
    import pytest
    f = tmp_path / "big.png"; f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_IMAGE_BYTES)
    with pytest.raises(MediaError):
        await upload_media(FakeUploadILink(), str(f), "u", None, None)


class FakeDownloadILink:
    def __init__(self, ciphertext: bytes):
        self._ct = ciphertext
        self.got_url = ""

    async def cdn_download(self, url):
        self.got_url = url
        return self._ct


async def test_download_inbound_image_full_url_priority(tmp_path):
    key = secrets.token_bytes(16)
    ct = aes_ecb_encrypt(_PNG_BYTES, key)
    import base64
    item = {"aeskey": key.hex(),
            "media": {"encrypt_query_param": "EQ", "full_url": "https://cdn/f"}}
    fake = FakeDownloadILink(ct)
    path = await download_inbound_image(fake, item, tmp_path / "in")
    assert fake.got_url == "https://cdn/f"            # full_url 优先
    assert Path(path).read_bytes() == _PNG_BYTES
    assert sniff_image(Path(path).read_bytes()) == "png"
    assert Path(path).parent == (tmp_path / "in")     # 落在 dest_dir


async def test_download_inbound_image_param_url(tmp_path):
    key = secrets.token_bytes(16)
    ct = aes_ecb_encrypt(b"GIF89a" + secrets.token_bytes(8), key)
    item = {"media": {"encrypt_query_param": "EQ PARAM", "aes_key": base64.b64encode(key).decode()}}
    fake = FakeDownloadILink(ct)
    path = await download_inbound_image(fake, item, tmp_path / "in")
    assert fake.got_url.startswith(CDN_BASE_URL + "/download?encrypted_query_param=")
    assert Path(path).name.endswith(".gif")


async def test_download_inbound_image_rejects_garbage(tmp_path):
    import pytest
    key = secrets.token_bytes(16)
    # 非 PNG/JPEG/GIF/WebP 明文（解密成功但 sniff 拒绝）
    ct = aes_ecb_encrypt(b"not an image at all.......", key)
    fake = FakeDownloadILink(ct)
    with pytest.raises(MediaError):
        await download_inbound_image(fake, {"aeskey": key.hex(), "media": {}},
                                     tmp_path / "in")
    # 无 CDN 引用
    with pytest.raises(MediaError):
        await download_inbound_image(fake, {"media": {}}, tmp_path / "in")


# ---- M5B：文件/语音/视频协议层 ----

def test_parse_media_aes_key_two_forms():
    import base64 as b64
    key16 = bytes(range(16))
    hex32 = key16.hex()
    # 形态一：base64(raw16B)
    assert parse_media_aes_key({"aes_key": b64.b64encode(key16).decode()}) == key16
    # 形态二：base64(hex32 ASCII)——文件/语音/视频的实际形态
    assert parse_media_aes_key({"aes_key": b64.b64encode(hex32.encode()).decode()}) == key16
    # 缺字段/坏 base64
    import pytest
    from gateway.media import MediaError
    with pytest.raises(MediaError):
        parse_media_aes_key({})
    with pytest.raises(MediaError):
        parse_media_aes_key({"aes_key": "!!!not-base64!!!"})


async def test_upload_media_file_type_body(tmp_path):
    """media_type=3：getuploadurl body 带正确类型与 100MB 上限（非图片 20MB）。"""
    import secrets as _s
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 " + _s.token_bytes(64))

    class Fake:
        async def getuploadurl(self, **kw):
            self.kw = kw
            return {"upload_full_url": "https://cdn/up"}

        async def cdn_upload(self, url, ct):
            return "DL-PARAM"

    fake = Fake()
    up = await upload_media(fake, str(f), "u", None, None, media_type=3)
    assert fake.kw["media_type"] == 3
    assert up.size_raw == f.stat().st_size            # file 条 len 用明文大小
    assert up.size_cipher == (up.size_raw + 16) // 16 * 16
    item = build_file_item(up, "doc.pdf")
    assert item["type"] == 4
    fi = item["file_item"]
    assert fi["file_name"] == "doc.pdf"
    assert fi["len"] == str(up.size_raw)              # 明文大小十进制字符串
    import base64 as b64
    assert fi["media"]["aes_key"] == b64.b64encode(up.aes_key.hex().encode()).decode()
    assert fi["media"]["encrypt_query_param"] == "DL-PARAM"


def test_build_video_item_shape():
    up = UploadedMedia(filekey="f" * 32, download_param="P", aes_key=bytes(16),
                       size_cipher=1234, size_raw=1200)
    item = build_video_item(up)
    assert item["type"] == 5
    vi = item["video_item"]
    assert vi["video_size"] == 1234                   # 密文数字
    assert set(vi) == {"media", "video_size"}         # 不填 thumb_*（官方同构）
    assert vi["media"]["encrypt_type"] == 1


async def test_upload_media_video_over_image_limit_ok(tmp_path):
    """视频/文件走 100MB 上限（图片 20MB 语义不变）——大文件 body 正常。"""
    class Fake:
        async def getuploadurl(self, **kw):
            return {"upload_param": "p"}

        async def cdn_upload(self, url, ct):
            return "DL"

    f = tmp_path / "big.mp4"
    f.write_bytes(b"\x00" * (21 * 1024 * 1024))       # 21MB：超图片限但低于 100MB
    up = await upload_media(Fake(), str(f), "u", None, None, media_type=2)
    assert up.size_raw == 21 * 1024 * 1024


class Fake:
    """getuploadurl/cdn_upload 最小桩（供下方超限测试：超限在网络调用之前
    抛出，桩方法不会被触达——若实现错误发起调用，此处无对应行为可见）。"""

    async def getuploadurl(self, **kw):
        return {"upload_param": "p"}

    async def cdn_upload(self, url, ct):
        return "DL"


async def test_upload_media_file_too_large(tmp_path):
    import pytest
    from gateway.media import MediaError
    f = tmp_path / "huge.bin"
    f.write_bytes(b"\x00" * (MAX_FILE_BYTES + 1))
    with pytest.raises(MediaError):
        await upload_media(Fake(), str(f), "u", None, None, media_type=3)


async def test_download_inbound_media(tmp_path):
    import base64 as b64
    import secrets as _s
    from gateway.media import aes_ecb_encrypt
    key = _s.token_bytes(16)
    raw = b"whatever-bytes-silk-or-pdf" + _s.token_bytes(32)
    media = {"encrypt_query_param": "EQ",
             "aes_key": b64.b64encode(key.hex().encode()).decode()}

    class Fake:
        async def cdn_download(self, url):
            assert "EQ" in url
            return aes_ecb_encrypt(raw, key)

    p = await download_inbound_media(Fake(), media, tmp_path, "file", "pdf")
    from pathlib import Path
    f = Path(p)
    assert f.parent == tmp_path and f.name.startswith("file-") and f.suffix == ".pdf"
    assert f.read_bytes() == raw                       # 落盘内容 = 解密明文


async def test_download_inbound_media_no_ref(tmp_path):
    import pytest
    from gateway.media import MediaError

    class NoCallFake:
        pass

    with pytest.raises(MediaError):
        await download_inbound_media(NoCallFake(), {}, tmp_path, "voice", "silk")

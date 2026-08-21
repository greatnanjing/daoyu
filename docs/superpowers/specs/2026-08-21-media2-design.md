# 刀鱼 M5B：媒体二期（文件双向 + 语音入站 + 视频入站）设计

- **日期**: 2026-08-21
- **状态**: 设计已确认（brainstorm 对话结论沉淀），待实现
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md) / [M3 媒体 spec](2026-08-19-m3-media-design.md)
- **协议事实源**: [.superpowers/sdd/media2-research.md](../../../.superpowers/sdd/media2-research.md)（@tencent-weixin/openclaw-weixin@2.4.6 src 逐行佐证；本 spec 引用其行号）
- **背景**: M5A 通知通道完成后按「通知 → 媒体 → 输入」顺序推进第二项。范围经 brainstorm 选定：**文件双向 + 语音入站（转写优先）+ 视频入站存盘；语音/视频出站以媒体条替代文件下载体验——send_file 按扩展名三路由（方案 A）**。

---

## 1. 背景与决策记录

| 问题 | 结论 |
|---|---|
| 类型范围 | 文件双向、语音入站、视频入站。语音出站不做专用条（media_type=4 官方零实现、服务端接受性未实证）——音频经 send_file 走文件条（官方同款模式） |
| 出站工具形态 | **方案 A：send_file 按扩展名三路由**（官方 `sendWeixinMediaFile` 同构，send-media.ts L17-72）——image 扩展名→复用 M3 图片链路（`_send_image`）；video 扩展名（mp4/mov/webm/mkv/avi）→ video 条（media_type=2）；其余 → file 条（media_type=3）。send_image 保留不动（M3 已验收契约 + magic bytes 校验） |
| 语音入站 | **`voice_item.text` 服务端转写非空 → 直接并入 text_parts 当用户文字**（官方 inbound.ts L191-194 同构，零解码成本）；text 空 → 下载 SILK 存盘保底 + ⚠️ 回执「语音未能转写（已存档），请补发文字」，**不建任务**（Claude 解不了 SILK，建任务无价值） |
| 文件/视频入站 | 下载存盘 → 「媒体入站即对话」（M3 图片同构）：file 带 `file_name` 原始名入 prompt；video 条无文件名字段（types.ts L136-145 只有 media），存盘扩展名 .mp4、prompt 提示可用 ffmpeg 抽帧 |
| 大小上限 | 图片保持 20MB（M3 收紧不变）；文件/语音/视频入站与出站 **100MB**（协议全局常量，media-download.ts L12） |
| 出站显示名 | file 条 `file_name` = basename（官方 send.ts L288 同构）——outbound 复制**保留原名**（非随机名），basename() 归一化防路径穿越 |
| 上传函数 | `upload_image` 泛化为 `upload_media(..., media_type=1)`（默认参向后兼容）；三 media_type 共用管线（官方 upload.ts L63-122 同构） |
| outbox kind | **单值 `kind='file'`**——MCP 层 image 扩展名直接写既有 kind='image' 行；outbound kind='file' 投递时按扩展名再分 video/file 条。两层路由共用同一扩展名常量表 |
| 架构 | M3 媒体模块直接扩展（media.py / app.py / outbound.py / approval_mcp.py / ilink.py），零新模块 |

## 2. 总体架构

```
入站：item_list 遍历（app.py handle_inbound）
  ├─ type==2 image（M3 既有）
  ├─ type==3 voice ─┬─ text 非空 → text_parts（当用户文字）
  │                 └─ text 空  → 下载 SILK 存盘 + ⚠️ 回执，不建任务
  ├─ type==4 file  → 下载存盘（随机名+原扩展名）→ prompt 带原始名+大小
  └─ type==5 video → 下载存盘（.mp4）→ prompt 提示 ffmpeg 抽帧
       ↓ 混合消息按 M3 模式拼接，建任务走既有 worker 池

出站：send_file(path, caption)（MCP，四档恒装）
  ├─ image 扩展名 → _send_image（M3 既有，kind='image'）
  └─ 其余 → 校验+复制 outbound（保留 basename）→ outbox kind='file'
       → outbound 投递：upload_media(media_type=2|3) → caption 文本条 → video/file 条
```

**高危协议事实**（研究 C 节，实现必须分常量域）：

- 入站 item type：VOICE=3 / FILE=4 / VIDEO=5；出站 getuploadurl media_type：VIDEO=2 / FILE=3 / VOICE=4——**同名不同值，两套编号无对应关系**。
- 入站 aeskey：语音/文件/视频仅 `media.aes_key`（base64(hex32 ASCII) 形态，`parseAesKey` 双形态的后半），无图片的 hex 优先字段；`full_url` 优先已在 M3 实现（[gateway/media.py:162-166](../../../gateway/media.py#L162-L166)，研究 I.8 该条与代码不符，以代码为准）。
- 条目大小字段三处语义不一致（照抄官方）：image 条 `mid_size`=密文数字；video 条 `video_size`=密文数字；file 条 **`len`=明文大小十进制字符串**。
- `no_need_thumb=true` 恒传（语义=「别返回缩略图上传参数」，不是「发不了」）；video 条不填 thumb_media 官方生产在用。

## 3. 组件设计

### 3.1 `gateway/media.py`（协议层扩展）

| 项 | 设计 |
|---|---|
| 常量 | `ITEM_TYPE_VOICE/FILE/VIDEO = 3/4/5`（入站）；`MEDIA_TYPE_VIDEO/FILE = 2/3`（出站）；`IMAGE_EXTS = {png,jpg,jpeg,gif,webp,bmp}`；`VIDEO_EXTS = {mp4,mov,webm,mkv,avi}`（对齐官方 mime.ts）；`MAX_FILE_BYTES = 100MB` |
| `parse_media_aes_key(media: dict) -> bytes` | 单形态解码：`media.aes_key` base64 → 16B 直接用 / 32B ASCII hex 再解码（`parse_inbound_aes_key` 内部逻辑抽出，图片双形态路径不变） |
| `download_inbound_media(ilink, media: dict, dest_dir, ext: str) -> str` | file/voice/video 共用：full_url 优先 → GET 密文 → 解密 → 100MB 上限 → 随机名 + `.{ext}` 落盘（复用 `download_inbound_image` 的 URL/解密/落盘骨架） |
| `upload_media(ilink, path, to_user, token, base_url, media_type=1) -> UploadedMedia` | `upload_image` 泛化：默认参 media_type=1，函数体不变（`filesize` 密文 ceil 公式三类型共用）；100MB 校验对 media_type≥2 生效（图片路径保持 20MB 既有校验） |

### 3.2 入站路由（[gateway/app.py](../../../gateway/app.py) `handle_inbound`）

item_list 遍历扩展四分支（与 M3 图片分支并列），混合消息拼接规则不变：

- **voice**：`text = (item.voice_item or {}).get("text")` 非空 → `text_parts.append(text)`；空 → `download_inbound_media(..., ext="silk")` 存档 + `db.enqueue` ⚠️ 回执，该 item 不参与建任务（`encode_type` 八值非恒 silk——存盘内容为解密后原始字节，扩展名统一 `.silk` 仅作提示）。
- **file**：`download_inbound_media(..., ext=<file_name 后缀或 bin>)` → prompt 行 `[用户发来文件 {file_name}（{MB:.1f}MB），已保存到 {path}，请查看处理]`。
- **video**：`download_inbound_media(..., ext="mp4")` → prompt 行 `[用户发来视频，已保存到 {path}，请查看处理（如需看内容可用 ffmpeg 抽帧，未装则如实告知）]`。
- 下载失败/超限：⚠️ 回执、该 item 跳过（M3 既有模式）；纯媒体全失败不建任务（M3 既有守卫覆盖）。
- `messages.media_path` 记首个成功媒体路径（M3 既有语义，多文件不扩展）。

### 3.3 MCP 工具 `send_file`（[worker/approval_mcp.py](../../../worker/approval_mcp.py)）

- 参数 `path: str, caption: str = ""`；校验存在 + ≤100MB；扩展名路由：`IMAGE_EXTS` → 调既有 `_send_image`（返回其确认文本）；否则复制 `data/media/outbound/<随机名>.<原扩展名>` → 写 outbox `kind='file'` 行（media_path=dest、caption）→ 纯文本确认（含最终条目形态：视频条/文件条）。
- `DAOYU_TOOLS` 装配：strict = `approve,send_image,send_file,notify`，其余 = `send_image,send_file,notify`（runner 装配点 + 三处测试断言更新）。
- `claude/settings.json` 加 allow `mcp__daoyu__send_file`；bg 无 MCP 不变（回执文案既有明示）。

### 3.4 出站投递（[gateway/outbound.py](../../../gateway/outbound.py) + ilink.py）

- `kind='file'` 分支：按 `media_path` 扩展名——`VIDEO_EXTS` → `upload_media(media_type=2)` + video 条；其余 → `upload_media(media_type=3)` + file 条。每条仍「caption 文本条（现有 `_send`）→ 媒体条」两步（官方 sendMediaItems 模式），`_respect_interval`/`_sent_today` 计数照走；上传失败整行重试（M3 既有语义）。
- 条目构造（[gateway/ilink.py](../../../gateway/ilink.py) `send_image_message` 泛化为 `send_media_message(to_user, ctx, uploaded, item, token, base_url)`，item 由调用方构造——改全部调用点，内部 API 无外部消费者）：
  - video 条：`{"type": 5, "video_item": {"media": {encrypt_query_param, aes_key: base64(hex32 ASCII), encrypt_type: 1}, "video_size": <密文大小>}}`
  - file 条：`{"type": 4, "file_item": {"media": {...同上...}, "file_name": <basename>, "len": str(<明文大小>)}}`

### 3.5 schema

无变更（outbox `kind/media_path/caption` 三列 M3 已有；messages.media_path 已有）。`data/media/inbound|outbound` 清理已被 `media_retention_days` 覆盖（M2 收尾批）。

## 4. 测试策略

| 层 | 内容 |
|---|---|
| 协议单测 | `parse_media_aes_key`（双形态/坏值）；`upload_media` media_type=2/3 body 断言（aioresponses）；video/file 条目构造（大小字段三种语义逐一断言：mid_size 密文/video_size 密文/len 明文字符串）；扩展名路由表 |
| 入站路由 | voice text 并入文本（建任务 prompt 断言）；voice 无 text 存档+回执不建任务；file 下载+原始名/大小入 prompt；video 下载+抽帧提示；混合消息拼接；下载失败/超限回执；msg_id 去重覆盖新类型 |
| MCP | `send_file` 子进程往返：image 扩展名转 `_send_image`（kind='image' 行）、video 扩展名（kind='file'）、普通文件、不存在/超限；`DAOYU_TOOLS` 四档串 |
| 出站 | kind='file' 两分支：caption+媒体条两条发送顺序、video/file item 结构断言、上传失败整行重试、文件丢失死信 |
| E2E | fake iLink 加 getuploadurl media_type 断言（tests/test_media_e2e.py 扩展）：fake claude 调 send_file → 全链路 sendmessage item 结构断言 |

## 5. 风险与真机验收点

| 风险 | 缓解 |
|---|---|
| 出站 video/file 条真机形态（官方生产在用但 daoyu 未实证） | 实现后真机验收：微信发 Claude 生成的小 mp4 与 pdf，核对播放/下载体验 |
| 100MB 大文件上传（密文 POST 内存整读） | 真机传一次大文件；失败走 outbox 重试既有链路 |
| 无缩略图视频微信端渲染 | 真机验收项（官方同形态生产在用，风险低） |
| `voice_item.text` 转写缺失率未知 | 无 text 路径已兜底（存档+回执）；真机观察出现率再决定是否三期上 SILK 解码 |

## 6. 明确不做

- SILK 解码（Python 选型未实测；text 转写兜住主场景，无 text 存档即止）
- 语音出站专用条（media_type=4 官方零实现；音频=文件条）
- 引用消息携带媒体（ref_msg.message_item，M3 未处理路径，三期留档）
- 入站图片明文下载兜底（无 aes_key 场景，低频）
- 文件类型白名单 / 病毒扫描（任意文件合法，安全模型同 M3 已论证：外泄面仅用户本人微信）
- 新配置键（100MB 硬上限跟协议；media 清理已覆盖）

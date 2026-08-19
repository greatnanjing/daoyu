# 刀鱼 M3 媒体收发设计（图片双向）

- **日期**: 2026-08-19
- **状态**: 已实现并真机验收通过（2026-08-19，§5 五项全过；验收期实测勘误登记于 §2.2 / §3.4 / §5）
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md)；实现计划另见 plans/

---

## 1. 背景与决策记录

M3 按 PRD 含三个相对独立子系统，本 session 拍板的切入与去留：

| 子系统 | 决策 |
|---|---|
| 媒体收发 | **本次先做**（M3 核心，PRD 验收标准"微信收到 Claude 截的原图"） |
| OCR/视觉 MCP | **暂缓**——媒体入站打通后 Claude 用 Read 工具原生看图（模型自带视觉），tesseract-ocr/ai-vision 大概率不再需要；待媒体上线后按实际体验评估去留 |
| `/mcp` 启停与 `/config` 写入 | **后续独立小项目**，不混入本 spec |

本 spec 范围内的澄清结论：

| 问题 | 结论 |
|---|---|
| 媒体类型 | 只做图片（PNG/JPEG/WebP/GIF），不做语音/文件/视频 |
| 出站发图触发 | MCP 工具 `send_image`（复用审批 MCP 的临时合并 mcp config + stdio server 架构） |
| 入站体验 | 发图即对话：图片落盘后自动建任务，Claude Read 看图回应 |
| 图文合并 | **不做时间窗合并**——Claude 会话上下文跨轮次连续，拆两轮效果等价（YAGNI） |
| 架构 | 方案 A：独立 `gateway/media.py` 模块，ilink.py 只加薄 API，outbox 加 kind 列 |

## 2. iLink 媒体协议（桌面调研结论）

**来源**（字段级以下述为准）：官方 npm 包 `@tencent-weixin/openclaw-weixin` **v2.4.6** dist 源码（`cdn/`、`media/`、`messaging/`、`api/` 模块，2026-08-19 实际拉包分析）；辅证：[openclaw-weixin 协议解析文档](https://github.com/hao-ji-xing/openclaw-weixin/blob/main/weixin-bot-api.md)（基于 1.0.2 的逆向分析，流程概述层级）。

### 2.1 类型常量（官方包 `api/types.js`）

```
UploadMediaType:  IMAGE=1  VIDEO=2  FILE=3  VOICE=4
MessageItemType:  TEXT=1  IMAGE=2  VOICE=3  FILE=4  VIDEO=5
                   （另有 TOOL_CALL_START=11 / TOOL_CALL_RESULT=12，本项目不用）
```

**关键**：入站图片消息的 `message_type` 仍是 **1**（用户消息，与文本同）；图片体现在 `item_list[].type == 2`。→ daoyu 现有 [gateway/app.py:30](../../gateway/app.py) 的 `message_type != 1` 过滤**不会**丢图片（源码推断，真机复核项 5.1）。

### 2.2 出站发图（bot → 用户）

官方包 `cdn/upload.js` + `cdn/cdn-upload.js` + `messaging/send.js` 的完整流程：

1. 读文件 → `rawsize`、`rawfilemd5`（MD5 hex）、`filesize = ceil((rawsize+1)/16)*16`（PKCS7 填充后密文大小）、`filekey = random16B.hex()`、`aeskey = random16B`
2. `POST /ilink/bot/getuploadurl`，body：
   ```json
   {"filekey": "<hex32>", "media_type": 1, "to_user_id": "<user>",
    "rawsize": <明文字节数>, "rawfilemd5": "<hex>", "filesize": <密文字节数>,
    "no_need_thumb": true, "aeskey": "<hex32>", "base_info": {...}}
   ```
   （`thumb_*` 字段在 `no_need_thumb: true` 时可省略）
3. 响应给 `upload_full_url`（优先使用）或 `upload_param`（回退拼接 `{CDN}/upload?encrypted_query_param={upload_param}&filekey={filekey}`）；两者全无 = 失败
4. **POST**（非 PUT）AES-128-ECB 密文（`Content-Type: application/octet-stream`）到上传 URL → 成功从响应头取 **`x-encrypted-param`**（= 后续发送用的 download param）；**4xx 客户端错误立即中止，5xx 服务端错误重试 ≤3 次**
5. `POST /ilink/bot/sendmessage`，item：
   ```json
   {"type": 2, "image_item": {
      "media": {"encrypt_query_param": "<x-encrypted-param>",
                 "aes_key": "<base64(hex32 ASCII)>", "encrypt_type": 1},
      "mid_size": <密文字节数>}}
   ```
   message_type=2（BOT）、message_state=2（FINISH）、client_id、context_token 照常。
   **caption 与图是两条独立 sendmessage**（官方实现每个 item 单独一条消息，`item_list` 恒单元素）——照抄。
   > **实测勘误（2026-08-19 真机验收）**：`aes_key` = base64(hex32 ASCII)——即把加密用 random16B key 的 hex 表示（32 个 ASCII 字符）再做 base64（官方 send.ts 形态）。本 spec 原写的 `base64(raw16B)` 是错的：真机传 raw16B 的 base64 微信端收图空白（上传/发送链路全 200 但图打不开）。

CDN 基址：`https://novac2c.cdn.weixin.qq.com/c2c`（官方包常量 `CDN_BASE_URL`，可配置覆盖；本项目按常量用）。

### 2.3 入站收图（用户 → bot）

官方包 `media/media-download.js` + `cdn/pic-decrypt.js`：

- `item_list[].type == 2`，`image_item` 结构：
  - aeskey 二选一：`image_item.aeskey`（**hex 字符串**，优先）或 `image_item.media.aes_key`（base64）；实现上 hex → base64 后统一处理
  - `image_item.media.encrypt_query_param`（CDN 下载参数）
  - `image_item.media.full_url`（可选完整下载 URL，**存在则优先**）
- 下载：`GET {CDN}/download?encrypted_query_param=<urlencoded>`（或 full_url）→ 密文 body
- 解密：AES-128-ECB + PKCS7。key 解析有两种野外形态：`base64(raw16B)`（图片）与 `base64(hex32 字符串)`（文件/语音/视频）——本项目只做图片，按第一种，但解析函数兼容第二种（官方 `parseAesKey` 行为）
- 官方包媒体大小上限 100MB（`WEIXIN_MEDIA_MAX_BYTES`）；本项目收紧至 **20MB**

### 2.4 对 daoyu 现有代码的影响

| 位置 | 现状 | 影响 |
|---|---|---|
| [gateway/app.py:39](../../gateway/app.py) | 只取 `item_list[0].text_item.text` | 图片消息取到空文本 → 改为遍历 item_list 分类处理 |
| [gateway/ilink.py](../../gateway/ilink.py) | 仅文本 sendmessage | 加 4 个薄方法（§3.2） |
| [common/db.py](../../common/db.py) | messages/outbox 无媒体列 | 加列迁移（§3.3） |
| [gateway/outbound.py](../../gateway/outbound.py) | 纯文本投递 | 加 kind 分支（§3.2） |
| [worker/approval_mcp.py](../../worker/approval_mcp.py) | 审批专用 server | 升级为 daoyu 统一 server（§3.4） |

**新依赖**：`cryptography`（AES-128-ECB；Python 标准库无 AES，现有依赖仅 aiohttp）。

## 3. 设计

### 3.1 范围

- **入站**：用户微信发图 → CDN 下载解密 → 落盘 `data/media/inbound/` → messages 带 `media_path` → 自动建任务（prompt 模板 `[用户发来图片，已保存到 {path}，请查看并回应]`）→ Claude Read 看图回应。
- **出站**：Claude 调 MCP 工具 `send_image(path, caption)` → 校验+复制到 `data/media/outbound/` → 写 outbox 媒体行 → 出站协程投递时**整链路现做**（上传 → caption 文本条 → 图片条）。
- **格式**：PNG / JPEG / WebP / GIF（magic bytes 白名单：`\x89PNG`、`\xFF\xD8`、`RIFF....WEBP`、`GIF8`）；**上限 20MB**。
- **明确不做**：语音/文件/视频、图文时间窗合并、OCR/视觉 MCP（另评估）、`/mcp`/`/config` 写入（另项目）。

### 3.2 模块与数据流

```
入站：iLink getupdates → handle_inbound（item_list 遍历）
        type==1 文本 → 现路径不变
        type==2 图片 → media.download_inbound_image() ─ 失败 → ⚠️ 回执（不建任务）
                              └ 成功 → data/media/inbound/<随机名>.<ext>
                                 → messages.media_path 落盘
                                 → 建任务（prompt 带图路径）

出站：claude 子进程 ─ MCP send_image(path, caption)
        → 校验（存在/≤20MB/magic bytes）→ 复制到 data/media/outbound/<随机名>
        → outbox 行（kind=image, media_path, caption）        [SQLite 跨进程]
        → OutboundLoop：kind==image → media.upload_image()（getuploadurl+CDN POST）
                        → caption 非空先发文本条 → ilink.send_image_message()
                        失败重试 = 整链路重做（不缓存 downloadParam，规避过期）
```

**`gateway/media.py`（新）**——纯逻辑，不依赖 gateway 其他模块，可独立单测：

| 函数 | 职责 |
|---|---|
| `aes_ecb_encrypt(buf, key16)` / `aes_ecb_decrypt(buf, key16)` | cryptography，ECB + PKCS7 |
| `pkcs7_padded_size(n)` | `ceil((n+1)/16)*16` |
| `parse_inbound_aes_key(image_item) -> bytes16` | 双形态（hex 顶层 / base64 media），hex 优先 |
| `sniff_image(buf) -> ext` | magic bytes 白名单 → 扩展名；不识别抛异常 |
| `upload_image(ilink, path, to_user, token, base_url) -> UploadedImage` | md5 → 随机 filekey/aeskey → `ilink.getuploadurl` → `ilink.cdn_upload`（密文 POST）→ `UploadedImage{filekey, download_param, aes_key, size_cipher}`；4xx 立败 5xx 重试≤3 |
| `download_inbound_image(ilink, image_item, dest_dir) -> path` | URL 拼装（full_url 优先）→ `ilink.cdn_download` → 解密 → sniff 校验 → 随机名落盘 |

**`gateway/ilink.py`（薄扩展，保持纯协议层）**：`getuploadurl(body)`、`cdn_upload(url, ciphertext)`（POST octet-stream，返回 `x-encrypted-param`）、`cdn_download(url)`（GET 密文）、`send_image_message(to_user, ctx, uploaded, token, base_url)`（sendmessage 媒体 item，容错误误处理照抄现有 `sendmessage`：失败返回 False 交 outbox 重试）。

**`gateway/outbound.py`**：`_drain_once` 按 `item.kind` 分支。image 分支：`upload_image` → caption 文本条（走现有 `_send`）→ `send_image_message`，每条间隔照走 `_respect_interval`；`_sent_today` 计数每 outbox 行 +1（与文本行同语义）。文件丢失（outbound 目录被清）→ 上传直接抛 → `mark_send_failed`，重试耗尽进死信告警（现机制）。

### 3.3 schema 变更（`common/db.py`）

```sql
ALTER TABLE messages ADD COLUMN media_path TEXT;            -- 入站图落盘路径，可空
ALTER TABLE outbox    ADD COLUMN kind TEXT NOT NULL DEFAULT 'text';
ALTER TABLE outbox    ADD COLUMN media_path TEXT;           -- 出站图路径，可空
ALTER TABLE outbox    ADD COLUMN caption TEXT;              -- 图配文，可空
```

幂等实现：`PRAGMA table_info` 检查列存在再 ADD（比 M2 sessions 整表搬迁简单得多）。`db.enqueue` 保持现签名（默认 text），新增 `db.enqueue_media(user, media_path, caption)`。

### 3.4 MCP server（`worker/approval_mcp.py` → daoyu 统一 server）

- 一个 stdio JSON-RPC server，暴露两个工具：`approve`（审批，现有）+ `send_image(path: str, caption: str = "")`（新）。
- 工具清单按 env `DAOYU_TOOLS` 装配（逗号分隔）：strict 档 = `approve,send_image`，auto/bypass/plan = `send_image`。
- 临时合并 mcp config 机制从"仅 strict 档合并"扩为"**四档都合并** daoyu 条目"（`daoyu-mcp-` 前缀临时文件、任务结束即删、启动清扫，全部复用现机制）。
- `send_image` 行为：校验（存在/≤20MB/magic bytes）→ 复制到 `data/media/outbound/<随机名>.<ext>` → 写 outbox 媒体行（to_user 经任务 env `DAOYU_TO_USER` 注入，照抄审批的 `DAOYU_DB`/`DAOYU_TASK_ID` 模式，见 [worker/runner.py:339-341](../../worker/runner.py)）→ 返回**纯文本确认**（普通工具返回，非审批，无需 behavior JSON）。
- `--bg` 任务**不装配**：> **实测勘误（2026-08-19 真机验收）**：`--mcp-config` 与 `--bg` 结构性不兼容——daemon 异步拉起 worker（客户端返回 ~1s 后才读 mcp config），临时文件在 run() 返回即删 → daemon "exit 1 before init" 100% 复现（daemon.log 三次三崩）；持久化文件 + 终态清理的替代方案其启动清扫会误删存活 bg 任务文件（gateway 重启后 bg 仍活着）。bg 会话因此无 MCP 工具（send_image 不可用），回执明示；CLI 修复该竞态后可再装回。
- 硬约束不变：server 键 `daoyu` 与工具引用严格一致；strict 审批 flag 语义不动。

### 3.5 安全

- **入站图 = 外部输入**：magic bytes 白名单 + 20MB 上限 + 随机文件名（路径无用户可控成分）+ 解密失败/格式不识别直接拒绝。
- **出站不做路径限制**：Claude 本就能 Read 宿主文件（auto 档），发图外泄面仅用户本人微信，与现有安全模型一致（YAGNI）；预算闸与权限档位对任务照常生效。
- **日志脱敏**：CDN 签名 URL 只记前 40 字符（官方 redact 先例）；aeskey 不进日志。
- `data/media/` 在 `data/`（已 gitignore）下，与 `daoyu.db` 同级；Claude 需能 Read 入站图（不在 deny 清单内，无需改动）。

## 4. 测试策略

| 层 | 内容 |
|---|---|
| 单测 | 加解密 roundtrip（含跨边界长度）、`pkcs7_padded_size`、`parse_inbound_aes_key` 双形态、`sniff_image` 四格式、`upload_image`（aioresponses mock getuploadurl/CDN POST，覆盖 4xx 立败/5xx 重试/缺 x-encrypted-param）、`download_inbound_image`（mock CDN GET，覆盖 full_url 优先/解密失败/坏 magic）、schema 加列迁移幂等 |
| 入站路由 | 图片消息 → 建任务 prompt 断言；图+文同条 item_list 拼合；下载失败回执；msg_id 去重覆盖图片重投 |
| MCP | `send_image` 协议往返（仿 approval 测试）：成功/文件不存在/超大/非图片；`DAOYU_TOOLS` 装配两档 |
| 出站 | outbox kind 分支：caption+图两条发送顺序、上传失败重试、文件丢失死信 |
| E2E | fake iLink 加媒体端点 + fake claude 调 send_image → 全链路断言 sendmessage 的 item 结构 |

## 5. 待实测清单（真机，实现完成后验收）

**✅ 全部验收通过（2026-08-19，生产服务器 + 微信真机）**：

1. ~~**入站 payload 采样**~~：✅ 真机发图采样，`message_type==1` + `item_list[].type==2` 与 §2.1 推断一致；双形态 aeskey 解析兼容真机 payload（`parse_inbound_aes_key` 三形态全保留）。
2. ~~**出站全链路**~~：✅ getuploadurl → CDN POST → sendmessage 真机成功，微信端收到原图（生产服务器验证）。**发现并修正 aes_key 形态**（见 §2.2 勘误：base64(hex32 ASCII)，非 base64(raw16B)）。
3. ~~**caption 呈现**~~：✅ caption 文本条先到、图片条后到（两条独立 sendmessage），微信端呈现正常。
4. ~~**生产服务器 依赖**~~：✅ `cryptography` 生产 venv 安装无坑。
5. ~~**微信压缩行为**~~：✅ 知识确认：用户端发图微信客户端会预压缩（非原图时）；CDN 链路本身不改变图片内容。

**验收期顺带落定的 bg 结论**（与媒体无直接关系，借真机环境实测）：三终态 `done`/`blocked`/`failed`、`--all` 必带、done 条目无输出字段、取结果恒 `--fork-session`、`--mcp-config` 与 `--bg` 结构性不兼容（见 §3.4 勘误）——详见 CLAUDE.md 硬性约束。

## 6. 实现顺序（writing-plans 细化基准）

1. `media.py` 加解密 + sniff + 单测（无网络依赖，先固化协议常量）
2. schema 加列迁移 + `enqueue_media` + 单测
3. `ilink.py` 薄方法 + 入站管线图片分支 + 路由测试
4. 出站 kind 分支 + 投递测试
5. MCP server 升级（统一 daoyu server + `DAOYU_TOOLS` 装配 + 四档合并）+ 测试
6. E2E + 文档更新（TRD §3.1 "仅支持文本"勘误、README 命令表、CLAUDE.md）
7. 真机验收（§5 清单，用户配合发图）

## 7. 实现后需同步的文档勘误

- TRD §3.1 "当前开源实现仅支持文本消息" → 更新为媒体协议已明（引用本 spec §2）
- PRD §7 范围外"媒体收发（二期）" → 移入已实现
- CLAUDE.md 当前状态节 M3 条目
- README 命令表（如无新增微信命令则只补媒体说明）

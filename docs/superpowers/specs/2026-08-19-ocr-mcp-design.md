# 刀鱼 M3 余项 B：OCR MCP（RapidOCR 本地封装）设计

- **日期**: 2026-08-19
- **状态**: 已确认（本 session 需求澄清与设计结论的沉淀）
- **配套文档**: [PRD.md](../../PRD.md) / [TRD.md](../../TRD.md) / [M3 media spec](2026-08-19-m3-media-design.md)（§1 划出的独立小项目之一）
- **前置**: [余项 A spec](2026-08-19-mcp-config-writable-design.md) 先行完成（mcp.json 平台无关化与 /mcp 启停是本项装载的干净前提）

---

## 1. 背景与决策记录

PRD FR-5 原计划「Tesseract 封装 + AI 视觉」，M3 media spec §1 暂缓（媒体入站打通
后 Claude 用 Read 原生看图）。本 session 用户确认：**偶有精确文本提取需求，做本地
OCR**。拍板：

| 问题 | 结论 |
|---|---|
| 引擎 | **RapidOCR**（`rapidocr-onnxruntime`）：pip 即装、无系统依赖、CPU 推理、模型约 50MB，中文质量接近 PaddleOCR（其模型的 onnx 移植）；Windows/Linux 通吃 |
| 弃选 | Tesseract（要装系统包 + chi_sim，两端各一套，中文质量弱）；云端 OCR API（违背本地诉求、按次计费、多 secret） |
| server 形态 | **独立 stdio server**（`worker/ocr_mcp.py`），不塞 daoyu server——审批/发图是控制面、OCR 是能力面，进程隔离；模型加载开销只在真调 ocr 时发生 |
| 装载方式 | runner 合并层动态注入（`daoyu-ocr` 条目，`sys.executable` + repo 绝对路径），**不进静态 mcp.json**（静态文件保持零路径/平台相关内容） |
| import 时机 | **lazy**：首次 tools/call(ocr) 才 import rapidocr（server 启动轻量，不拖累任务冷启动） |
| /mcp 列表呈现 | daoyu-ocr 显示为**系统条目**（标注「系统」，不可 off——它是能力面基础设施，同 daoyu 审批条目一样随任务恒装配） |
| 输入校验 | 读 bytes 失败 / 非 PNG/JPEG 图片（magic bytes 白名单，复用 [gateway/media.py](../../gateway/media.py) `sniff_image`）→ 明确报错，不浪费模型加载 |
| 返回形态 | 按行序纯文本（不带置信度，YAGNI）；中英自动混识（RapidOCR 默认行为，无 lang 参数） |

## 2. 设计

### 2.1 server 结构（照 [worker/approval_mcp.py](../../worker/approval_mcp.py) 先例）

`worker/ocr_mcp.py`——stdio JSON-RPC：

- 顶部 repo 根 sys.path 自举（子进程 `sys.path[0]` = worker/，import gateway.media
  需要 repo 根——approval_mcp.send_image 同款问题同款解）。
- stdin/stdout reconfigure UTF-8（Windows 管道 cp936 坑）。
- initialize / tools/list / tools/call / ping 分发；未知方法回空 result（先例）。
- tools/call 兜底 try/except → isError 文本（磁盘满等不击穿 server 进程，先例）。
- 无 DB 依赖、无任务 env 依赖（纯能力工具，不需要 DAOYU_DB/DAOYU_TASK_ID）。

**工具契约**：

```json
{"name": "ocr",
 "description": "识别图片中的文字（本地 RapidOCR，中英混识，返回按行文本）",
 "inputSchema": {"type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"]}}
```

返回：识别文本（多行 `\n` 连接）或 `识别失败: <原因>`（文件不存在 / 非图片 /
解码失败 / 引擎异常）。

### 2.2 引擎调用（lazy）

```python
_engine = None

def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR   # lazy：首次调用才加载
        _engine = RapidOCR()
    return _engine
```

- `_ocr(args)`：读 bytes → `sniff_image` 白名单（PNG/JPEG；GIF/WebP 动图非 OCR
  合理输入，白名单收紧为 sniff 的 png/jpg 两种）→ 临时文件（RapidOCR 收路径不收
  bytes；temp 目录随机名）→ `engine(path)` → 行文本拼接 → 删临时文件。
- 引擎返回结构按 rapidocr_onnxruntime 实际 API 处理（实现首步以包内签名/示例为准，
  不预设字段名——**待实测项 §5.1**）。

### 2.3 装载（runner 合并层）

- `_write_daoyu_mcp_config` 在合并 daoyu（审批/发图）条目之外，**恒注入**
  `daoyu-ocr` 条目：

```json
{"type": "stdio",
 "command": "<sys.executable 绝对路径>",
 "args": ["<repo>/worker/ocr_mcp.py"],
 "env": {}}
```

- 键名 `daoyu-ocr` 连字符合法（现有 mcp.json 的 `chrome-devtools` 同款形态）。
- **不受 disabled 过滤**：注入发生在静态清单读取之后，系统条目（daoyu 审批 +
  daoyu-ocr）不在 /mcp on/off 管辖。
- bg 任务无 MCP（M3 定论）——ocr 在 bg 不可用，与 send_image 同口径，回执已明示。
- `claude/settings.json` allow 追加 `mcp__daoyu-ocr__ocr`（M3 教训：acceptEdits
  不放行 MCP 工具、headless 无确认通道直接 deny）。

### 2.4 依赖

- `rapidocr-onnxruntime` 进 [pyproject.toml](../../pyproject.toml) `dependencies`
  （产品主功能，不做 optional；onnxruntime 随其传入）。
- 模型随 pip 包分发、无需联网下载（**待实测 §5.2 确认**；若需下载，部署文档写
  预热步骤）。

## 3. 测试策略

| 层 | 内容 |
|---|---|
| 单测（无真引擎） | monkeypatch `_get_engine` 返回 fake（固定行文本）——tools/call 协议往返、多行拼接、path 不存在、非 PNG/JPEG 输入拒绝、lazy 断言（模块 import 后 `rapidocr_onnxruntime` 不在 sys.modules——server 启动不加载） |
| 装配单测 | _write_daoyu_mcp_config 产物含 daoyu-ocr 条目（command = sys.executable 绝对路径）、disabled 过滤不影响系统条目 |
| settings | claude/settings.json allow 含 mcp__daoyu-ocr__ocr（断言防回归） |
| 真机冒烟 | 生产装依赖 → 微信发含文字图 + 「OCR 提取图中文字」→ 文字回微信；/mcp 列表显示 daoyu-ocr（系统） |

## 4. 验收清单（真机）

1. 生产服务器 venv `pip install rapidocr-onnxruntime` 无坑、import 成功。
2. 微信发图 + OCR 指令 → 识别文字回微信（中文图一张 + 英文图一张）。
3. `/mcp` 列表含 daoyu-ocr 系统条目；/mcp off 不影响它。
4. bg 任务回执明示无 MCP（口径不变，回归确认）。

## 5. 待实测清单（实现首步）

1. **RapidOCR Python API 形态**：`RapidOCR()(path)` 的返回结构（版本演进过
   `[box, text, score]` 列表等形态）——以装的版本实际签名为准。
2. **模型是否随包分发**：`pip install rapidocr-onnxruntime` 后离线可跑则确认；
   否则部署文档写模型预热。
3. **首次调用时延**：模型加载耗时（预期 1~3s）——若显著超预期，回执体验评估
   （Claude 侧等待，不阻塞微信）。

## 6. 实现后需同步的文档勘误

- PRD FR 表：「OCR（Tesseract 封装）」→ RapidOCR 本地封装；「AI 视觉」行核对
  （模型自带视觉已覆盖，确认删除或改注）
- TRD §11「OCR MCP 选型」行落定；§MCP 装载表（tesseract-ocr 默认行 → daoyu-ocr）
- CLAUDE.md：M2/M3 清单 MCP 相关行同步
- README：M2 边界「OCR/视觉 MCP 再评估」→ 已实现（daoyu-ocr）

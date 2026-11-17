# 可选离线资源

[English](README.md) | **简体中文**

仓库不包含词库或额外模型。`manifest.json` 记录已验证资源的相对路径、字节数与 SHA-256，用于迁移和一致性检查，不包含下载凭证。

| 资源 | 位置 | 缺少时的影响 |
|---|---|---|
| 中英 OCR | RapidOCR 安装包自带 | 需先正确安装运行依赖 |
| 日文识别模型 | `data/ocr/japan.onnx` | 不提供对应识别器 |
| 韩文识别模型 | `data/ocr/korean.onnx` | 不提供对应识别器 |
| 俄文识别模型 | `data/ocr/cyrillic.onnx` | 不提供对应识别器 |
| StarDict | `data/dictionaries/<词库目录>/` | 缺少对应离线词库 |
| JMdict SQLite | `data/dictionaries/jmdict/jmdict.db` | 不提供对应日语离线查询 |

中英之外的模型可从可信上游获取，需带 `character` 元数据并兼容 PP-OCR CTC `3×48×W` 输入。可通过设置页导入；不是任意 ONNX 模型均兼容。

StarDict 可通过设置 → 词典导入，要求 `.ifo`、`.idx`、`.dict` 等配套文件，也支持程序说明的压缩与同义词格式。

JMdict 原始 XML 不能直接改名为 `.db`。本程序读取 SQLite 表 `jmdict`，按 `key` 查询 `word`、`reading`、`gloss`、`pos`。当前仓库没有 XML 转换工具；需使用兼容数据库并保留 EDRDG 署名与许可。没有该数据库时仍可使用模型词典或其他 StarDict 词库。

如果本机已有清单对应的资源，可从另一份 ScreenGloss 安装目录迁移：

```powershell
.\.venv\Scripts\python.exe -B tools\import_resources.py --source "另一份 ScreenGloss 的根目录"
```

工具只复制清单中的 7 个资源，不复制配置、词卡、缓存或日志。先验证全部源文件的大小与哈希，再逐个复制并复核；目标存在不同内容时拒绝覆盖。目标资源位于被 Git 忽略的 `data/`。

校验已安装的清单资源：

```powershell
.\.venv\Scripts\python.exe -B tools\verify_resources.py
```

其他版本资源可以另行使用，但不会通过此清单的精确校验。校验失败不等于该版本模型或词库必然不可用。

资源来源与许可见 [第三方说明](../THIRD-PARTY.zh-CN.md)。本仓库不提供资源下载服务。

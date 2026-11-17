# 第三方组件

[English](THIRD-PARTY.md) | **简体中文**

本仓库只分发 ScreenGloss 源码与文档，不附带第三方运行环境、词库或模型二进制。下列组件由包管理器安装，授权与版权以对应版本的软件包为准。

| 组件 | 许可 | 用途 |
|---|---|---|
| PySide6 Essentials / Qt | LGPL-3.0 或商业许可，具体模块以 Qt 声明为准 | 图形界面 |
| ONNX Runtime | MIT | CPU 推理 |
| RapidOCR | Apache-2.0 | 检测、识别与字框 |
| NumPy | BSD-3-Clause，另含第三方声明 | 数组处理 |
| OpenCV | Apache-2.0，另含第三方组件 | 图像处理 |
| Pillow | HPND | 图像处理 |
| mss | MIT | 截图 |
| requests | Apache-2.0 | API 客户端 |
| pywin32 | PSF License，另含第三方声明 | 热键、SAPI 与快捷方式 |
| edge-tts | LGPLv3，srt_composer.py 为 MIT | 可选在线朗读 |

可选资源的来源与许可：

- [ECDICT](https://github.com/skywind3000/ECDICT)：上游项目使用 MIT；导入其他来源的 StarDict 时，须另行核对该份数据的来源与许可。
- [JMdict / EDRDG](https://www.edrdg.org/edrdg/licence.html)：CC-BY-SA 4.0，保留 Electronic Dictionary Research and Development Group 署名，转换数据遵循相应条件。
- [RapidOCR](https://github.com/RapidAI/RapidOCR) / PP-OCR 多语言模型：按实际模型发布来源保留许可；开发资源所用模型说明为 Apache-2.0，不能将此泛化为任意导入模型。

若另外发布包含依赖或资源的便携包，应随包保留对应的许可与署名。本项目 MIT 许可仅适用于原创代码。

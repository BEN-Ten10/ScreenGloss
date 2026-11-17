# Third-party components

**English** | [简体中文](THIRD-PARTY.zh-CN.md)

This repository distributes ScreenGloss source code and documentation, without third-party runtimes, dictionary data, or model binaries. The packages below are installed through a package manager; their respective versions determine the applicable copyright and license terms.

| Component | License | Purpose |
|---|---|---|
| PySide6 Essentials / Qt | LGPL-3.0 or commercial; refer to the specific Qt modules | UI |
| ONNX Runtime | MIT | CPU inference |
| RapidOCR | Apache-2.0 | Text detection and recognition |
| NumPy | BSD-3-Clause, with additional third-party notices | Arrays |
| OpenCV | Apache-2.0, with additional third-party components | Image processing |
| Pillow | HPND | Image processing |
| mss | MIT | Screen capture |
| requests | Apache-2.0 | HTTP |
| pywin32 | PSF License, with additional third-party notices | Windows integration |
| edge-tts | LGPLv3; srt_composer.py is MIT | Optional online speech |

Sources and licensing for optional resources:

- [ECDICT](https://github.com/skywind3000/ECDICT): the upstream project uses MIT. Check the specific license of StarDict data from other sources.
- [JMdict / EDRDG](https://www.edrdg.org/edrdg/licence.html): CC-BY-SA 4.0. Preserve Electronic Dictionary Research and Development Group attribution and follow the applicable conditions when redistributing converted data.
- [RapidOCR](https://github.com/RapidAI/RapidOCR) / PP-OCR multilingual models: retain the notices from the actual model source. The development resource notes identify the listed models as Apache-2.0; this does not apply automatically to other imported models.

When separately distributing a portable package containing dependencies or resources, include the corresponding licenses and attribution. ScreenGloss's MIT license covers only its original code.

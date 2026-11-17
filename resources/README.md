# Optional offline resources

**English** | [简体中文](README.zh-CN.md)

The repository contains no dictionaries or additional model weights. `manifest.json` records the relative paths, sizes, and SHA-256 hashes of verified resources for migration and consistency checks. It contains no download credentials.

| Resource | Location | Without it |
|---|---|---|
| Chinese/English OCR | Supplied by the RapidOCR package | Install the runtime dependencies first |
| Japanese recognizer | `data/ocr/japan.onnx` | The corresponding recognizer is unavailable |
| Korean recognizer | `data/ocr/korean.onnx` | The corresponding recognizer is unavailable |
| Russian recognizer | `data/ocr/cyrillic.onnx` | The corresponding recognizer is unavailable |
| StarDict | `data/dictionaries/<dictionary-folder>/` | That offline dictionary is unavailable |
| JMdict SQLite | `data/dictionaries/jmdict/jmdict.db` | The corresponding Japanese offline lookup is unavailable |

Obtain additional models from a trusted upstream source. They need `character` metadata and a compatible PP-OCR CTC `3 x 48 x W` input. Import them through settings; arbitrary ONNX models are not necessarily compatible.

Import StarDict dictionaries through Settings > Dictionary. The `.ifo`, `.idx`, and `.dict` files must be supplied together. Supported compressed and synonym formats are handled by the importer.

Do not rename raw JMdict XML to `.db`. The application queries a SQLite table named `jmdict`, using `key` to retrieve `word`, `reading`, `gloss`, and `pos`. No XML converter is included. Supply a compatible database and preserve EDRDG attribution and licensing. Model lookup or other StarDict dictionaries can be used without it.

If another local ScreenGloss installation has the exact manifest-listed resources, migrate them with:

```powershell
.\.venv\Scripts\python.exe -B tools\import_resources.py --source "path to another ScreenGloss installation"
```

Only the seven listed resources are copied. Settings, word cards, caches, and logs are excluded. All sources are checked for size and hash before copying; each copy is verified again. Conflicting destination content is not overwritten. Imported resources go into the Git-ignored `data/` directory.

Verify installed manifest-listed resources:

```powershell
.\.venv\Scripts\python.exe -B tools\verify_resources.py
```

Other resource versions may work but will not pass this exact manifest check. A mismatch does not necessarily mean that a different model or dictionary version is unusable.

See [third-party components](../THIRD-PARTY.md) for resource sources and licensing. This repository does not provide a resource download service.

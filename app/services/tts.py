"""Cancelable Windows SAPI and optional Edge-TTS pronunciation."""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import uuid

from PySide6.QtCore import QObject, Signal

from .. import config
from .logging_service import event


class Notifications(QObject):
    warning = Signal(str)


notifications = Notifications()

_EDGE_VOICES = {
    "en": "en-US-AriaNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "de": "de-DE-KatjaNeural",
    "fr": "fr-FR-DeniseNeural",
    "es": "es-ES-ElviraNeural",
}

_LANG_HINTS = {
    "en": ("english", "zira", "david", "mark", "en-us", "en-gb"),
    "zh": ("chinese", "huihui", "yaoyao", "zh-cn", "zh-tw"),
    "ja": ("japanese", "haruka", "ayumi", "ja-jp"),
    "ko": ("korean", "heami", "ko-kr"),
    "ru": ("russian", "irina", "ru-ru"),
    "de": ("german", "hedda", "de-de"),
    "fr": ("french", "hortense", "fr-fr"),
    "es": ("spanish", "helena", "es-es"),
}


def _pick_voice(voice, lang: str):
    hints = _LANG_HINTS.get((lang or "en").split("-")[0].lower(), ())
    try:
        tokens = voice.GetVoices()
        for index in range(tokens.Count):
            token = tokens.Item(index)
            description = str(token.GetDescription()).lower()
            if any(hint in description for hint in hints):
                return token
    except Exception:
        pass
    return None


_speak_lock = threading.Lock()


def _speak_edge(text: str, lang: str, cancel) -> None:
    """Online neural voice via edge-tts, played through Windows MCI."""
    import asyncio

    import edge_tts  # type: ignore

    voice = _EDGE_VOICES.get((lang or "en").split("-")[0].lower(), _EDGE_VOICES["en"])
    folder = config.DATA_DIR / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"tts_{uuid.uuid4().hex}.mp3"

    async def save() -> None:
        job = asyncio.create_task(edge_tts.Communicate(text, voice).save(str(target)))
        try:
            deadline = time.monotonic() + 25
            while not job.done():
                if cancel.is_set() or time.monotonic() > deadline:
                    job.cancel()
                    raise RuntimeError("在线朗读已取消或超时")
                await asyncio.sleep(0.1)
            await job
        finally:
            if not job.done():
                job.cancel()
                try:
                    await job
                except asyncio.CancelledError:
                    pass

    alias = f"screengloss_tts_{threading.get_ident()}"
    mci = ctypes.windll.winmm.mciSendStringW
    try:
        asyncio.run(save())
        if cancel.is_set():
            return
        if mci(f'open "{target}" type mpegvideo alias {alias}', None, 0, 0) != 0:
            raise RuntimeError("MCI could not open the synthesized audio")
        if mci(f"play {alias}", None, 0, 0) != 0:
            raise RuntimeError("MCI playback failed")
        buffer = ctypes.create_unicode_buffer(64)
        while not cancel.wait(0.05):
            mci(f"status {alias} mode", buffer, 64, 0)
            if buffer.value != "playing":
                break
    finally:
        mci(f"close {alias}", None, 0, 0)
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
    event("tts.completed", category="edge", language=lang)


def _speak_sapi(text: str, lang: str, rate: int, cancel) -> None:
    import pythoncom

    pythoncom.CoInitialize()
    try:
        import win32com.client

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        token = _pick_voice(voice, lang)
        if token is not None:
            voice.Voice = token
        else:
            notifications.warning.emit("没有找到对应语言的系统语音，将使用系统默认声音")
        try:
            voice.Rate = int(rate)
        except Exception:
            pass
        voice.Speak(text, 3)
        deadline = time.monotonic() + 120
        while not voice.WaitUntilDone(50):
            if cancel.is_set() or time.monotonic() > deadline:
                voice.Speak("", 3)
                break
        event(
            "tts.cancelled" if cancel.is_set() else "tts.completed", category="sapi", language=lang
        )
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def engine_name() -> str:
    try:
        return str(config.load().get("tts", {}).get("engine", "sapi"))
    except Exception:
        return "sapi"


_queue = queue.Queue(maxsize=1)
_active_cancel = None
_thread = None


def _run_queue():
    while True:
        text, lang, rate, chosen, cancel = _queue.get()
        try:
            if cancel.is_set():
                continue
            if chosen == "edge":
                try:
                    _speak_edge(text, lang, cancel)
                    continue
                except Exception:
                    if cancel.is_set():
                        continue
                    notifications.warning.emit("在线朗读失败，已回退系统语音")
            if not cancel.is_set():
                _speak_sapi(text, lang, rate, cancel)
        except Exception:
            event("tts.failed", category="playback")
            notifications.warning.emit("朗读失败，请检查系统声音和已安装的语音")
        finally:
            _queue.task_done()


def speak(text: str, lang: str = "en", rate: int = -1, engine: str | None = None) -> None:
    """Speak text asynchronously; calls are serialized so voices never overlap."""
    text = (text or "").strip()
    if not text:
        return
    chosen = (engine or engine_name() or "sapi").lower()

    global _active_cancel, _thread
    with _speak_lock:
        if _active_cancel:
            _active_cancel.set()
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            pass
        _active_cancel = threading.Event()
        _queue.put_nowait((text, lang, rate, chosen, _active_cancel))
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run_queue, daemon=True, name="speech")
            _thread.start()


def stop() -> None:
    with _speak_lock:
        if _active_cancel:
            _active_cancel.set()
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            pass


def cleanup():
    for path in (config.DATA_DIR / "tmp").glob("tts_*.mp3"):
        try:
            path.unlink()
        except OSError:
            pass

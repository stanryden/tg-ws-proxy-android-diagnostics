# Android lifecycle/status bridge for the existing Telegram WS proxy.
import asyncio
import json
import logging
import os
import secrets
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import proxy.tg_ws_proxy as core

_log = logging.getLogger("tgwsproxy.android")
_thread = None
_monitor_thread = None
_loop = None
_stop_event = None
_lock = threading.RLock()
_status = "stopped"
_error = ""
_stopping = False
DEFAULT_DC = ["2:149.154.167.220", "4:149.154.167.220"]

# The SOCKS5 listener is always loopback-only and always requires a
# per-installation username/password, so other apps on the device cannot use it.
LISTEN_HOST = "127.0.0.1"
DEFAULT_SETTINGS = {
    "verify_tls": True,           # verify Telegram's TLS certificate
    "strict_destinations": True,  # only Telegram destinations via passthrough
}


def _data_dir():
    d = Path.home() / "TgWsProxy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_private(path, text):
    """Atomic write, readable by this app only (0600)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(str(tmp), str(path))


def _load_credentials():
    """Return (user, password); generate and persist them on first use."""
    path = _data_dir() / "credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        user, password = data["user"], data["password"]
        if (isinstance(user, str) and isinstance(password, str)
                and 1 <= len(user.encode()) <= 255 and len(password) >= 16):
            return user, password
    except Exception:
        pass
    user = "tg" + secrets.token_hex(3)
    password = secrets.token_urlsafe(24)  # ~192 bits, URL-safe characters
    _write_private(path, json.dumps({"user": user, "password": password}))
    return user, password


def _load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads((_data_dir() / "settings.json").read_text(encoding="utf-8"))
        for key in DEFAULT_SETTINGS:
            if isinstance(data.get(key), bool):
                settings[key] = data[key]
    except Exception:
        pass
    return settings


def set_setting(key, value):
    """Persist a boolean setting; takes effect on the next proxy start."""
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    settings = _load_settings()
    settings[key] = bool(value)
    _write_private(_data_dir() / "settings.json", json.dumps(settings))
    return "ok"


def get_connection_info(port=1080):
    """JSON with everything needed to add the proxy to Telegram."""
    user, password = _load_credentials()
    settings = _load_settings()
    port = int(port)
    link = f"tg://socks?server={LISTEN_HOST}&port={port}&user={user}&pass={password}"
    return json.dumps({
        "host": LISTEN_HOST, "port": port, "user": user, "password": password,
        "link": link, "verify_tls": settings["verify_tls"],
    })


def _set_status(state, error=""):
    global _status, _error
    with _lock:
        _status = state
        _error = error or ""


def get_status():
    """Return a short status string for the Android service/UI."""
    with _lock:
        if _status == "error":
            return "error: " + (_error or "неизвестная ошибка")
        return _status


def _worker(port, dc_opt, auth, verify_tls, strict_destinations):
    global _loop, _stop_event
    loop = asyncio.new_event_loop()
    with _lock:
        _loop = loop
        _stop_event = asyncio.Event()
        stop_event = _stop_event
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(core._run(
            port, dc_opt, stop_event=stop_event, host=LISTEN_HOST,
            auth=auth, verify_tls=verify_tls,
            strict_destinations=strict_destinations))
        with _lock:
            stopping = _stopping
            state = _status
        if not stopping and state != "error":
            _set_status("error", "сервер прокси неожиданно завершился")
    except Exception as exc:
        _log.exception("Proxy worker stopped unexpectedly")
        with _lock:
            stopping = _stopping
        if not stopping:
            _set_status("error", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            loop.close()
        except Exception:
            pass
        with _lock:
            _loop = None
            _stop_event = None
            stopping = _stopping
            state = _status
        if not stopping and state in ("starting", "running"):
            _set_status("error", "сервер прокси остановился")


def _watch_ready(worker, timeout=12.0):
    """Wait until asyncio.start_server has actually bound the SOCKS port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            state = _status
            stopping = _stopping
        if stopping or state == "error":
            return
        if core._server_instance is not None:
            _set_status("running")
            return
        if not worker.is_alive():
            with _lock:
                state = _status
            if state == "starting":
                _set_status("error", "сервер завершился до открытия порта")
            return
        time.sleep(0.1)
    with _lock:
        state = _status
    if state == "starting":
        _set_status("error", "тайм-аут запуска SOCKS5-сервера")



_proxy_file_log_ready = False

def _setup_proxy_file_logging():
    global _proxy_file_log_ready
    if _proxy_file_log_ready:
        return
    log_dir = Path.home() / "TgWsProxy"
    log_dir.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    for handler in list(root_logger.handlers):
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename.endswith("/proxy.log"):
            _proxy_file_log_ready = True
            return
    warning_stream = logging.StreamHandler()
    warning_stream.setLevel(logging.WARNING)
    warning_stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(warning_stream)
    fh = RotatingFileHandler(log_dir / "proxy.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(fh)
    _proxy_file_log_ready = True


def start_proxy(port=1080):
    try:
        _setup_proxy_file_logging()
    except Exception as exc:
        print(f'Failed to initialize proxy file logging: {exc}', file=sys.stderr)

    global _thread, _monitor_thread, _stopping
    with _lock:
        if _thread is not None and _thread.is_alive():
            return get_status()
        _stopping = False
        _set_status("starting")
        dc_opt = core.parse_dc_ip_list(DEFAULT_DC)
        try:
            auth = _load_credentials()
        except Exception as exc:  # never fall back to an open proxy
            _set_status("error", f"не удалось подготовить учётные данные: {exc}")
            return get_status()
        settings = _load_settings()
        _thread = threading.Thread(
            target=_worker,
            args=(int(port), dc_opt, auth, settings["verify_tls"],
                  settings["strict_destinations"]),
            daemon=True)
        _thread.start()
        _monitor_thread = threading.Thread(target=_watch_ready, args=(_thread,), daemon=True)
        _monitor_thread.start()
    return "starting"


def stop_proxy():
    global _loop, _stop_event, _thread, _stopping
    with _lock:
        _stopping = True
        loop = _loop
        stop_event = _stop_event
        worker = _thread
    _set_status("stopping")
    if loop is not None and stop_event is not None:
        try:
            loop.call_soon_threadsafe(stop_event.set)
        except RuntimeError:
            pass
    if worker is not None:
        worker.join(timeout=4)
    with _lock:
        _thread = None
        _loop = None
        _stop_event = None
    _set_status("stopped")
    with _lock:
        _stopping = False
    return "stopped"

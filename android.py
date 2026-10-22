from __future__ import annotations

import asyncio as _asyncio
import copy
import json
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, Tuple

# Import the core proxy logic
import proxy.tg_ws_proxy as tg_ws_proxy

# --- Configuration ---
APP_NAME = "TgWsProxy"

# On Android, we save config in the home directory
APP_DIR = Path.home() / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "port": 1080,
    "host": "127.0.0.1",
    "dc_ip": ["2:149.154.167.220", "4:149.154.167.220"],
    "verbose": False,
}

_proxy_thread: Optional[threading.Thread] = None
_async_stop: Optional[Tuple[_asyncio.AbstractEventLoop, _asyncio.Event]] = None
_config: dict = {}

log = logging.getLogger("tg-ws-android")


def _ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    _ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config root must be a JSON object")
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, copy.deepcopy(v))
            return data
        except Exception as exc:
            # Logging is not configured yet at this point, so this goes to
            # stderr; remember it and re-log after setup_logging().
            log.warning("Failed to load config: %s", exc)
    return copy.deepcopy(DEFAULT_CONFIG)


def setup_logging(verbose: bool = False) -> None:
    _ensure_dirs()
    root = logging.getLogger()
    # Root must pass DEBUG so the file handler can record it even when the
    # console is quiet; each handler applies its own level.
    root.setLevel(logging.DEBUG)

    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Persistent, bounded logs; available to the app for export.
    log_path = APP_DIR / "proxy.log"
    file_handler = RotatingFileHandler(
        log_path, maxBytes=1_500_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    log.info("Persistent diagnostics enabled: %s", log_path)


def _run_proxy_thread(
    port: int,
    dc_opt: Dict[int, str],
    host: str = "127.0.0.1",
) -> None:
    global _async_stop

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    stop_ev = _asyncio.Event()
    _async_stop = (loop, stop_ev)

    try:
        loop.run_until_complete(
            tg_ws_proxy._run(port, dc_opt, stop_event=stop_ev, host=host)
        )
    except Exception:
        log.exception("Proxy thread crashed")
    finally:
        try:
            pending = [t for t in _asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    _asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            log.exception("Error during event loop cleanup")
        finally:
            loop.close()
            _async_stop = None


def start_proxy() -> bool:
    """Start the proxy thread. Returns True if it is running."""
    global _proxy_thread

    if _proxy_thread and _proxy_thread.is_alive():
        log.info("Proxy already running")
        return True

    cfg = _config
    port = int(cfg.get("port", DEFAULT_CONFIG["port"]))
    host = cfg.get("host", DEFAULT_CONFIG["host"])
    dc_ip_list = cfg.get("dc_ip", DEFAULT_CONFIG["dc_ip"])

    try:
        dc_opt = tg_ws_proxy.parse_dc_ip_list(dc_ip_list)
    except ValueError as e:
        log.error("Bad config dc_ip: %s", e)
        return False

    log.info("Starting proxy on %s:%d ...", host, port)
    _proxy_thread = threading.Thread(
        target=_run_proxy_thread,
        args=(port, dc_opt, host),
        daemon=True,
        name="proxy",
    )
    _proxy_thread.start()

    # Give the thread a moment to fail fast (e.g. port already in use)
    # before telling the user that everything is fine.
    time.sleep(0.5)
    if not _proxy_thread.is_alive():
        log.error("Proxy failed to start, see log for details")
        return False

    print("\n" + "=" * 40)
    print("Proxy is running!")
    print(f"Host: {host}")
    print(f"Port: {port}")
    print("Configure Telegram with these settings.")
    print("=" * 40 + "\n")
    return True


def stop_proxy(timeout: float = 5.0) -> None:
    """Ask the proxy to stop and wait for the thread to finish."""
    stop = _async_stop
    if stop is not None:
        loop, stop_ev = stop
        try:
            loop.call_soon_threadsafe(stop_ev.set)
        except RuntimeError:
            pass  # loop already closed
    if _proxy_thread:
        _proxy_thread.join(timeout)


def main() -> None:
    global _config

    _config = load_config()
    setup_logging(_config.get("verbose", False))
    log.info("Starting Android TG WS Proxy...")

    if not start_proxy():
        sys.exit(1)

    try:
        # Keep the main thread alive while the proxy thread is running.
        while _proxy_thread and _proxy_thread.is_alive():
            time.sleep(1)
        log.error("Proxy thread exited unexpectedly")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop_proxy()


if __name__ == "__main__":
    main()

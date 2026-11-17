#!/usr/bin/env python3
"""netspeed: замер скорости скачивания по HTTP(S).

Делает 10 последовательных запросов к адресу, считает среднее время запроса,
общий объём данных и скорость в Мбит/с. Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import configparser
import math
import os
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit, urlunsplit

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
LOGS_DIR = BASE_DIR / "logs"
DOWNLOADS_DIR = BASE_DIR / "downloads"

REQUESTS_PER_URL = 10
CHUNK_SIZE = 64 * 1024
MAX_REDIRECTS = 5
REDRAW_INTERVAL = 0.1  # секунд между перерисовками прогресса
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; netspeed/1.0)",
    "Accept": "*/*",
    "Accept-Encoding": "identity",  # считаем байты как есть, без сжатия
    "Cache-Control": "no-cache",    # просим CDN не отдавать из кэша
    "Pragma": "no-cache",
}
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(_\d+)?$")

BAR_FILL, BAR_EMPTY = "█", "░"


# ───────────────────────────── настройки ─────────────────────────────

@dataclass
class Config:
    logging_enabled: bool = False
    max_logs: int = 0
    download_files: bool = False
    download_first_only: bool = False
    timeout: float = 30.0


def _to_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("true", "yes", "on", "1"):
        return True
    if value in ("false", "no", "off", "0"):
        return False
    raise ValueError(raw)


def _to_max_logs(raw: str) -> int:
    value = int(raw.strip())
    if value < 0:
        raise ValueError(raw)
    return value


def _to_timeout(raw: str) -> float:
    value = float(raw.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError(raw)
    return value


CONVERTERS = {
    "logging_enabled": _to_bool,
    "max_logs": _to_max_logs,
    "download_files": _to_bool,
    "download_first_only": _to_bool,
    "timeout": _to_timeout,
}


def load_config(path: Path) -> Tuple[Config, List[str]]:
    """Читает config.ini. Любые неверные данные заменяются значениями по умолчанию."""
    cfg = Config()
    warnings: List[str] = []
    parser = configparser.ConfigParser(
        interpolation=None, strict=False, inline_comment_prefixes=("#", ";")
    )
    try:
        found = parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError) as exc:
        return cfg, [f"{path.name} не удалось прочитать ({exc}); используются значения по умолчанию."]
    if not found:
        return cfg, [f"Файл {path.name} не найден; используются значения по умолчанию."]
    if not parser.has_section("settings"):
        return cfg, [f"В {path.name} нет секции [settings]; используются значения по умолчанию."]

    section = parser["settings"]
    for key, convert in CONVERTERS.items():
        if key not in section:
            continue
        raw = section[key]
        try:
            setattr(cfg, key, convert(raw))
        except ValueError:
            warnings.append(
                f"Неверное значение {key} = {raw!r} в {path.name}; "
                f"используется значение по умолчанию ({getattr(cfg, key)})."
            )
    return cfg, warnings


# ───────────────────────────── адреса ─────────────────────────────

def parse_url(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Возвращает (нормализованный_адрес, None) или (None, текст_ошибки)."""
    url = (raw or "").strip()
    if not url:
        return None, "адрес не указан"
    if re.search(r"\s", url):
        return None, "адрес содержит пробелы"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        return None, f"некорректный адрес ({exc})"
    if not parts.scheme:
        return None, "не указана схема: адрес должен начинаться с http:// или https://"
    if parts.scheme.lower() not in ("http", "https"):
        return None, f"схема '{parts.scheme}' не поддерживается (нужна http или https)"
    if not host:
        return None, "в адресе нет имени хоста"
    try:
        ascii_host = host if host.isascii() else host.encode("idna").decode("ascii")
    except UnicodeError:
        return None, "некорректное имя хоста"
    if ":" in ascii_host:  # IPv6
        ascii_host = f"[{ascii_host}]"
    netloc = ascii_host + (f":{port}" if port else "")
    path = quote(parts.path, safe="/%:@&=+$,!~*'()")
    query = quote(parts.query, safe="=&%+/?:@,;$!~*'()")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, "")), None


class UsageError(Exception):
    pass


def read_url_file(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = [line.strip() for line in fh]
    except OSError as exc:
        raise UsageError(f"Не удалось прочитать файл со списком адресов: {exc}")
    except UnicodeError:
        raise UsageError(f"Файл {path} должен быть в кодировке UTF-8.")
    urls = [line for line in lines if line and not line.startswith("#")]
    if not urls:
        raise UsageError(f"В файле {path} нет ни одного адреса.")
    return urls


def collect_urls(args: argparse.Namespace) -> List[str]:
    urls: List[str] = []
    if args.url:
        urls.append(args.url)
    if args.file:
        urls.extend(read_url_file(args.file))
    if not urls:
        try:
            urls.append(input("Введите адрес для проверки (http:// или https://): "))
        except EOFError:
            raise UsageError("Адрес не указан.")
    return urls


# ───────────────────────────── форматирование ─────────────────────────────

def fmt_size(num_bytes: float) -> str:
    value = float(num_bytes)
    if value < 1000:
        return f"{int(value)} Б"
    for unit in ("КБ", "МБ", "ГБ"):
        value /= 1000
        if value < 1000 or unit == "ГБ":
            return f"{value:.2f} {unit}"
    return ""


def fmt_secs(seconds: float) -> str:
    return f"{seconds:.3f} с"


def fmt_clock(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes):02d}:{rest:04.1f}"


def mbps(num_bytes: float, seconds: float) -> Optional[float]:
    """Мегабиты в секунду (1 Мбит = 1 000 000 бит)."""
    if seconds <= 0:
        return None
    return num_bytes * 8 / seconds / 1_000_000


def fmt_speed(value: Optional[float]) -> str:
    return "н/д" if value is None else f"{value:.2f} Мбит/с"


def bar(fraction: float, width: int = 24) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "[" + BAR_FILL * filled + BAR_EMPTY * (width - filled) + "]"


def rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(BASE_DIR))
    except ValueError:
        return str(path)


# ───────────────────────────── один запрос ─────────────────────────────

@dataclass
class RequestResult:
    index: int
    ok: bool
    status: Optional[int]
    size: int                      # получено байт тела (при ошибке - частично)
    seconds: float
    error: Optional[str] = None
    saved_path: Optional[Path] = None


class IncompleteBody(Exception):
    pass


class LimitedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise urllib.error.HTTPError(newurl, code, "редирект на неподдерживаемую схему", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(LimitedRedirect)


def describe_error(exc: BaseException) -> Tuple[Optional[int], str]:
    """Возвращает (HTTP-код или None, понятное сообщение)."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code, f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return None, "не удалось найти хост (ошибка DNS)"
        if isinstance(reason, TimeoutError):
            return None, "таймаут соединения"
        if isinstance(reason, ConnectionRefusedError):
            return None, "соединение отклонено сервером"
        if isinstance(reason, ssl.SSLError):
            return None, f"ошибка SSL: {reason}"
        return None, f"сетевая ошибка: {reason}"
    if isinstance(exc, IncompleteBody):
        return None, str(exc)
    if isinstance(exc, IncompleteRead):
        return None, "соединение оборвано до конца ответа"
    if isinstance(exc, TimeoutError):
        return None, "таймаут: сервер перестал отвечать"
    if isinstance(exc, ssl.SSLError):
        return None, f"ошибка SSL: {exc}"
    if isinstance(exc, ConnectionError):
        return None, f"соединение разорвано: {exc}"
    if isinstance(exc, HTTPException):
        return None, f"ошибка протокола HTTP: {exc}"
    if isinstance(exc, OSError):
        return None, f"ошибка ввода-вывода: {exc}"
    return None, f"неожиданная ошибка: {exc}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if len(name) > 100:
        stem, ext = os.path.splitext(name)
        name = stem[: max(1, 100 - len(ext))] + ext
    return name or "download.bin"


def filename_for(resp, url: str) -> str:
    name = None
    try:
        name = resp.headers.get_filename()
    except Exception:
        name = None
    if not name:
        name = unquote(os.path.basename(urlsplit(url).path))
    return safe_filename(name or "")


def unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists() or path.with_name(path.name + ".part").exists():
        path = directory / f"{stem}_{n}{suffix}"
        n += 1
    return path


def fetch(
    index: int,
    url: str,
    timeout: float,
    on_progress: Callable[[int, Optional[int]], None],
    save_dir: Optional[Path] = None,
    save_prefix: str = "",
) -> RequestResult:
    """Один полный запрос. Ошибки возвращаются в результате; Ctrl+C пробрасывается."""
    request = urllib.request.Request(url, headers=HEADERS)
    start = time.perf_counter()
    received = 0
    status: Optional[int] = None
    out = None
    part_path: Optional[Path] = None
    final_path: Optional[Path] = None
    try:
        with OPENER.open(request, timeout=timeout) as resp:
            status = resp.status
            length = resp.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            on_progress(0, total)
            if save_dir is not None:
                save_dir.mkdir(parents=True, exist_ok=True)
                final_path = unique_path(save_dir, save_prefix + filename_for(resp, url))
                part_path = final_path.with_name(final_path.name + ".part")
                out = open(part_path, "wb")
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if out is not None:
                    out.write(chunk)
                on_progress(received, total)
            elapsed = time.perf_counter() - start
            if total is not None and received != total:
                raise IncompleteBody(f"соединение оборвано: получено {received} из {total} байт")
        saved: Optional[Path] = None
        if out is not None:
            out.close()
            out = None
            os.replace(part_path, final_path)
            saved = final_path
        return RequestResult(index, True, status, received, elapsed, None, saved)
    except Exception as exc:  # KeyboardInterrupt сюда не попадает
        code, message = describe_error(exc)
        return RequestResult(index, False, code or status, received, time.perf_counter() - start, message)
    finally:
        if out is not None:
            out.close()
        if part_path is not None and part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass


# ───────────────────────────── статистика ─────────────────────────────

@dataclass
class Stats:
    ok: int
    failed: int
    avg_time: Optional[float]
    size: int
    seconds: float
    speed: Optional[float]
    partial: int


def summarize(results: List[RequestResult]) -> Stats:
    """Среднее время, объём и скорость считаются только по успешным запросам."""
    good = [r for r in results if r.ok]
    size = sum(r.size for r in good)
    seconds = sum(r.seconds for r in good)
    return Stats(
        ok=len(good),
        failed=len(results) - len(good),
        avg_time=seconds / len(good) if good else None,
        size=size,
        seconds=seconds,
        speed=mbps(size, seconds) if good else None,
        partial=sum(r.size for r in results if not r.ok),
    )


@dataclass
class UrlReport:
    url: str
    status: str = "pending"  # ok | partial | unavailable | invalid | interrupted
    error: Optional[str] = None
    requests: List[RequestResult] = field(default_factory=list)
    saved_files: List[Path] = field(default_factory=list)


# ───────────────────────────── прогресс ─────────────────────────────

class Display:
    """Блок из нескольких строк, который перерисовывается на месте (ANSI)."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._lines = 0

    def draw(self, lines: List[str]) -> None:
        if not self.enabled:
            return
        cols = shutil.get_terminal_size((100, 20)).columns - 1
        parts = []
        if self._lines:
            parts.append(f"\033[{self._lines}A")
        for line in lines:
            parts.append("\r\033[K" + line[:cols] + "\n")
        sys.stdout.write("".join(parts))
        sys.stdout.flush()
        self._lines = len(lines)

    def release(self) -> None:
        self._lines = 0


class Progress:
    def __init__(self, display: Display, total: int):
        self.display = display
        self.total = total
        self.started = time.perf_counter()
        self.done = self.ok = self.failed = 0
        self.ok_size = 0
        self.ok_time = 0.0
        self.index = 0
        self.state = "idle"  # idle | running | ok | fail
        self.cur_start = 0.0
        self.cur_end: Optional[float] = None
        self.cur_size = 0
        self.cur_total: Optional[int] = None
        self.cur_error = ""
        self.got_headers = False
        self._last_draw = 0.0

    def start(self, index: int) -> None:
        self.index = index
        self.state = "running"
        self.cur_start = time.perf_counter()
        self.cur_end = None
        self.cur_size = 0
        self.cur_total = None
        self.cur_error = ""
        self.got_headers = False
        self.draw(force=True)

    def update(self, received: int, total: Optional[int]) -> None:
        self.cur_size = received
        self.cur_total = total
        self.got_headers = True
        self.draw()

    def finish(self, result: RequestResult) -> None:
        self.cur_end = time.perf_counter()
        self.done += 1
        if result.ok:
            self.ok += 1
            self.ok_size += result.size
            self.ok_time += result.seconds
            self.state = "ok"
            self.cur_size = result.size
        else:
            self.failed += 1
            self.state = "fail"
            self.cur_error = result.error or ""
        self.draw(force=True)

    def draw(self, force: bool = False) -> None:
        if not self.display.enabled:
            return
        now = time.perf_counter()
        if not force and now - self._last_draw < REDRAW_INTERVAL:
            return
        self._last_draw = now
        self.display.draw(self.lines(now))

    def lines(self, now: float) -> List[str]:
        running = self.state == "running"
        cur_elapsed = 0.0
        if self.state != "idle":
            cur_elapsed = (self.cur_end or now) - self.cur_start
        speed_size = self.ok_size + (self.cur_size if running else 0)
        speed_time = self.ok_time + (cur_elapsed if running else 0.0)
        speed = mbps(speed_size, speed_time) or 0.0

        label = f"Файл #{self.index:<2}  "
        if self.state == "idle":
            file_line = "Файл      ожидание..."
        elif self.state == "fail":
            file_line = f"{label}ошибка: {self.cur_error}"
        elif running and not self.got_headers:
            file_line = f"{label}ожидание ответа сервера..."
        elif self.cur_total is None:
            file_line = f"{label}размер неизвестен, получено {fmt_size(self.cur_size)}"
        else:
            fraction = self.cur_size / self.cur_total if self.cur_total else 1.0
            file_line = (
                f"{label}{bar(fraction)} {int(fraction * 100):3d}%  "
                f"{fmt_size(self.cur_size)} / {fmt_size(self.cur_total)}"
            )
        return [
            f"Запросы   {bar(self.done / self.total)} {self.done}/{self.total}"
            f"  (успешно {self.ok}, ошибок {self.failed})",
            file_line,
            f"Таймер    запрос {fmt_clock(cur_elapsed)}  |  всего {fmt_clock(now - self.started)}",
            f"Скорость  {speed:7.2f} Мбит/с  |  Скачано всего: {fmt_size(speed_size)}",
        ]


# ───────────────────────────── логи ─────────────────────────────

class RunLog:
    def __init__(self, path: Optional[Path]):
        self.path = path

    def write(self, text: str) -> None:
        if self.path is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {text}\n")
        except OSError as exc:
            print(f"[!] Не удалось записать лог ({exc}); дальше лог не ведётся.")
            self.path = None


def new_run_id() -> str:
    base = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_id, n = base, 2
    while (LOGS_DIR / run_id).exists() or (DOWNLOADS_DIR / run_id).exists():
        run_id = f"{base}_{n}"
        n += 1
    return run_id


def prune_logs(max_logs: int) -> None:
    runs = sorted(p for p in LOGS_DIR.iterdir() if p.is_dir() and RUN_ID_RE.match(p.name))
    for old in runs[: max(0, len(runs) - max_logs)]:
        shutil.rmtree(old, ignore_errors=True)


def start_log(cfg: Config, run_id: str) -> RunLog:
    if not (cfg.logging_enabled and cfg.max_logs > 0):
        return RunLog(None)
    try:
        folder = LOGS_DIR / run_id
        folder.mkdir(parents=True)
        prune_logs(cfg.max_logs)
        return RunLog(folder / "run.log")
    except OSError as exc:
        print(f"[!] Не удалось создать папку логов ({exc}); лог не ведётся.")
        return RunLog(None)


@dataclass
class RunContext:
    run_id: str
    log: RunLog
    display: Display
    downloads_root: Path


def emit(ctx: RunContext, lines: List[str]) -> None:
    for line in lines:
        print(line)
        ctx.log.write(line)


# ───────────────────────────── прогон одного адреса ─────────────────────────────

def host_dirname(url: str) -> str:
    return safe_filename(urlsplit(url).hostname or "host")


def log_request(ctx: RunContext, r: RequestResult) -> None:
    if r.ok:
        saved = rel(r.saved_path) if r.saved_path else "no"
        ctx.log.write(
            f"  #{r.index} OK status={r.status} size={r.size} time={r.seconds:.3f}s "
            f"speed={fmt_speed(mbps(r.size, r.seconds))} saved={saved}"
        )
    else:
        ctx.log.write(
            f'  #{r.index} FAIL status={r.status or "-"} error="{r.error}" '
            f"partial_size={r.size} time={r.seconds:.3f}s saved=no"
        )


def run_url(report: UrlReport, number: int, count: int, cfg: Config, ctx: RunContext) -> None:
    print(f"\n== [{number}/{count}] {report.url}")
    ctx.log.write(f"URL [{number}/{count}] {report.url}")

    request_url, problem = parse_url(report.url)
    if request_url is None:
        report.status, report.error = "invalid", problem
        return

    progress = Progress(ctx.display, REQUESTS_PER_URL)
    save_dir = ctx.downloads_root / host_dirname(request_url) if cfg.download_files else None
    saved_any = False
    report.status = "running"
    try:
        for i in range(1, REQUESTS_PER_URL + 1):
            progress.start(i)
            save_now = save_dir is not None and not (cfg.download_first_only and saved_any)
            prefix = "" if cfg.download_first_only else f"{i:02d}_"
            result = fetch(i, request_url, cfg.timeout, progress.update,
                           save_dir if save_now else None, prefix)
            report.requests.append(result)
            if result.saved_path:
                saved_any = True
                report.saved_files.append(result.saved_path)
            progress.finish(result)
            log_request(ctx, result)
            if not ctx.display.enabled:
                if result.ok:
                    print(f"  #{i}: ок, {fmt_size(result.size)} за {fmt_secs(result.seconds)}")
                else:
                    print(f"  #{i}: ошибка, {result.error}")
            if i == 1 and not result.ok:
                report.status = "unavailable"
                break
    finally:
        ctx.display.release()
        if save_dir is not None:
            try:
                save_dir.rmdir()  # уберёт папку, только если она пуста
            except OSError:
                pass
    if report.status == "running":
        report.status = "ok" if all(r.ok for r in report.requests) else "partial"


# ───────────────────────────── итоги ─────────────────────────────

def url_summary_lines(report: UrlReport, cfg: Config) -> List[str]:
    if report.status == "invalid":
        return [f"[x] Неверный адрес: {report.error}"]
    if report.status == "unavailable":
        r = report.requests[0]
        code = f" (HTTP-код {r.status})" if r.status else ""
        return [f"[x] Адрес недоступен{code}: {r.error}", "    Остальные запросы пропущены."]

    s = summarize(report.requests)
    done = len(report.requests)
    lines = ["", "Итоги:"]
    if report.status == "interrupted":
        lines.append(f"  Прервано: выполнено {done} из {REQUESTS_PER_URL} запросов")
        lines.append(f"  Успешно: {s.ok} из {done}, не удалось: {s.failed}")
    else:
        lines.append(f"  Успешно: {s.ok} из {REQUESTS_PER_URL}, не удалось: {s.failed}")
    lines.append(f"  Среднее время запроса: {fmt_secs(s.avg_time) if s.avg_time is not None else 'н/д'}")
    lines.append(f"  Общий объём скачанных данных: {fmt_size(s.size)}")

    groups: Dict[str, List[int]] = {}
    for r in report.requests:
        if not r.ok:
            groups.setdefault(r.error or "неизвестная ошибка", []).append(r.index)
    if groups:
        lines.append("  Ошибки:")
        for message, indexes in groups.items():
            lines.append(f"    запросы {', '.join('#' + str(i) for i in indexes)}: {message}")
    if s.partial:
        lines.append(f"  Получено до обрыва (в объём не входит): {fmt_size(s.partial)}")
    if cfg.download_files:
        if report.saved_files:
            lines.append(f"  Сохранено файлов: {len(report.saved_files)} "
                         f"(папка: {rel(report.saved_files[0].parent)})")
        else:
            lines.append("  Сохранено файлов: 0")
    lines.append(f">> Скорость скачивания: {fmt_speed(s.speed)}")
    return lines


def total_summary_lines(reports: List[UrlReport]) -> List[str]:
    lines = ["", "== Общая сводка =="]
    for n, r in enumerate(reports, 1):
        s = summarize(r.requests)
        if r.status in ("ok", "partial"):
            desc = f"{s.ok}/{REQUESTS_PER_URL} успешно, {fmt_size(s.size)}, {fmt_speed(s.speed)}"
        elif r.status == "unavailable":
            desc = f"недоступен ({r.requests[0].error})"
        elif r.status == "invalid":
            desc = f"неверный адрес ({r.error})"
        elif r.status == "interrupted":
            desc = "прервано"
        else:
            desc = "не обработан"
        lines.append(f"  {n}. {r.url}: {desc}")

    results = [res for r in reports for res in r.requests]
    s = summarize(results)
    clean = sum(1 for r in reports if r.status == "ok")
    lines.append("")
    lines.append(f"Адресов: {len(reports)}, полностью успешно: {clean}, "
                 f"с ошибками или недоступно: {len(reports) - clean}")
    lines.append(f"Успешных запросов: {s.ok} из {len(results)}")
    lines.append(f"Среднее время запроса: {fmt_secs(s.avg_time) if s.avg_time is not None else 'н/д'}")
    lines.append(f"Общий объём скачанных данных: {fmt_size(s.size)}")
    lines.append(f">> Общая скорость скачивания: {fmt_speed(s.speed)}")
    return lines


# ───────────────────────────── точка входа ─────────────────────────────

def configure_console() -> None:
    global BAR_FILL, BAR_EMPTY
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        (BAR_FILL + BAR_EMPTY).encode(encoding)
    except (UnicodeError, LookupError):
        BAR_FILL, BAR_EMPTY = "#", "-"
    if os.name == "nt":
        os.system("")  # включает ANSI-последовательности в консоли Windows 10+


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="netspeed.py",
        description="Замер скорости скачивания: 10 последовательных запросов к адресу.",
    )
    parser.add_argument("url", nargs="?",
                        help="адрес (http:// или https://); если не указан и нет --file, "
                             "будет запрошен интерактивно")
    parser.add_argument("-f", "--file", metavar="ФАЙЛ",
                        help="текстовый файл со списком адресов (по одному в строке, # - комментарий)")
    parser.add_argument("--pause", action="store_true",
                        help="в конце ждать нажатия Enter (чтобы окно не закрывалось сразу); "
                             "включается сам, если запуск без аргументов")
    return parser.parse_args(argv)


def pause_if_needed(args: argparse.Namespace) -> None:
    """Не даёт окну консоли закрыться сразу (двойной клик по файлу в Windows)."""
    wanted = args.pause or (not args.url and not args.file)
    if wanted and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            input("\nНажмите Enter, чтобы закрыть окно...")
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_console()

    cfg, warnings = load_config(CONFIG_PATH)
    for warning in warnings:
        print(f"[!] {warning}")
    if cfg.logging_enabled and cfg.max_logs == 0:
        print("[!] Логи включены, но max_logs = 0: логи сохраняться не будут.")

    try:
        urls = collect_urls(args)
    except UsageError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2

    run_id = new_run_id()
    interactive_tty = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    ctx = RunContext(run_id, start_log(cfg, run_id), Display(interactive_tty), DOWNLOADS_DIR / run_id)
    ctx.log.write(
        f"START run_id={run_id} urls={len(urls)} logging={cfg.logging_enabled} "
        f"max_logs={cfg.max_logs} download_files={cfg.download_files} "
        f"download_first_only={cfg.download_first_only} timeout={cfg.timeout}"
    )

    reports: List[UrlReport] = []
    current: Optional[UrlReport] = None
    interrupted = False
    try:
        for number, url in enumerate(urls, 1):
            current = UrlReport(url)
            reports.append(current)
            run_url(current, number, len(urls), cfg, ctx)
            emit(ctx, url_summary_lines(current, cfg))
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n[!] Прервано пользователем.")
        ctx.log.write("INTERRUPTED by user")
        if current is not None and current.status not in ("ok", "partial", "unavailable", "invalid"):
            current.status = "interrupted"
            if current.requests:
                emit(ctx, url_summary_lines(current, cfg))
    finally:
        try:
            ctx.downloads_root.rmdir()  # если ничего не скачано, пустую папку не оставляем
        except OSError:
            pass

    if len(reports) > 1:
        emit(ctx, total_summary_lines(reports))

    if interrupted:
        code = 130
    else:
        code = 0 if reports and all(r.status == "ok" for r in reports) else 1
    ctx.log.write(f"FINISH exit_code={code}")
    pause_if_needed(args)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

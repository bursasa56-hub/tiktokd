from __future__ import annotations

import asyncio
import mimetypes
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import yt_dlp

from app.config import DOWNLOAD_DIR, TELEGRAM_MAX_BYTES

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

SUPPORTED_HOSTS = (
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
)


class DownloadError(Exception):
    pass


@dataclass
class DownloadResult:
    kind: str  # "video" | "photos"
    title: str
    source: str
    path: Path | None = None
    photos: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        for photo in self.photos:
            photo.unlink(missing_ok=True)
        if self.path:
            self.path.unlink(missing_ok=True)


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(").,]>\"'") if match else None


def is_supported_url(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in SUPPORTED_HOSTS)


def _source_name(url: str) -> str:
    lowered = url.lower()
    if "tiktok" in lowered:
        return "TikTok"
    if "youtu" in lowered:
        return "YouTube"
    return "видео"


def _limit_label() -> str:
    return f"{TELEGRAM_MAX_BYTES // (1024 * 1024)} МБ"


def _format_selector() -> str:
    # H.264 (avc1) + AAC (m4a) до 1080p — Telegram проигрывает такой MP4
    # без перекодирования. VP9/AV1 ломают воспроизведение (только звук).
    limit = TELEGRAM_MAX_BYTES
    return (
        f"bestvideo[vcodec^=avc1][height<=1080][filesize<{limit}]+bestaudio[ext=m4a]/"
        f"bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
        f"bestvideo[vcodec^=avc1][filesize<{limit}]+bestaudio/"
        f"best[vcodec^=avc1][filesize<{limit}]/"
        f"best[ext=mp4][filesize<{limit}]/"
        f"best[filesize<{limit}]/"
        "bv*+ba/b"
    )


def _ydl_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 3,
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "format": _format_selector(),
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        },
    }


def _find_photos(info: dict) -> list[str]:
    """Собрать прямые ссылки на кадры TikTok-слайдшоу."""
    candidates: list[str] = []
    if isinstance(info.get("thumbnails"), list):
        for thumb in info["thumbnails"]:
            if thumb.get("url"):
                candidates.append(thumb["url"])
    if info.get("thumbnail"):
        candidates.append(info["thumbnail"])

    unique: list[str] = []
    seen = set()
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _human_error(raw: str) -> str:
    text = raw.lower()
    if "private" in text or "login" in text or "sign in" in text or "cookies" in text:
        return "Видео недоступно без авторизации или скрыто (например, ограничение 18+)."
    if "unavailable" in text or "not available" in text:
        return "Видео недоступно. Проверьте ссылку."
    if "copyright" in text or "removed" in text:
        return "Видео удалено или заблокировано."
    if "unsupported url" in text:
        return "Такая ссылка не поддерживается. Пришли ссылку на видео TikTok или YouTube."
    if "blocked" in text or "bot" in text:
        return "Сервис временно блокирует скачивание. Попробуй другое видео или позже."
    return "Не удалось скачать видео. Проверьте ссылку и попробуйте ещё раз."


def _download_sync(url: str, dest_dir: Path) -> tuple[DownloadResult, list[str]]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    opts = _ydl_opts(str(dest_dir / f"{job_id}.%(ext)s"))

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise DownloadError("Не удалось получить информацию о видео.")
            if "entries" in info:
                entries = [item for item in (info.get("entries") or []) if item]
                if not entries:
                    raise DownloadError("По ссылке нет видео.")
                info = entries[0]
            filename = ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(_human_error(str(exc))) from exc

    title = (info.get("title") or "video").strip() or "video"
    source = _source_name(url)

    path = Path(filename)
    if not path.exists():
        mp4 = path.with_suffix(".mp4")
        if mp4.exists():
            path = mp4

    if path.exists() and path.stat().st_size > 0:
        size = path.stat().st_size
        if size > TELEGRAM_MAX_BYTES:
            path.unlink(missing_ok=True)
            raise DownloadError(
                f"Видео слишком большое для Telegram (лимит — {_limit_label()}). "
                "Попробуйте другое видео или более короткое."
            )
        return DownloadResult(kind="video", title=title[:200], source=source, path=path), []

    # Если видео нет — пробуем TikTok-слайдшоу (фото).
    photo_urls = _find_photos(info)
    if photo_urls:
        return DownloadResult(kind="photos", title=title[:200], source=source), photo_urls

    raise DownloadError("Файл после скачивания не найден.")


async def _download_photos(urls: list[str], dest_dir: Path) -> list[Path]:
    photos: list[Path] = []
    async with aiohttp.ClientSession() as session:
        for index, url in enumerate(urls, start=1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
                    if not data:
                        continue
                    ext = Path(url.split("?")[0]).suffix or mimetypes.guess_extension(
                        resp.headers.get("Content-Type", "")
                    ) or ".jpg"
                    photo_path = dest_dir / f"{uuid.uuid4().hex}_{index}{ext}"
                    photo_path.write_bytes(data)
                    photos.append(photo_path)
            except Exception:
                continue
    return photos


_SSSTIK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


async def _ssstik_resolve(url: str, session: aiohttp.ClientSession) -> str:
    """Получить HTML со ссылками на скачивание от ssstik.io."""
    async with session.get(
        "https://ssstik.io/en",
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        page = await resp.text()
    token = re.search(r'tt\s*[:=]\s*"([^"]+)"', page) or re.search(
        r"tt\s*[:=]\s*'([^']+)'", page
    )
    if not token:
        raise DownloadError("Сервис скачивания TikTok временно недоступен. Попробуй позже.")

    headers = {
        "HX-Request": "true",
        "HX-Target": "target",
        "HX-Current-URL": "https://ssstik.io/en",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    data = {"id": url, "locale": "en", "tt": token.group(1)}
    async with session.post(
        "https://ssstik.io/abc?url=dl",
        data=data,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        return await resp.text()


def _parse_ssstik(result: str) -> tuple[str | None, list[str], str]:
    """Достать ссылку на видео без водяного знака или список фото."""
    import html as html_module

    result = html_module.unescape(result)

    title = "TikTok"
    title_match = re.search(r'<p class="maintext">(.*?)</p>', result, re.DOTALL)
    if title_match:
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() or "TikTok"

    video_match = re.search(r'href="(https://tikcdn\.io/ssstik/\d+[^"]*)"', result)
    if video_match:
        return video_match.group(1), [], title

    avatar = re.search(r'<img class="result_author" src="([^"]+)"', result)
    avatar_url = avatar.group(1) if avatar else None
    photos = []
    for img in re.findall(r'<img[^>]+src="(https://tikcdn\.io/[^"]+)"', result):
        if img != avatar_url and img not in photos:
            photos.append(img)
    return None, photos, title


async def _fetch_tiktok(url: str, dest_dir: Path) -> DownloadResult:
    """Скачивание TikTok через ssstik.io (без водяного знака, обходит блок IP)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _SSSTIK_UA}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            result_html = ""
            for attempt in range(2):
                result_html = await _ssstik_resolve(url, session)
                if result_html.strip():
                    break
                await asyncio.sleep(2)

            video_url, photo_urls, title = _parse_ssstik(result_html)

            if video_url:
                path = dest_dir / f"{uuid.uuid4().hex}.mp4"
                async with session.get(
                    video_url,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    if resp.status != 200:
                        raise DownloadError("Не удалось скачать видео. Попробуй ещё раз.")
                    content = await resp.read()
                if not content:
                    raise DownloadError("Получен пустой файл.")
                if len(content) > TELEGRAM_MAX_BYTES:
                    raise DownloadError(
                        f"Видео слишком большое для Telegram (лимит — {_limit_label()}). "
                        "Попробуйте другое видео или более короткое."
                    )
                path.write_bytes(content)
                return DownloadResult(kind="video", title=title[:200], source="TikTok", path=path)

            if photo_urls:
                photos = await _download_photos(photo_urls, dest_dir)
                if not photos:
                    raise DownloadError("Не удалось скачать фото.")
                return DownloadResult(kind="photos", title=title[:200], source="TikTok", photos=photos)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError("Сервис скачивания TikTok временно недоступен. Попробуй позже.") from exc

    raise DownloadError(
        "TikTok не отдал контент по этой ссылке. Пост может быть удалён, скрыт или ограничен."
    )


async def download_video(url: str) -> DownloadResult:
    if "tiktok" in url.lower():
        return await _fetch_tiktok(url, DOWNLOAD_DIR)

    result, photo_urls = await asyncio.to_thread(_download_sync, url, DOWNLOAD_DIR)
    if result.kind == "photos" and photo_urls:
        photos = await _download_photos(photo_urls, DOWNLOAD_DIR)
        if not photos:
            raise DownloadError("Не удалось скачать фото.")
        result.photos = photos
    return result

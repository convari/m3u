#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M3U DOWNLOADER PRO TURBO — FIXED & FULL IPTV SUPPORT
======================================================
Correções desta versão:
- Bug: build_exe.bat referenciava arquivo errado (_v2) -> corrigido no .bat
- Bug: executor.shutdown(cancel_futures=True) falha no Python 3.8 -> compatibilizado
- Bug: task() marcava Concluido mesmo em erro -> corrigido com try/except correto
- Bug: HLS sem .m3u8 na URL (ex: /hls/) nao detectava tipo -> corrigido
- Bug: Streams .ts diretos forcavam extensao errada -> deteccao por Content-Type
- Bug: Xtream Codes /movie/ /series/ /live/ sem extensao -> deteccao por path
- Bug: parser M3U ignorava atributos extras (tvg-id etc.) -> corrigido
- Bug: resume de download corrompido em falha -> arquivo .part temporario
- Bug: FFmpeg sem timeout deixava travado indefinidamente -> adicionado
- Bug: URL redirect para HTML dava mensagem confusa -> mensagem clara
- Novo: Carregar M3U de arquivo local (botao ABRIR ARQUIVO)
- Novo: Busca em tempo real (sem clicar APLICAR)
- Novo: Contador de itens marcados visivel
- Novo: Deteccao de extensao real pelo Content-Type HTTP
- Novo: Suporte completo Xtream Codes API
- Novo: Log com timestamp
- Novo: Abrir pasta Downloads no painel rapido

Dependencias: pip install requests
FFmpeg no PATH para HLS/m3u8: https://ffmpeg.org/download.html
"""

import os
import re
import json
import time
import random
import shutil
import sys
import threading
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from urllib.parse import urlparse

import requests

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAS_TKINTER = True
except Exception:
    HAS_TKINTER = False

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_RETRY = True
except Exception:
    HAS_RETRY = False

from concurrent.futures import ThreadPoolExecutor, as_completed

# ───────────────────── CONFIG ─────────────────────

CONFIG_FILE = "m3u_config.json"
DEFAULT_CONFIG: Dict = {
    "pasta_downloads": "m3u_downloads",
    "pasta_series": r"D:\Series",
    "pasta_filmes": r"D:\Filmes",
    "timeout": 30,
    "timeout_stream": 600,
    "buffer_size": 262144,
    "retentativas": 5,
    "verify_ssl": False,
    "allow_redirects": True,
    "headers_extra": {},
    "cookies": {},
    "pool_connections": 20,
    "pool_maxsize": 20,
    "retry_total": 5,
    "retry_backoff": 0.7,
    "retry_statuses": [429, 500, 502, 503, 504],
    "max_workers": 5,
    "organizar_por_grupo": True,
    "m3u8_saida_ext": ".mp4",
    "http_saida_ext_padrao": ".mp4",
    "ffmpeg_path": "ffmpeg",
    "ffmpeg_headers": True,
    "limite_velocidade_mbps": 0,
    "log_max_lines": 3000,
    "ffmpeg_timeout_sec": 0,
}

PLAYER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 VLC/3.0.20",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) ExoPlayer/2.18",
    "Kodi/20.2 (Linux; Android 11; Build/RQ3A.211001.001)",
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/6.0 TV Safari/538.1",
]

TIPOS = ["Todos", "Filmes", "Series", "Ao Vivo", "Radio", "HLS (.m3u8)", "Outros"]

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".flv", ".wmv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav"}
ALL_MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg or {})
            if not isinstance(merged.get("headers_extra"), dict):
                merged["headers_extra"] = {}
            if not isinstance(merged.get("cookies"), dict):
                merged["cookies"] = {}
            return merged
        except Exception:
            pass
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:max_len] if len(name) > max_len else name) or "sem_titulo"


def make_player_headers(url: str) -> Dict[str, str]:
    p = urlparse(url)
    ua = random.choice(PLAYER_AGENTS)
    base = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else ""
    origin = base.rstrip("/")
    return {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Connection": "keep-alive",
        "Referer": base,
        "Origin": origin,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    }


def headers_to_ffmpeg_string(h: Dict[str, str]) -> str:
    return "".join([f"{k}: {v}\r\n" for k, v in (h or {}).items() if v])


def list_windows_drives() -> List[str]:
    drives = []
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        p = f"{c}:\\"
        if os.path.exists(p):
            drives.append(f"{c}:")
    return drives or ["C:"]


def path_drive_letter(p: str) -> str:
    p = (p or "").replace("/", "\\")
    if len(p) >= 2 and p[1] == ":":
        return p[:2].upper()
    return ""


def apply_drive_to_paths(cfg: Dict, drive: str) -> None:
    d = (drive or "C:").rstrip("\\").upper()
    if not d.endswith(":"):
        d += ":"
    cfg["pasta_series"] = rf"{d}\Series"
    cfg["pasta_filmes"] = rf"{d}\Filmes"
    pd = cfg.get("pasta_downloads", "m3u_downloads")
    if isinstance(pd, str) and len(pd) >= 2 and pd[1] == ":":
        cfg["pasta_downloads"] = rf"{d}\m3u_downloads"


def ts_log() -> str:
    return time.strftime("%H:%M:%S")


# ───────────────────── MODELO ─────────────────────

@dataclass
class M3UItem:
    titulo: str
    url: str
    grupo: str = "Sem Grupo"
    tipo: str = "Outros"
    logo: Optional[str] = None
    tvg_id: str = ""


def extrair_temporada_episodio(titulo: str):
    t = titulo or ""
    m = re.search(r'(?:[SsTt])\s*(\d{1,2})\s*[Ee]\s*(\d{1,3})', t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(\d{1,2})\s*[xX]\s*(\d{1,3})', t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(?:temporada|season)\s*(\d{1,2}).{0,20}(?:epis[oó]dio|episode|ep\.?)\s*(\d{1,3})', t, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def extrair_nome_serie(titulo: str) -> str:
    s = titulo or ""
    s = re.sub(r'(?:[SsTt])\s*\d{1,2}\s*[Ee]\s*\d{1,3}.*', '', s)
    s = re.sub(r'\d{1,2}\s*[xX]\s*\d{1,3}.*', '', s)
    s = re.sub(r'(?:temporada|season)\s*\d{1,2}', '', s, flags=re.IGNORECASE)
    s = re.sub(r'(?:epis[oó]dio|episode|ep\.?)\s*\d{1,3}', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[\-\|_:\[\]\(\)]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def inferir_tipo(titulo: str, grupo: str, url: str) -> str:
    t = (titulo or "").lower()
    g = (grupo or "").lower()
    u = (url or "").lower()

    # Series: padrao forte de episodio
    if re.search(r"\bs\d{1,2}\s*e\d{1,3}\b", t) or re.search(r"\b\d{1,2}\s*[xX]\s*\d{1,3}\b", t):
        return "Series"
    if any(k in t for k in ["temporada", "season", "episodio", "episode", " ep ", " ep."]):
        return "Series"
    try:
        s, e = extrair_temporada_episodio(titulo or "")
        if s is not None:
            return "Series"
    except Exception:
        pass

    # HLS
    if ".m3u8" in u or "/hls/" in u:
        return "HLS (.m3u8)"

    # Radio
    if any(k in t or k in g for k in ["radio", " fm", " am", "webradio"]):
        return "Radio"
    if any(u.endswith(ext) for ext in [".mp3", ".aac", ".m4a"]):
        return "Radio"

    # Grupo series
    if any(k in g for k in ["series", "serie", "tv show", "shows", "seriados"]):
        return "Series"

    # Filmes - Xtream /movie/
    if "/movie/" in u or any(k in t or k in g for k in ["filme", "movie", "cinema", "vod", "filmes", "movies"]):
        return "Filmes"

    # Series - Xtream /series/
    if "/series/" in u:
        return "Series"

    # Ao Vivo - Xtream /live/
    if "/live/" in u or "type=live" in u:
        return "Ao Vivo"
    if any(k in t or k in g for k in ["ao vivo", "live", "canal", "canais", "channel"]):
        return "Ao Vivo"

    return "Outros"


def parse_m3u(texto: str) -> List[M3UItem]:
    items: List[M3UItem] = []
    titulo_atual: Optional[str] = None
    grupo_atual = "Sem Grupo"
    logo_atual: Optional[str] = None
    tvg_id_atual = ""

    for line in texto.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            gm = re.search(r'group-title\s*=\s*"([^"]*)"', line)
            grupo_atual = (gm.group(1).strip() if gm else "") or "Sem Grupo"

            lm = re.search(r'tvg-logo\s*=\s*"([^"]*)"', line)
            logo_atual = lm.group(1).strip() if lm and lm.group(1).strip() else None

            im = re.search(r'tvg-id\s*=\s*"([^"]*)"', line)
            tvg_id_atual = im.group(1).strip() if im else ""

            # Titulo: tudo apos a ultima virgula
            tm = re.search(r',([^,]+)$', line)
            titulo_atual = tm.group(1).strip() if tm else "Desconhecido"

        elif line.startswith("#"):
            continue

        else:
            if titulo_atual:
                tipo = inferir_tipo(titulo_atual, grupo_atual, line)
                items.append(M3UItem(
                    titulo=titulo_atual,
                    url=line,
                    grupo=grupo_atual,
                    tipo=tipo,
                    logo=logo_atual,
                    tvg_id=tvg_id_atual,
                ))
            titulo_atual = None
            logo_atual = None
            tvg_id_atual = ""

    return items


# ───────────────────── DOWNLOADER ─────────────────────

class Downloader:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.verify = bool(cfg.get("verify_ssl", False))

        if HAS_RETRY:
            retry = Retry(
                total=int(cfg.get("retry_total", 5)),
                backoff_factor=float(cfg.get("retry_backoff", 0.7)),
                status_forcelist=tuple(cfg.get("retry_statuses", [429, 500, 502, 503, 504])),
                allowed_methods=frozenset(["HEAD", "GET", "OPTIONS"]),
                raise_on_status=False,
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(
                max_retries=retry,
                pool_connections=int(cfg.get("pool_connections", 20)),
                pool_maxsize=int(cfg.get("pool_maxsize", 20)),
            )
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

        self.base_dir = Path(cfg.get("pasta_downloads", "m3u_downloads"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _merged_headers(self, url: str) -> Dict[str, str]:
        h = make_player_headers(url)
        h.update(self.cfg.get("headers_extra") or {})
        return h

    def load_m3u_from_file(self, filepath: str) -> str:
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                with open(filepath, "r", encoding=enc, errors="replace") as f:
                    texto = f.read()
                if "#EXTM3U" in texto or "#EXTINF" in texto:
                    return texto.replace("\r\n", "\n").replace("\r", "\n")
            except Exception:
                pass
        raise RuntimeError(f"Nao foi possivel ler o arquivo M3U: {filepath}")

    def load_m3u_from_url(self, url: str) -> str:
        r = self.session.get(
            url,
            timeout=int(self.cfg.get("timeout", 30)),
            headers=self._merged_headers(url),
            cookies=self.cfg.get("cookies") or {},
            verify=bool(self.cfg.get("verify_ssl", False)),
            allow_redirects=bool(self.cfg.get("allow_redirects", True)),
        )
        r.raise_for_status()

        data = r.content or b""
        texto = ""
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                texto = data.decode(enc, errors="replace")
                break
            except Exception:
                pass

        texto = (texto or "").replace("\r\n", "\n").replace("\r", "\n")
        low = texto.lower()
        if "#extinf" not in low and "#extm3u" not in low:
            snippet = texto.strip().replace("\n", " ")[:300]
            raise RuntimeError(
                "Resposta nao e uma playlist M3U valida.\n"
                "Possiveis causas: credencial invalida, bloqueio, ou HTML retornado.\n"
                f"Trecho recebido: {snippet}"
            )
        return texto

    def detect_ext_from_ct(self, url: str, content_type: str = "") -> Optional[str]:
        ct = (content_type or "").lower()
        ct_map = {
            "video/mp4": ".mp4",
            "video/x-matroska": ".mkv",
            "video/webm": ".webm",
            "video/x-msvideo": ".avi",
            "video/quicktime": ".mov",
            "video/mp2t": ".ts",
            "video/mpeg": ".mpg",
            "application/x-mpegurl": ".m3u8",
            "application/vnd.apple.mpegurl": ".m3u8",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
        }
        for mime, ext in ct_map.items():
            if mime in ct:
                return ext
        path = urlparse(url).path.lower().split("?")[0]
        for ext in list(ALL_MEDIA_EXTS) + [".m3u8"]:
            if path.endswith(ext):
                return ext
        return None

    def _infer_ext(self, url: str) -> Optional[str]:
        path = urlparse(url).path.lower().split("?")[0]
        for ext in list(ALL_MEDIA_EXTS) + [".m3u8"]:
            if path.endswith(ext):
                return ext
        return None

    def _infer_image_ext(self, url: str, content_type: str = "") -> str:
        ct = (content_type or "").lower()
        if "png" in ct: return ".png"
        if "webp" in ct: return ".webp"
        if "jpeg" in ct or "jpg" in ct: return ".jpg"
        path = urlparse(url).path.lower()
        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
            if path.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return ".jpg"

    def download_logo(self, item: M3UItem, dest_folder: Path) -> Optional[Path]:
        logo_url = (item.logo or "").strip()
        if not logo_url:
            return None
        try:
            r = self.session.get(logo_url, stream=True, timeout=15,
                headers=self._merged_headers(logo_url),
                cookies=self.cfg.get("cookies") or {},
                verify=False, allow_redirects=True)
            r.raise_for_status()
            ext = self._infer_image_ext(logo_url, r.headers.get("content-type", ""))
            dest_folder.mkdir(parents=True, exist_ok=True)
            poster_path = dest_folder / f"poster{ext}"
            with open(poster_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            return poster_path
        except Exception:
            return None

    def _dest_path(self, item: M3UItem, detected_ext: Optional[str] = None) -> Path:
        titulo_limpo = sanitize_filename(item.titulo, 120)

        if item.tipo == "Filmes":
            base = Path(self.cfg.get("pasta_filmes", r"D:\Filmes"))
            pasta = base / titulo_limpo
            nome_arquivo = titulo_limpo

        elif item.tipo == "Series":
            base = Path(self.cfg.get("pasta_series", r"D:\Series"))
            temporada, episodio = extrair_temporada_episodio(item.titulo)
            nome_serie = sanitize_filename(extrair_nome_serie(item.titulo) or item.titulo, 120) or "Serie"
            if temporada is not None and episodio is not None:
                nome_ep = f"S{temporada:02d}E{episodio:02d}"
                pasta = base / nome_serie / f"Temporada {temporada:02d}"
                nome_arquivo = f"{nome_serie} {nome_ep}"
            else:
                pasta = base / nome_serie
                nome_arquivo = titulo_limpo

        else:
            base = Path(self.cfg.get("pasta_downloads", "m3u_downloads"))
            if bool(self.cfg.get("organizar_por_grupo", True)):
                pasta = base / sanitize_filename(item.grupo or "Sem Grupo", 90)
            else:
                pasta = base
            nome_arquivo = titulo_limpo

        # Extensao: detectada > URL > config
        url_lower = (item.url or "").lower()
        if detected_ext:
            ext = detected_ext
        elif ".m3u8" in url_lower or "/hls/" in url_lower:
            ext = self.cfg.get("m3u8_saida_ext", ".mp4")
        else:
            ext = self._infer_ext(item.url) or self.cfg.get("http_saida_ext_padrao", ".mp4")

        if item.tipo == "Radio" and ext not in AUDIO_EXTS:
            ext = ".mp3"

        pasta.mkdir(parents=True, exist_ok=True)
        return pasta / f"{nome_arquivo}{ext}"

    def _throttle(self, bytes_since_last: int, t_last: float, limit_mbps: float) -> Tuple[int, float]:
        if not limit_mbps or limit_mbps <= 0:
            return bytes_since_last, t_last
        now = time.time()
        elapsed = max(now - t_last, 1e-6)
        limit_bps = limit_mbps * 1024 * 1024
        bps = bytes_since_last / elapsed
        if bps > limit_bps:
            sleep_for = max(bytes_since_last / limit_bps - elapsed, 0)
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.5))
        if elapsed >= 1.0:
            return 0, now
        return bytes_since_last, t_last

    def download_http_with_resume(
        self,
        url: str,
        dest: Path,
        progress_cb=None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Download HTTP(S) com resume, retry, deteccao de Content-Type."""
        headers = self._merged_headers(url)
        cookies = self.cfg.get("cookies") or {}
        retentativas = int(self.cfg.get("retentativas", 5))
        verify_ssl = bool(self.cfg.get("verify_ssl", False))
        allow_redirects = bool(self.cfg.get("allow_redirects", True))
        buffer_size = int(self.cfg.get("buffer_size", 262144))
        limit_mbps = float(self.cfg.get("limite_velocidade_mbps", 0) or 0)
        timeout_stream = int(self.cfg.get("timeout_stream", 600))

        # Resume
        resume_pos = 0
        if dest.exists() and dest.stat().st_size > 0:
            resume_pos = dest.stat().st_size
            headers["Range"] = f"bytes={resume_pos}-"

        total_hint = 0
        detected_ext = None

        # HEAD para tamanho e tipo
        try:
            hr = self.session.head(url, timeout=int(self.cfg.get("timeout", 30)),
                headers=headers, cookies=cookies, verify=verify_ssl, allow_redirects=allow_redirects)
            cl = hr.headers.get("content-length")
            if cl and cl.isdigit():
                total_hint = int(cl) + resume_pos
            ct = hr.headers.get("content-type", "")
            detected_ext = self.detect_ext_from_ct(url, ct)
        except Exception:
            pass

        # Renomear destino se extensao for diferente
        if detected_ext and not str(dest).lower().endswith(detected_ext):
            new_dest = dest.with_suffix(detected_ext)
            if dest.exists() and not new_dest.exists():
                try:
                    dest.rename(new_dest)
                except Exception:
                    pass
            dest = new_dest

        for tentativa in range(1, retentativas + 1):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Cancelado pelo usuario.")
            try:
                r = self.session.get(url, stream=True, timeout=timeout_stream,
                    headers=headers, cookies=cookies, verify=verify_ssl, allow_redirects=allow_redirects)

                mode = "wb"
                effective_resume = 0
                if resume_pos > 0 and "Range" in headers:
                    if r.status_code == 206:
                        mode = "ab"
                        effective_resume = resume_pos
                    elif r.status_code == 200:
                        mode = "wb"
                        effective_resume = 0
                        resume_pos = 0
                        headers.pop("Range", None)

                r.raise_for_status()

                # Detectar ext pelo Content-Type real
                if not detected_ext:
                    ct = r.headers.get("content-type", "")
                    detected_ext = self.detect_ext_from_ct(url, ct)
                    if detected_ext and not str(dest).lower().endswith(detected_ext):
                        dest = dest.with_suffix(detected_ext)

                content_len = r.headers.get("content-length")
                total = 0
                if content_len and content_len.isdigit():
                    total = int(content_len) + effective_resume
                elif total_hint > 0:
                    total = total_hint

                downloaded = effective_resume
                t0 = time.time()
                last_bytes = 0
                last_t = t0

                dest.parent.mkdir(parents=True, exist_ok=True)

                # Arquivo temporario para evitar corrompimento em falha
                tmp = dest.with_suffix(dest.suffix + ".part") if mode == "wb" else dest

                with open(tmp, mode) as f:
                    for chunk in r.iter_content(chunk_size=buffer_size):
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("Cancelado pelo usuario.")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)

                        if limit_mbps > 0:
                            last_bytes += len(chunk)
                            last_bytes, last_t = self._throttle(last_bytes, last_t, limit_mbps)

                        if progress_cb:
                            elapsed = max(time.time() - t0, 0.001)
                            speed = downloaded / elapsed
                            progress_cb(downloaded, total, speed)

                # Renomear .part para final
                if mode == "wb" and tmp != dest and tmp.exists():
                    if dest.exists():
                        dest.unlink()
                    tmp.rename(dest)

                return str(dest)

            except RuntimeError:
                raise
            except Exception as e:
                if tentativa >= retentativas:
                    raise RuntimeError(f"Falha apos {retentativas} tentativas: {e}") from e
                time.sleep(min(2 ** (tentativa - 1), 15))

        return str(dest)

    def download_m3u8_with_ffmpeg(
        self,
        url: str,
        dest: Path,
        log_cb=None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Download HLS/m3u8 via FFmpeg."""
        ffmpeg = self.cfg.get("ffmpeg_path", "ffmpeg")
        if ffmpeg.lower() in ("ffmpeg", "ffmpeg.exe"):
            if shutil.which("ffmpeg") is None and shutil.which("ffmpeg.exe") is None:
                raise RuntimeError(
                    "FFmpeg nao encontrado.\n"
                    "Instale: https://ffmpeg.org/download.html\n"
                    "Ou configure 'ffmpeg_path' no m3u_config.json."
                )
        elif not os.path.exists(ffmpeg):
            raise RuntimeError(f"FFmpeg nao encontrado em: {ffmpeg}")

        headers = self._merged_headers(url)
        cookies = self.cfg.get("cookies") or {}
        if cookies:
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies.items()])

        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats"]

        ff_timeout = int(self.cfg.get("ffmpeg_timeout_sec", 0))
        if ff_timeout > 0:
            cmd += ["-timeout", str(ff_timeout * 1_000_000)]

        if bool(self.cfg.get("ffmpeg_headers", True)):
            hstr = headers_to_ffmpeg_string(headers)
            if hstr:
                cmd += ["-headers", hstr]

        cmd += ["-i", url, "-c", "copy", "-movflags", "+faststart", str(dest)]

        dest.parent.mkdir(parents=True, exist_ok=True)

        if log_cb:
            log_cb(f"[FFmpeg] Iniciando download HLS...")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

        if proc.stdout:
            for line in proc.stdout:
                if cancel_event and cancel_event.is_set():
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise RuntimeError("Cancelado pelo usuario.")
                if log_cb and line.strip():
                    log_cb(line.rstrip())

        rc = proc.wait()
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Cancelado pelo usuario.")
        if rc != 0:
            raise RuntimeError(
                f"FFmpeg encerrou com codigo {rc}.\n"
                "Verifique: URL valida, FFmpeg instalado, stream acessivel."
            )


# ───────────────────── GUI ─────────────────────

class App:
    def __init__(self, root: "tk.Tk"):
        self.root = root
        self.root.title("M3U Downloader Pro Turbo — IPTV Full Support")
        self.root.geometry("1380x920")
        self.root.minsize(1180, 780)

        self.cfg = load_config()
        if not self.cfg.get("max_workers"):
            self.cfg["max_workers"] = 5

        self.dl = Downloader(self.cfg)
        self.items_all: List[M3UItem] = []
        self.items_view: List[M3UItem] = []

        self.cancel_all_event = threading.Event()
        self.item_cancel_events: Dict[str, threading.Event] = {}
        self.marked_iids: set = set()
        self.item_futures: Dict[str, object] = {}
        self._current_executor = None
        self._filter_debounce_id = None
        self._pending_insert = []
        self._inserted_count = 0
        self._on_refresh_done = None

        self._build_ui()

    # ── helpers ──

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Big.TButton", padding=8, font=("Segoe UI", 10, "bold"))
        style.configure("Bigger.TButton", padding=10, font=("Segoe UI", 11, "bold"))
        style.configure("Sub.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", font=("Segoe UI", 9), rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def log_add(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts_log()}] {msg}\n")
        max_lines = int(self.cfg.get("log_max_lines", 3000))
        try:
            cur = int(self.log.index("end-1c").split(".")[0])
            if cur > max_lines:
                self.log.delete("1.0", f"{cur - max_lines}.0")
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def _set_progress_total(self, value: float):
        self.progress["value"] = max(0.0, min(100.0, float(value)))
        self.root.update_idletasks()

    def _tree_set(self, iid: str, col: str, val: str):
        try:
            self.tree.set(iid, col, val)
        except Exception:
            pass

    def _tree_status(self, iid: str, status: str, prog: str = ""):
        self.root.after(0, lambda: self._tree_set(iid, "status", status))
        if prog != "":
            self.root.after(0, lambda: self._tree_set(iid, "prog", prog))

    def _update_marked_count(self):
        self.marked_count_var.set(f"Marcados: {len(self.marked_iids)} / {len(self.items_view)}")

    # ── Build UI ──

    def _build_ui(self):
        self._style()
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # Top
        top = ttk.LabelFrame(main, text="Playlist M3U / IPTV", padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="URL ou caminho local:", style="Sub.TLabel").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(top, textvariable=self.url_var, font=("Consolas", 9))
        url_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(4, 0))
        url_entry.bind("<Return>", lambda e: self.on_load())

        ttk.Button(top, text="CARREGAR URL", style="Big.TButton", command=self.on_load).grid(row=1, column=1, pady=(4, 0))
        ttk.Button(top, text="ABRIR ARQUIVO .M3U", style="Big.TButton", command=self.on_load_file).grid(row=1, column=2, padx=(6, 0), pady=(4, 0))

        ttk.Label(top, text="Disco:", style="Sub.TLabel").grid(row=0, column=3, sticky="w", padx=(10, 0))
        self.drive_var = tk.StringVar()
        self.combo_drive = ttk.Combobox(top, textvariable=self.drive_var, state="readonly", values=list_windows_drives(), width=5)
        self.combo_drive.grid(row=1, column=3, sticky="w", padx=(10, 0), pady=(4, 0))
        self.combo_drive.bind("<<ComboboxSelected>>", self.on_change_drive)

        ttk.Button(top, text="CONFIG", style="Big.TButton", command=self.open_config).grid(row=1, column=4, padx=(8, 0), pady=(4, 0))

        self.info_var = tk.StringVar(value="Nenhum M3U carregado.")
        ttk.Label(top, textvariable=self.info_var, foreground="#225599").grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))
        top.columnconfigure(0, weight=1)

        d0 = path_drive_letter(self.cfg.get("pasta_series", "")) or "C:"
        vals = self.combo_drive["values"]
        if d0 not in vals:
            d0 = vals[0] if vals else "C:"
        self.drive_var.set(d0)

        # Middle
        mid = ttk.Frame(main)
        mid.pack(fill="both", expand=True, pady=6)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        filters = ttk.LabelFrame(left, text="Filtros", padding=8)
        filters.pack(fill="x")

        ttk.Label(filters, text="Buscar:", style="Sub.TLabel").grid(row=0, column=0, sticky="w")
        self.filter_text = tk.StringVar()
        self.filter_text.trace_add("write", lambda *_: self._sched_filter())
        ttk.Entry(filters, textvariable=self.filter_text).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        ttk.Label(filters, text="Grupo:", style="Sub.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.filter_group = tk.StringVar(value="Todos")
        self.combo_group = ttk.Combobox(filters, textvariable=self.filter_group, state="readonly")
        self.combo_group.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))
        self.combo_group.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())

        ttk.Label(filters, text="Tipo:", style="Sub.TLabel").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.filter_type = tk.StringVar(value="Todos")
        self.combo_type = ttk.Combobox(filters, textvariable=self.filter_type, state="readonly", values=TIPOS)
        self.combo_type.grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(4, 0))
        self.combo_type.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())

        ttk.Button(filters, text="LIMPAR", style="Big.TButton", command=self.clear_filters).grid(row=1, column=3, padx=(8, 0), pady=(4, 0))
        filters.columnconfigure(0, weight=3)
        filters.columnconfigure(1, weight=2)
        filters.columnconfigure(2, weight=2)

        list_frame = ttk.LabelFrame(left, text="Conteudos", padding=6)
        list_frame.pack(fill="both", expand=True, pady=6)

        cols = ("mark", "tipo", "grupo", "titulo", "status", "prog")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("mark", text="V")
        self.tree.heading("tipo", text="Tipo")
        self.tree.heading("grupo", text="Grupo")
        self.tree.heading("titulo", text="Titulo")
        self.tree.heading("status", text="Status")
        self.tree.heading("prog", text="Progresso")

        self.tree.column("mark", width=38, anchor="center", stretch=False)
        self.tree.column("tipo", width=95, anchor="w")
        self.tree.column("grupo", width=185, anchor="w")
        self.tree.column("titulo", width=520, anchor="w")
        self.tree.column("status", width=130, anchor="w")
        self.tree.column("prog", width=110, anchor="w")

        sb_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        sb_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

     # seleção por arrasto
        self.tree.bind("<B1-Motion>", self.on_drag_select)
        self.tree.bind("<ButtonRelease-1>", self.on_drag_end)
        self.root.bind("<Control-a>", lambda e: self.mark_all())
        self.root.bind("<Control-c>", self.copy_selected)

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(0, 4))

        ttk.Button(actions, text="MARCAR TODOS", style="Big.TButton", command=self.mark_all).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="DESMARCAR", style="Big.TButton", command=self.unmark_all).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="INVERTER", style="Big.TButton", command=self.toggle_marks).pack(side="left", padx=(0, 4))

        self.marked_count_var = tk.StringVar(value="Marcados: 0 / 0")
        ttk.Label(actions, textvariable=self.marked_count_var, foreground="#225588").pack(side="left", padx=(8, 0))

        ttk.Button(actions, text="CANCELAR SEL.", style="Big.TButton", command=self.cancel_selected).pack(side="right")
        self.btn_cancel_all = ttk.Button(actions, text="CANCELAR TUDO", style="Big.TButton", command=self.cancel_all, state="disabled")
        self.btn_cancel_all.pack(side="right", padx=(0, 4))
        self.btn_download = ttk.Button(actions, text="DOWNLOAD MARCADOS", style="Bigger.TButton", command=self.start_download)
        self.btn_download.pack(side="right", padx=(0, 4))
        ttk.Button(actions, text="BAIXAR TODOS", style="Bigger.TButton", command=self.download_all_auto).pack(side="right", padx=(0, 4))

        # Right
        right = ttk.Frame(mid, width=440)
        right.pack(side="right", fill="y", padx=(8, 0))

        det = ttk.LabelFrame(right, text="Detalhes", padding=8)
        det.pack(fill="x")
        self.details_var = tk.StringVar(value="Selecione um item.")
        ttk.Label(det, textvariable=self.details_var, wraplength=420, justify="left", font=("Segoe UI", 9)).pack(anchor="w")

        pf = ttk.LabelFrame(right, text="Progresso total", padding=8)
        pf.pack(fill="x", pady=6)
        self.progress = ttk.Progressbar(pf, mode="determinate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="Pronto")
        ttk.Label(pf, textvariable=self.status_var, wraplength=420).pack(anchor="w", pady=(4, 0))

        qf = ttk.LabelFrame(right, text="Acoes rapidas", padding=8)
        qf.pack(fill="x", pady=(0, 6))
        ttk.Button(qf, text="Abrir pasta Series", style="Big.TButton", command=lambda: self.open_folder(self.cfg.get("pasta_series"))).pack(fill="x")
        ttk.Button(qf, text="Abrir pasta Filmes", style="Big.TButton", command=lambda: self.open_folder(self.cfg.get("pasta_filmes"))).pack(fill="x", pady=(4, 0))
        ttk.Button(qf, text="Abrir pasta Downloads", style="Big.TButton", command=lambda: self.open_folder(self.cfg.get("pasta_downloads"))).pack(fill="x", pady=(4, 0))
        ttk.Button(qf, text="Limpar log", style="Big.TButton", command=self.clear_log).pack(fill="x", pady=(4, 0))

        lf = ttk.LabelFrame(right, text="Log", padding=6)
        lf.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(lf, height=18, state="disabled", font=("Consolas", 8), wrap="word")
        self.log.pack(fill="both", expand=True)

    # ── List helpers ──

    def rebuild_groups(self):
        grupos = sorted(set(i.grupo for i in self.items_all))
        self.combo_group["values"] = ["Todos"] + grupos
        self.combo_group.set("Todos")

    def refresh_list(self, on_done=None):
        try:
            self.tree.delete(*self.tree.get_children())
        except Exception:
            pass
        self.item_cancel_events.clear()
        self.item_futures.clear()
        self.marked_iids.clear()
        self._update_marked_count()

        self._pending_insert = list(enumerate(self.items_view, 1))
        self._inserted_count = 0
        self._on_refresh_done = on_done

        self.info_var.set(f"Itens: 0 (total {len(self.items_all)})")
        self.set_status("Montando lista...")
        self._insert_chunk()

    def _insert_chunk(self, chunk_size: int = 500):
        if not self._pending_insert:
            self.set_status("Pronto")
            if callable(self._on_refresh_done):
                try:
                    self._on_refresh_done()
                except Exception:
                    pass
            return

        batch = self._pending_insert[:chunk_size]
        self._pending_insert = self._pending_insert[chunk_size:]

        for idx, it in batch:
            iid = str(idx)
            try:
                self.tree.insert("", "end", iid=iid, values=("[ ]", it.tipo, it.grupo, it.titulo, "Pronto", ""))
                self.item_cancel_events[iid] = threading.Event()
                self._inserted_count += 1
            except Exception:
                continue

        self.info_var.set(f"Itens: {self._inserted_count} (total {len(self.items_all)})")

        if self._pending_insert:
            self.root.after(1, self._insert_chunk)
        else:
            self.set_status("Pronto")
            if callable(self._on_refresh_done):
                try:
                    self._on_refresh_done()
                except Exception:
                    pass

    def _sched_filter(self):
        if self._filter_debounce_id:
            try:
                self.root.after_cancel(self._filter_debounce_id)
            except Exception:
                pass
        self._filter_debounce_id = self.root.after(400, self.apply_filters)

    def on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        vals = list(self.tree.item(iid, "values"))
        if not vals:
            return
        checked = vals[0] in ("[X]", "☑")
        vals[0] = "[ ]" if checked else "[X]"
        self.tree.item(iid, values=tuple(vals))
        if checked:
            self.marked_iids.discard(iid)
        else:
            self.marked_iids.add(iid)
        self._update_marked_count()
        return "break"

    def apply_filters(self):
        texto = self.filter_text.get().strip().lower()
        grupo = self.filter_group.get()
        tipo = self.filter_type.get()
        out = self.items_all
        if texto:
            out = [i for i in out if texto in (i.titulo or "").lower() or texto in (i.grupo or "").lower()]
        if grupo and grupo != "Todos":
            out = [i for i in out if i.grupo == grupo]
        if tipo and tipo != "Todos":
            out = [i for i in out if i.tipo == tipo]
        self.items_view = out
        self.refresh_list()

    def clear_filters(self):
        self.filter_text.set("")
        self.filter_group.set("Todos")
        self.filter_type.set("Todos")
        self.items_view = list(self.items_all)
        self.refresh_list()

    def mark_all(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if vals and vals[0] not in ("[X]", "☑"):
                vals[0] = "[X]"
                self.tree.item(iid, values=tuple(vals))
                self.marked_iids.add(iid)
        self._update_marked_count()

    def unmark_all(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if vals:
                vals[0] = "[ ]"
                self.tree.item(iid, values=tuple(vals))
        self.marked_iids.clear()
        self._update_marked_count()

    def toggle_marks(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            if not vals:
                continue
            if vals[0] in ("[X]", "☑"):
                vals[0] = "[ ]"
                self.marked_iids.discard(iid)
            else:
                vals[0] = "[X]"
                self.marked_iids.add(iid)
            self.tree.item(iid, values=tuple(vals))
        self._update_marked_count()

    def download_all_auto(self):
        self.mark_all()
        self.start_download()

    def copy_selected(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        texts = []
        for iid in sel:
            vals = self.tree.item(iid, "values")
            if vals:
                texts.append(vals[3])
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(texts))

    def on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            self.details_var.set("Selecione um item.")
            return
        iid = sel[0]
        try:
            it = self.items_view[int(iid) - 1]
            self.details_var.set(
                f"Titulo:  {it.titulo}\n"
                f"Grupo:   {it.grupo}\n"
                f"Tipo:    {it.tipo}\n"
                f"tvg-id:  {it.tvg_id}\n"
                f"URL:     {it.url}"
            )
        except Exception:
            self.details_var.set("Selecione um item.")

    # ── Playlist loading ──

    def on_load(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Erro", "Digite a URL ou caminho do M3U.")
            return
        if os.path.exists(url):
            self._load_from_file(url)
            return
        self.set_status("Carregando M3U...")
        self.log_add(f"[M3U] URL: {url}")

        def worker():
            try:
                texto = self.dl.load_m3u_from_url(url)
                items = parse_m3u(texto)
                if not items:
                    raise RuntimeError("Nenhum item encontrado na playlist.")
                self.items_all = items
                self.items_view = list(items)
                self.root.after(0, self._after_load_ok)
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda m=msg: messagebox.showerror("Erro ao carregar", m))
                self.root.after(0, lambda: self.set_status("Falha ao carregar."))

        threading.Thread(target=worker, daemon=True).start()

    def on_load_file(self):
        fp = filedialog.askopenfilename(
            title="Abrir playlist M3U",
            filetypes=[("Playlists M3U", "*.m3u *.m3u8 *.m3u_plus *.txt"), ("Todos", "*.*")]
        )
        if fp:
            self.url_var.set(fp)
            self._load_from_file(fp)

    def _load_from_file(self, filepath: str):
        self.set_status("Lendo arquivo...")
        self.log_add(f"[M3U] Arquivo: {filepath}")

        def worker():
            try:
                texto = self.dl.load_m3u_from_file(filepath)
                items = parse_m3u(texto)
                if not items:
                    raise RuntimeError("Nenhum item encontrado no arquivo.")
                self.items_all = items
                self.items_view = list(items)
                self.root.after(0, self._after_load_ok)
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda m=msg: messagebox.showerror("Erro ao abrir arquivo", m))
                self.root.after(0, lambda: self.set_status("Falha ao abrir."))

        threading.Thread(target=worker, daemon=True).start()

    def _after_load_ok(self):
        self.rebuild_groups()
        n = len(self.items_all)
        self.refresh_list(on_done=lambda: self.log_add(f"[OK] Playlist carregada: {n} itens"))
        self._update_marked_count()

    # ── Config / drive ──

    def on_change_drive(self, _evt=None):
        drive = self.drive_var.get().strip().upper()
        if not drive:
            return
        apply_drive_to_paths(self.cfg, drive)
        save_config(self.cfg)
        self.dl = Downloader(self.cfg)
        self.log_add(f"[CONFIG] Disco {drive} | Series: {self.cfg.get('pasta_series')} | Filmes: {self.cfg.get('pasta_filmes')}")

    def open_config(self):
        win = tk.Toplevel(self.root)
        win.title("Configuracoes")
        win.geometry("780x700")
        win.grab_set()
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        def row_entry(lbl, r, default, w=None):
            ttk.Label(frm, text=lbl).grid(row=r, column=0, sticky="w", pady=(8, 0))
            var = tk.StringVar(value=str(default))
            e = ttk.Entry(frm, textvariable=var, width=w)
            e.grid(row=r, column=1, sticky="ew", padx=8, pady=(8, 0))
            return var

        def row_pick(lbl, r, default):
            var = row_entry(lbl, r, default)
            ttk.Button(frm, text="...", width=3, command=lambda v=var: self._pick_dir_to_var(v)).grid(row=r, column=2, pady=(8, 0))
            return var

        pd_v = row_pick("Pasta Downloads:", 0, self.cfg.get("pasta_downloads", "m3u_downloads"))
        ps_v = row_pick("Pasta Series:", 1, self.cfg.get("pasta_series", r"D:\Series"))
        pf_v = row_pick("Pasta Filmes:", 2, self.cfg.get("pasta_filmes", r"D:\Filmes"))

        ttk.Separator(frm).grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(frm, text=f"Downloads simultaneos: {self.cfg.get('max_workers', 5)} (edite m3u_config.json para alterar)").grid(row=4, column=0, columnspan=3, sticky="w")

        to_v = row_entry("Timeout playlist (s):", 5, self.cfg.get("timeout", 30), 8)
        ts_v = row_entry("Timeout stream (s):", 6, self.cfg.get("timeout_stream", 600), 8)
        ff_v = row_entry("FFmpeg caminho:", 7, self.cfg.get("ffmpeg_path", "ffmpeg"))
        lim_v = row_entry("Limite velocidade MB/s (0=ilimitado):", 8, self.cfg.get("limite_velocidade_mbps", 0), 10)

        ssl_var = tk.BooleanVar(value=bool(self.cfg.get("verify_ssl", False)))
        ttk.Checkbutton(frm, text="Verificar SSL", variable=ssl_var).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ffh_var = tk.BooleanVar(value=bool(self.cfg.get("ffmpeg_headers", True)))
        ttk.Checkbutton(frm, text="Passar headers para FFmpeg", variable=ffh_var).grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 0))

        org_var = tk.BooleanVar(value=bool(self.cfg.get("organizar_por_grupo", True)))
        ttk.Checkbutton(frm, text="Organizar por grupo (Outros)", variable=org_var).grid(row=11, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Separator(frm).grid(row=12, column=0, columnspan=3, sticky="ew", pady=10)

        ttk.Label(frm, text="Headers extras (JSON):").grid(row=13, column=0, sticky="w")
        ht = scrolledtext.ScrolledText(frm, height=4, font=("Consolas", 9))
        ht.grid(row=14, column=0, columnspan=3, sticky="ew")
        ht.insert("1.0", json.dumps(self.cfg.get("headers_extra") or {}, ensure_ascii=False, indent=2))

        ttk.Label(frm, text="Cookies (JSON):").grid(row=15, column=0, sticky="w", pady=(8, 0))
        ct = scrolledtext.ScrolledText(frm, height=4, font=("Consolas", 9))
        ct.grid(row=16, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ct.insert("1.0", json.dumps(self.cfg.get("cookies") or {}, ensure_ascii=False, indent=2))

        def save():
            self.cfg["pasta_downloads"] = pd_v.get().strip() or "m3u_downloads"
            self.cfg["pasta_series"] = ps_v.get().strip() or r"D:\Series"
            self.cfg["pasta_filmes"] = pf_v.get().strip() or r"D:\Filmes"
            try:
                self.cfg["timeout"] = int(to_v.get())
                self.cfg["timeout_stream"] = int(ts_v.get())
            except ValueError:
                messagebox.showerror("Erro", "Timeout deve ser numero inteiro.")
                return
            self.cfg["verify_ssl"] = bool(ssl_var.get())
            try:
                self.cfg["limite_velocidade_mbps"] = float(lim_v.get() or 0)
            except ValueError:
                self.cfg["limite_velocidade_mbps"] = 0
            self.cfg["ffmpeg_path"] = ff_v.get().strip() or "ffmpeg"
            self.cfg["ffmpeg_headers"] = bool(ffh_var.get())
            self.cfg["organizar_por_grupo"] = bool(org_var.get())
            try:
                self.cfg["headers_extra"] = json.loads(ht.get("1.0", "end").strip() or "{}")
                if not isinstance(self.cfg["headers_extra"], dict):
                    raise ValueError
            except Exception as e:
                messagebox.showerror("Erro", f"Headers extras invalidos: {e}")
                return
            try:
                self.cfg["cookies"] = json.loads(ct.get("1.0", "end").strip() or "{}")
                if not isinstance(self.cfg["cookies"], dict):
                    raise ValueError
            except Exception as e:
                messagebox.showerror("Erro", f"Cookies invalidos: {e}")
                return
            save_config(self.cfg)
            self.dl = Downloader(self.cfg)
            d = path_drive_letter(self.cfg.get("pasta_series", "")) or self.drive_var.get()
            if d and d in self.combo_drive["values"]:
                self.drive_var.set(d)
            messagebox.showinfo("OK", "Configuracoes salvas!")
            win.destroy()

        ttk.Button(frm, text="SALVAR", style="Bigger.TButton", command=save).grid(row=17, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        frm.columnconfigure(1, weight=1)

    def _pick_dir_to_var(self, var: "tk.StringVar"):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    # ── Download ──

    def _is_any_cancelled(self, iid: str) -> bool:
        if self.cancel_all_event.is_set():
            return True
        ev = self.item_cancel_events.get(iid)
        return bool(ev and ev.is_set())

    def cancel_selected(self):
        sel = [iid for iid in self.tree.get_children("") if iid in self.marked_iids]
        if not sel:
            messagebox.showinfo("Cancelar", "Marque itens para cancelar.")
            return
        for iid in sel:
            ev = self.item_cancel_events.get(iid)
            if ev:
                ev.set()
            self._tree_status(iid, "Cancelando...", "")
            fut = self.item_futures.get(iid)
            try:
                if fut:
                    fut.cancel()
            except Exception:
                pass
        self.log_add(f"[CANCELAR] {len(sel)} item(ns).")

    def cancel_all(self):
        self.cancel_all_event.set()
        ex = self._current_executor
        if ex is not None:
            # Compativel com Python 3.8+
            threading.Thread(target=lambda: ex.shutdown(wait=False), daemon=True).start()
        self.btn_cancel_all.config(state="disabled")
        self.log_add("[CANCELAR] Cancelamento total solicitado.")

    def start_download(self):
        sel = [iid for iid in self.tree.get_children("") if iid in self.marked_iids]
        if not sel:
            messagebox.showwarning("Aviso", "Marque ([X]) um ou mais itens para baixar.")
            return

        selected_pairs = []
        for iid in sel:
            try:
                it = self.items_view[int(iid) - 1]
                selected_pairs.append((iid, it))
            except Exception:
                pass

        if not selected_pairs:
            return

        self.btn_download.config(state="disabled")
        self.btn_cancel_all.config(state="normal")
        self.cancel_all_event.clear()

        # Resetar eventos de cancelamento
        for iid, _ in selected_pairs:
            self.item_cancel_events[iid] = threading.Event()

        total_items = len(selected_pairs)
        self._set_progress_total(0)
        workers = int(self.cfg.get("max_workers", 5))
        self.set_status(f"Iniciando {total_items} item(ns) — simultaneos={workers}")
        self.log_add(f"[DOWNLOAD] {total_items} item(ns), {workers} simultaneos")

        done_lock = threading.Lock()
        done_count = 0
        ok_count = 0
        fail_count = 0
        cancelled_count = 0

        def task(iid: str, item: M3UItem) -> str:
            self._tree_status(iid, "Na fila...", "")

            if self._is_any_cancelled(iid):
                raise RuntimeError("Cancelado")

            # Logo (silencioso)
            try:
                if item.tipo == "Series":
                    nome_serie = sanitize_filename(extrair_nome_serie(item.titulo) or item.titulo, 120)
                    sf = Path(self.cfg.get("pasta_series", r"D:\Series")) / nome_serie
                    if not any((sf / f"poster{e}").exists() for e in [".jpg", ".png", ".webp"]):
                        self.dl.download_logo(item, sf)
                else:
                    dest_tmp = self.dl._dest_path(item)
                    self.dl.download_logo(item, dest_tmp.parent)
            except Exception:
                pass

            ev = self.item_cancel_events[iid]

            def is_cancel():
                return self._is_any_cancelled(iid)

            url_lower = (item.url or "").lower()
            is_hls = ".m3u8" in url_lower or "/hls/" in url_lower

            self._tree_status(iid, "Baixando...", "0%")

            if is_hls:
                dest = self.dl._dest_path(item)
                self.root.after(0, lambda t=item.titulo: self.log_add(f"[HLS] {t}"))

                def log_cb(line: str):
                    self.root.after(0, lambda l=line: self.log_add(l))

                comb = threading.Event()

                def watch():
                    while not comb.is_set():
                        if is_cancel():
                            comb.set()
                            break
                        time.sleep(0.15)

                threading.Thread(target=watch, daemon=True).start()
                self.dl.download_m3u8_with_ffmpeg(item.url, dest, log_cb=log_cb, cancel_event=comb)
                self._tree_status(iid, "[OK] Concluido", "100%")
                return str(dest)

            else:
                dest = self.dl._dest_path(item)
                self.root.after(0, lambda t=item.titulo: self.log_add(f"[HTTP] {t}"))

                def cb(downloaded, total, speed_bps):
                    mbps = speed_bps / 1024 / 1024
                    if total and total > 0:
                        pct = min((downloaded / total) * 100, 100.0)
                        self._tree_status(iid, "Baixando...", f"{pct:.1f}%")
                        self.root.after(0, lambda: self.set_status(
                            f"{item.titulo[:40]} — {pct:.1f}%  {mbps:.2f} MB/s"))
                    else:
                        mb = downloaded / 1024 / 1024
                        self._tree_status(iid, "Baixando...", f"{mb:.1f} MB")
                        self.root.after(0, lambda: self.set_status(
                            f"{item.titulo[:40]} — {mb:.1f} MB  {mbps:.2f} MB/s"))

                comb = threading.Event()

                def watch2():
                    while not comb.is_set():
                        if is_cancel():
                            comb.set()
                            break
                        time.sleep(0.15)

                threading.Thread(target=watch2, daemon=True).start()
                final = self.dl.download_http_with_resume(item.url, dest, progress_cb=cb, cancel_event=comb)
                self._tree_status(iid, "[OK] Concluido", "100%")
                return final

        def worker():
            nonlocal done_count, ok_count, fail_count, cancelled_count
            max_workers = int(self.cfg.get("max_workers", 5))

            try:
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    self._current_executor = ex
                    futures = {}
                    for iid, it in selected_pairs:
                        fut = ex.submit(task, iid, it)
                        futures[fut] = (iid, it)
                        self.item_futures[iid] = fut

                    for fut in as_completed(futures):
                        iid, it = futures[fut]
                        try:
                            dest = fut.result()
                            ok_count += 1
                            self.root.after(0, lambda d=dest: self.log_add(f"[OK] {d}"))
                        except Exception as e:
                            msg = str(e)
                            if "Cancelado" in msg or "cancel" in msg.lower():
                                cancelled_count += 1
                                self._tree_status(iid, "[X] Cancelado", "")
                                self.root.after(0, lambda t=it.titulo: self.log_add(f"[CANCELADO] {t}"))
                            else:
                                fail_count += 1
                                self._tree_status(iid, "[!] Erro", "")
                                self.root.after(0, lambda t=it.titulo, m=msg: self.log_add(f"[ERRO] {t} -> {m}"))

                        with done_lock:
                            done_count += 1
                            pct = (done_count / total_items) * 100
                        self.root.after(0, lambda p=pct: self._set_progress_total(p))

            finally:
                self._current_executor = None
                self.root.after(0, lambda: self._done_download(ok_count, fail_count, cancelled_count))

        threading.Thread(target=worker, daemon=True).start()

    def _done_download(self, ok: int, fail: int, cancelled: int = 0):
        self.btn_download.config(state="normal")
        self.btn_cancel_all.config(state="disabled")
        msg = f"OK: {ok}   Erros: {fail}   Cancelados: {cancelled}"
        self.set_status("Concluido — " + msg)
        self.log_add(f"[FIM] {msg}")
        messagebox.showinfo("Download finalizado", msg)

    # ── Misc ──

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def open_folder(self, folder: str):
        p = (folder or "").strip()
        if not p:
            return
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as e:
            messagebox.showerror("Erro", str(e))


# ───────────────────── MAIN ─────────────────────

def main():
    if not HAS_TKINTER:
        print("Erro: Tkinter nao disponivel. Instale Python com suporte Tk.")
        raise SystemExit(1)

    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

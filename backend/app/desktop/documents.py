from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import trafilatura
from pypdf import PdfReader

from app.adapters.fetch import UrlFetcher
from app.config import Settings
from app.desktop.config import DesktopConfig

FORMATS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".pdf", ".docx", ".pptx",
           ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
IMAGES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\x00", "").replace("\r", "")).strip()


def _pdf_text(path: Path) -> str:
    pdf = PdfReader(path)
    if len(pdf.pages) > 100:
        raise ValueError("PDF 超过 100 页，请拆分后投递。")
    return "\n\n".join(f"[第 {i + 1} 页]\n{page.extract_text() or ''}" for i, page in enumerate(pdf.pages))


def _office_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 80 * 1024 * 1024:
            raise ValueError("文档解压后超过 80MB，请拆分。")
        if path.suffix.lower() == ".docx":
            root = ET.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            return "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
        slides = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                        key=lambda name: int(re.search(r"slide(\d+)", name)[1]))
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        return "\n\n".join(f"[第 {index + 1} 张幻灯片]\n" + "\n".join(
            node.text or "" for node in ET.fromstring(archive.read(name)).findall(".//a:t", ns))
            for index, name in enumerate(slides))


class Documents:
    def __init__(self, config: DesktopConfig):
        self.config = config
        self.fetcher = UrlFetcher(Settings(max_html_bytes=8 * 1024 * 1024,
                                           max_pdf_bytes=30 * 1024 * 1024, fetch_timeout_seconds=35))

    async def ocr(self, path: Path) -> str:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(self.config.root / "scripts/Read-LocalDocument.ps1"), "-Path", str(path.resolve()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=240)
        if process.returncode:
            raise ValueError("Windows OCR 解析失败：" + stderr.decode("utf-8", errors="replace")[-700:])
        result = json.loads(stdout.decode("utf-8-sig"))
        text = "\n\n".join(f"[第 {page['page']} 页]\n{page['text']}" for page in result["pages"])
        return re.sub(r"(?<=[\u4e00-\u9fff，。！？；：]) +(?=[\u4e00-\u9fff，。！？；：])", "", text)

    async def parse_file(self, path: Path) -> str:
        extension = path.suffix.lower()
        if extension == ".pdf":
            text = await asyncio.to_thread(_pdf_text, path)
            # Scanned PDFs contain no usable text layer; page labels do not count as content.
            if len(re.sub(r"\[第 \d+ 页\]|\s", "", text)) < 20:
                text = await self.ocr(path)
        elif extension in IMAGES:
            text = await self.ocr(path)
        elif extension in {".docx", ".pptx"}:
            text = await asyncio.to_thread(_office_text, path)
        else:
            raw = await asyncio.to_thread(path.read_bytes)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("gb18030")
            if extension in {".html", ".htm"}:
                text = trafilatura.extract(text, include_tables=True) or ""
        text = clean(text)
        if len(re.sub(r"\[第 \d+ 页\]|\s", "", text)) < 2:
            raise ValueError("未提取到可读内容；图片请使用清晰的文字截图，图示可补充文字说明。")
        return text[:300_000]

    async def read_url(self, url: str) -> tuple[str, str, str]:
        page = await self.fetcher.fetch(url)
        media = page.content_type.lower()
        if "pdf" in media or page.body.startswith(b"%PDF"):
            extension = ".pdf"
        elif media.startswith("image/"):
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
                media.split(";")[0], ".png")
        else:
            return page.title or urlparse(page.url).hostname or url, clean(page.text), page.url
        folder = self.config.data / "downloads"
        folder.mkdir(exist_ok=True)
        path = folder / (hashlib.sha256(page.body).hexdigest() + extension)
        await asyncio.to_thread(path.write_bytes, page.body)
        return page.title or Path(urlparse(url).path).name or url, await self.parse_file(path), page.url

#!/usr/bin/env python3
"""Rebuild Stemzel Framer site into this directory for static production hosting.

Usage:
  python3 build_framer_site.py

Notes:
  - HTML image/font/CSS assets are mirrored under ./assets
  - JS modules are downloaded locally but NOT rewritten (rewriting breaks Framer bundles)
  - Appear animations come from Framer's inline animator (homepage) + React hydration
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = "https://stemzel.framer.ai"
SITE_ID = "14nJLNAeVlyOYddnJ1eXdy"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PAGES = {
    "/": ROOT / "index.html",
    "/privacy-policy": ROOT / "privacy-policy" / "index.html",
    "/terms-of-service": ROOT / "terms-of-service" / "index.html",
    "/404": ROOT / "404.html",
}
HOST_ALLOW = {"framerusercontent.com", "fonts.gstatic.com", "fonts.googleapis.com"}
URL_RE = re.compile(
    r"https://(?:framerusercontent\.com|fonts\.gstatic\.com|fonts\.googleapis\.com)[^\"'\s\)<>`]+"
)
IMPORT_RE = re.compile(r"""(?:from|import)\s*["'](\./[^"']+)["']|import\([`'"](\./[^`'"]+)[`'"]\)""")
CSS_URL_RE = re.compile(r"""url\((['"]?)([^)"']+)\1\)""")
SRCSET_RE = re.compile(r"""(?:srcset|imagesrcset)=["']([^"']+)["']""", re.I)


def clean_url(u: str) -> str:
    u = htmlmod.unescape(u.strip())
    # stop at template / junk
    for stop in ("`", "${", "'", '"'):
        if stop in u and not u.startswith("http"):
            break
    u = u.split("`")[0].split("${")[0]
    return u.rstrip("\\").rstrip(".,;)")


def is_junk_url(url: str) -> bool:
    u = clean_url(url)
    if not u.startswith("https://"):
        return True
    p = urllib.parse.urlparse(u)
    if p.netloc not in HOST_ALLOW:
        return True
    if "${" in u or "`" in u:
        return True
    path = p.path.rstrip("/")
    if path in {"", "/a", "/image", "/images", "/third-party-a", "/assets", "/s"}:
        return True
    # must look like a real asset
    name = path.split("/")[-1]
    if not name or name in {"s"}:
        return True
    return False


def fetch(url: str, retries: int = 3, accept_404: bool = False) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": UA, "Accept": "*/*", "Referer": SITE + "/"},
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                body = e.read()
                if accept_404 and e.code == 404 and body:
                    return body
                if e.code in (429, 500, 502, 503, 504):
                    last = e
                    time.sleep(0.5 * (i + 1))
                    continue
                raise
        except Exception as e:
            last = e
            time.sleep(0.35 * (i + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def local_path_for(url: str) -> Path:
    u = clean_url(url)
    p = urllib.parse.urlparse(u)
    path = urllib.parse.unquote(p.path.lstrip("/"))
    if not path or path.endswith("/"):
        path = (path or "") + "_root"
    if p.query:
        qh = hashlib.md5(p.query.encode()).hexdigest()[:10]
        if "." in Path(path).name:
            stem, ext = path.rsplit(".", 1)
            path = f"{stem}.{qh}.{ext}"
        else:
            path = f"{path}.{qh}"
    return ROOT / "assets" / p.netloc / path


def rel_from(from_path: Path, asset_path: Path) -> str:
    return os.path.relpath(asset_path, start=from_path.parent).replace(os.sep, "/")


def extract_urls_from_text(text: str, base_url: str | None = None) -> set[str]:
    found: set[str] = set()
    for m in URL_RE.findall(text):
        found.add(clean_url(m))
    for m in SRCSET_RE.findall(text):
        for part in m.split(","):
            part = part.strip().split(" ")[0]
            if part.startswith("https://"):
                found.add(clean_url(part))
    for _, u in CSS_URL_RE.findall(text):
        if u.startswith("https://"):
            found.add(clean_url(u))
        elif base_url and u and not u.startswith("data:"):
            absu = urllib.parse.urljoin(base_url, u)
            if urllib.parse.urlparse(absu).netloc in HOST_ALLOW:
                found.add(clean_url(absu))
    if base_url:
        for a, b in IMPORT_RE.findall(text):
            spec = a or b
            if spec and spec.startswith("./"):
                found.add(clean_url(urllib.parse.urljoin(base_url, spec)))
            elif spec and spec.startswith("https://"):
                found.add(clean_url(spec))
    return {u for u in found if not is_junk_url(u)}


def rewrite_html(text: str, file_path: Path) -> str:
    def repl(m: re.Match) -> str:
        raw = clean_url(m.group(0))
        if is_junk_url(raw):
            return m.group(0)
        return rel_from(file_path, local_path_for(raw))

    text = re.sub(
        r'<script[^>]*src="https://events\.framer\.com[^"]*"[^>]*></script>\s*',
        "",
        text,
    )
    return URL_RE.sub(repl, text)


def download_asset(url: str) -> tuple[str, Path | None, set[str], str | None]:
    url = clean_url(url)
    if is_junk_url(url):
        return url, None, set(), "junk"
    dest = local_path_for(url)
    # Never rewrite JS module bodies
    is_module = dest.suffix.lower() in {".mjs", ".js"}
    if dest.exists() and dest.stat().st_size > 0:
        new_urls = set()
        if is_module or dest.suffix.lower() in {".css", ".svg", ".json", ".html"} or "fonts.googleapis" in url:
            try:
                new_urls = extract_urls_from_text(dest.read_text(encoding="utf-8", errors="ignore"), base_url=url)
            except Exception:
                pass
        return url, dest, new_urls, None
    try:
        data = fetch(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        new_urls = set()
        if is_module or dest.suffix.lower() in {".css", ".svg", ".json", ".html"} or "fonts.googleapis" in url:
            try:
                new_urls = extract_urls_from_text(data.decode("utf-8", errors="ignore"), base_url=url)
            except Exception:
                pass
        return url, dest, new_urls, None
    except Exception as e:
        p = urllib.parse.urlparse(url)
        if p.query:
            try:
                base = urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
                data = fetch(base)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                return url, dest, set(), None
            except Exception as e2:
                return url, None, set(), f"{e} | {e2}"
        return url, None, set(), str(e)


def download_site_modules() -> None:
    site_dir = ROOT / "assets" / "framerusercontent.com" / "sites" / SITE_ID
    site_dir.mkdir(parents=True, exist_ok=True)
    base = f"https://framerusercontent.com/sites/{SITE_ID}/"
    names: set[str] = set()
    for html in PAGES.values():
        if html.exists():
            for m in re.findall(rf"sites/{SITE_ID}/([^\"'\\s>]+\.mjs)", html.read_text(encoding="utf-8", errors="ignore")):
                names.add(m)
    pending, seen = set(names), set()
    while pending:
        batch = sorted(pending - seen)
        pending = set()
        for name in batch:
            seen.add(name)
            dest = site_dir / name
            data = fetch(base + name)
            dest.write_bytes(data)
            text = data.decode("utf-8", errors="ignore")
            for a, b in IMPORT_RE.findall(text):
                spec = a or b
                if spec.startswith("./"):
                    n = spec[2:]
                    if n not in seen:
                        pending.add(n)
            print(f"  module {name} ({len(data)})")


def main() -> None:
    print("=== Pages ===")
    page_texts: dict[Path, str] = {}
    for route, dest in PAGES.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = fetch(SITE + route, accept_404=(route == "/404"))
        text = data.decode("utf-8", errors="replace")
        if route == "/terms-of-service":
            text = text.replace("<title>Privacy Policy</title>", "<title>Terms of Service</title>", 1)
            text = re.sub(r'(property="og:title" content=")Privacy Policy(")', r"\1Terms of Service\2", text, count=1)
            text = re.sub(r'(name="twitter:title" content=")Privacy Policy(")', r"\1Terms of Service\2", text, count=1)
        page_texts[dest] = text
        print(f"  {route} -> {dest.relative_to(ROOT)} ({len(text)})")

    queue: set[str] = set()
    for text in page_texts.values():
        queue |= extract_urls_from_text(text)
    # Prefer non-mjs from HTML first; modules handled separately pristine
    queue = {u for u in queue if not u.endswith(".mjs") and "/sites/" not in u}

    print(f"=== Assets seed {len(queue)} ===")
    downloaded, failed, pending = {}, [], set(queue)
    while pending:
        batch = sorted(pending)
        pending = set()
        with ThreadPoolExecutor(max_workers=14) as ex:
            futs = [ex.submit(download_asset, u) for u in batch]
            for fut in as_completed(futs):
                url, dest, new_urls, err = fut.result()
                if err and err != "junk":
                    failed.append(f"{url} :: {err}")
                elif dest is not None:
                    downloaded[url] = dest
                    for nu in new_urls:
                        if nu not in downloaded and not is_junk_url(nu) and not nu.endswith(".mjs"):
                            pending.add(nu)

    print(f"=== Modules ===")
    download_site_modules()

    print("=== Rewrite HTML ===")
    for dest, text in page_texts.items():
        dest.write_text(rewrite_html(text, dest), encoding="utf-8")
        print(f"  wrote {dest.relative_to(ROOT)}")

    alt = ROOT / "404" / "index.html"
    alt.parent.mkdir(parents=True, exist_ok=True)
    alt.write_text(rewrite_html(page_texts[ROOT / "404.html"], alt), encoding="utf-8")

    (ROOT / "_redirects").write_text(
        "/privacy-policy /privacy-policy/index.html 200\n"
        "/privacy-policy/ /privacy-policy/index.html 200\n"
        "/terms-of-service /terms-of-service/index.html 200\n"
        "/terms-of-service/ /terms-of-service/index.html 200\n"
        "/* /404.html 404\n"
    )
    (ROOT / "vercel.json").write_text(
        json.dumps(
            {
                "cleanUrls": True,
                "trailingSlash": False,
                "rewrites": [
                    {"source": "/privacy-policy", "destination": "/privacy-policy/index.html"},
                    {"source": "/terms-of-service", "destination": "/terms-of-service/index.html"},
                ],
            },
            indent=2,
        )
        + "\n"
    )

    files = [p for p in ROOT.rglob("*") if p.is_file() and "_legacy" not in p.parts]
    manifest = {
        "site": SITE,
        "pages": list(PAGES),
        "assets": len(downloaded),
        "failed": failed[:50],
        "file_count": len(files),
        "size_bytes": sum(p.stat().st_size for p in files),
    }
    (ROOT / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"DONE files={manifest['file_count']} size_mb={manifest['size_bytes']/1e6:.1f} failed={len(failed)}")


if __name__ == "__main__":
    main()

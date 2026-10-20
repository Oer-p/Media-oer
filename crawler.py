
#!/usr/bin/env python3
import os
import re
import time
import socket
import ipaddress
import logging
import xml.etree.ElementTree as ET

from collections import deque
from urllib.parse import (
    urljoin, urlparse, urldefrag, parse_qsl, urlencode, urlunparse
)
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# KONFIGURASI OoeR CRAWLER
# =========================================================

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TABLE = "oeer_search_index"
SITES_FILE = os.getenv("SITES_FILE", "sites.txt")

MAX_PAGES = int(os.getenv("MAX_PAGES", "100"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "3"))
DELAY_SECONDS = float(os.getenv("DELAY_SECONDS", "1.5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
MAX_BYTES = int(os.getenv("MAX_BYTES", str(2 * 1024 * 1024)))

USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "OoeRCrawler/0.1 (+https://oer-p.github.io/Media-oer/)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

TRACKING_PARAMS = {
    "fbclid", "gclid", "utm_source", "utm_medium",
    "utm_campaign", "utm_term", "utm_content"
}

robots_cache = {}


# =========================================================
# NORMALISASI DAN VALIDASI URL
# =========================================================

def normalize_url(url):
    if not url:
        return None

    url, _ = urldefrag(url.strip())
    p = urlparse(url)

    if p.scheme.lower() not in ("http", "https"):
        return None

    if not p.hostname or p.username or p.password:
        return None

    host = p.hostname.lower().rstrip(".")

    try:
        port = p.port
    except ValueError:
        return None

    netloc = host

    if port and not (
        (p.scheme.lower() == "http" and port == 80)
        or (p.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", p.path or "/")

    params = [
        (key, value)
        for key, value in parse_qsl(
            p.query, keep_blank_values=True
        )
        if key.lower() not in TRACKING_PARAMS
    ]

    return urlunparse((
        p.scheme.lower(),
        netloc,
        path,
        "",
        urlencode(params, doseq=True),
        ""
    ))


def is_public_host(hostname):
    try:
        addresses = {
            ipaddress.ip_address(hostname)
        }
    except ValueError:
        try:
            records = socket.getaddrinfo(
                hostname, None, type=socket.SOCK_STREAM
            )
            addresses = {
                ipaddress.ip_address(record[4][0])
                for record in records
            }
        except (OSError, ValueError):
            return False

    return bool(addresses) and all(
        address.is_global for address in addresses
    )


def host_allowed(url, allowed_hosts):
    host = (urlparse(url).hostname or "").lower().rstrip(".")

    return any(
        host == allowed
        or host.endswith("." + allowed)
        for allowed in allowed_hosts
    )


def safe_get(url):
    """
    Mengambil URL publik tanpa mengikuti redirect otomatis.
    Redirect diperiksa dan dibatasi maksimal tiga kali.
    """
    current = normalize_url(url)

    for _ in range(4):
        if not current:
            raise ValueError("URL tidak valid")

        parsed = urlparse(current)

        if not parsed.hostname or not is_public_host(parsed.hostname):
            raise ValueError("Host bukan alamat IP publik")

        response = session.get(
            current,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True
        )

        if response.is_redirect:
            location = response.headers.get("Location")
            response.close()

            if not location:
                raise ValueError("Redirect tanpa Location")

            current = normalize_url(urljoin(current, location))
            continue

        response.raise_for_status()
        return response

    raise ValueError("Terlalu banyak redirect")


def read_limited(response):
    chunks = []
    total = 0

    for chunk in response.iter_content(16384):
        if not chunk:
            continue

        total += len(chunk)

        if total > MAX_BYTES:
            raise ValueError("Ukuran respons melewati batas")

        chunks.append(chunk)

    return b"".join(chunks)


# =========================================================
# MEMBACA DAFTAR SITUS YANG DIIZINKAN
# =========================================================

def load_sites():
    seeds = []

    with open(SITES_FILE, encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            url = normalize_url(line)

            if url:
                seeds.append(url)

    allowed_hosts = {
        (urlparse(url).hostname or "").lower().rstrip(".")
        for url in seeds
    }

    if not seeds:
        raise RuntimeError(
            "sites.txt kosong. Tambahkan URL awal terlebih dahulu."
        )

    return list(dict.fromkeys(seeds)), allowed_hosts


# =========================================================
# MEMATUHI ROBOTS.TXT
# =========================================================

def can_fetch(url):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in robots_cache:
        parser = RobotFileParser()
        robots_url = origin + "/robots.txt"
        parser.set_url(robots_url)

        try:
            response = safe_get(robots_url)

            try:
                text = read_limited(response).decode(
                    "utf-8", errors="replace"
                )
            finally:
                response.close()

            parser.parse(text.splitlines())

        except Exception as exc:
            # Jika robots.txt tidak bisa diperiksa,
            # crawler memilih untuk tidak merayapi situs itu.
            logging.warning(
                "Tidak dapat memeriksa robots.txt %s: %s",
                origin, exc
            )
            parser.parse([
                "User-agent: *",
                "Disallow: /"
            ])

        robots_cache[origin] = parser

    return robots_cache[origin].can_fetch(USER_AGENT, url)


# =========================================================
# MEMBACA SITEMAP XML
# =========================================================

def sitemap_urls(sitemap_url, allowed_hosts, depth=0):
    if depth > 3:
        return []

    results = []

    try:
        response = safe_get(sitemap_url)

        try:
            data = read_limited(response)
        finally:
            response.close()

        root = ET.fromstring(data)
        root_type = root.tag.rsplit("}", 1)[-1].lower()

        if root_type == "sitemapindex":
            nodes = root.findall(".//{*}sitemap/{*}loc")

            for node in nodes:
                child_url = normalize_url(node.text or "")

                if (
                    child_url
                    and host_allowed(child_url, allowed_hosts)
                ):
                    results.extend(
                        sitemap_urls(
                            child_url,
                            allowed_hosts,
                            depth + 1
                        )
                    )

        elif root_type == "urlset":
            nodes = root.findall(".//{*}url/{*}loc")

            for node in nodes:
                page_url = normalize_url(node.text or "")

                if (
                    page_url
                    and host_allowed(page_url, allowed_hosts)
                ):
                    results.append(page_url)

    except Exception as exc:
        logging.warning(
            "Sitemap gagal dibaca %s: %s",
            sitemap_url, exc
        )

    return results


def discover_sitemaps(seeds, allowed_hosts):
    sitemap_list = []

    for seed in seeds:
        parsed = urlparse(seed)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Cari deklarasi sitemap dalam robots.txt.
        try:
            response = safe_get(origin + "/robots.txt")

            try:
                robots_text = read_limited(response).decode(
                    "utf-8", errors="replace"
                )
            finally:
                response.close()

            for line in robots_text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_url = normalize_url(
                        line.split(":", 1)[1].strip()
                    )

                    if (
                        sitemap_url
                        and host_allowed(
                            sitemap_url, allowed_hosts
                        )
                    ):
                        sitemap_list.append(sitemap_url)

        except Exception as exc:
            logging.info(
                "Robots/sitemap tidak tersedia untuk %s: %s",
                origin, exc
            )

        # Coba lokasi sitemap standar.
        sitemap_list.append(origin + "/sitemap.xml")

    found = []

    for sitemap_url in dict.fromkeys(sitemap_list):
        found.extend(
            sitemap_urls(sitemap_url, allowed_hosts)
        )
        time.sleep(DELAY_SECONDS)

    return list(dict.fromkeys(found))


# =========================================================
# MENGEKSTRAK KONTEN HALAMAN HTML
# =========================================================

def extract_page(url, html):
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = (
        title_tag.get_text(" ", strip=True)
        if title_tag else ""
    )

    description_tag = soup.find(
        "meta",
        attrs={"name": re.compile("^description$", re.I)}
    )

    description = (
        description_tag.get("content", "").strip()
        if description_tag else ""
    )

    keywords_tag = soup.find(
        "meta",
        attrs={"name": re.compile("^keywords$", re.I)}
    )

    keywords = (
        keywords_tag.get("content", "").strip()
        if keywords_tag else ""
    )

    canonical_tag = soup.find(
        "link",
        rel=lambda value: value and "canonical" in value
    )

    canonical_url = url

    if canonical_tag and canonical_tag.get("href"):
        candidate = normalize_url(
            urljoin(url, canonical_tag["href"])
        )

        if candidate:
            canonical_url = candidate

    # Ambil tautan sebelum membersihkan konten.
    links = []

    for anchor in soup.find_all("a", href=True):
        link = normalize_url(
            urljoin(url, anchor["href"])
        )

        if link:
            links.append(link)

    for element in soup([
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "form",
        "nav",
        "footer"
    ]):
        element.decompose()

    main = (
        soup.find("main")
        or soup.find("article")
        or soup.body
        or soup
    )

    content = main.get_text(" ", strip=True)
    content = re.sub(r"\s+", " ", content).strip()

    if not title:
        title = (
            urlparse(url).path.strip("/")
            or urlparse(url).hostname
            or url
        )

    return {
        "url": canonical_url,
        "title": title[:500],
        "description": description[:2000],
        "content": content[:50000],
        "keywords": keywords[:2000],
        "source_type": "web",
        "language": "id",
        "is_indexed": bool(content),
        "links": list(dict.fromkeys(links))
    }


# =========================================================
# MENYIMPAN / MEMPERBARUI INDEKS SUPABASE
# =========================================================

def save_to_supabase(page):
    endpoint = (
        f"{SUPABASE_URL}/rest/v1/{TABLE}"
    )

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": (
            f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
        ),
        "Content-Type": "application/json",
        "Prefer": (
            "resolution=merge-duplicates,return=minimal"
        )
    }

    row = {
        key: page[key]
        for key in (
            "url",
            "title",
            "description",
            "content",
            "keywords",
            "source_type",
            "language",
            "is_indexed"
        )
    }

    response = requests.post(
        endpoint,
        params={"on_conflict": "url"},
        headers=headers,
        json=row,
        timeout=REQUEST_TIMEOUT
    )

    try:
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Supabase HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
    finally:
        response.close()


# =========================================================
# MESIN CRAWLER
# =========================================================

def main():
    seeds, allowed_hosts = load_sites()

    logging.info("Host yang diizinkan: %s", sorted(allowed_hosts))
    logging.info("Memulai pencarian sitemap...")

    discovered = discover_sitemaps(
        seeds, allowed_hosts
    )

    queue = deque()
    seen = set()

    for url in discovered + seeds:
        url = normalize_url(url)

        if (
            url
            and url not in seen
            and host_allowed(url, allowed_hosts)
        ):
            seen.add(url)
            queue.append((url, 0))

    indexed = 0
    failed = 0
    skipped = 0

    while queue and indexed < MAX_PAGES:
        url, depth = queue.popleft()

        if not host_allowed(url, allowed_hosts):
            skipped += 1
            continue

        if not can_fetch(url):
            logging.info("Dilewati robots.txt: %s", url)
            skipped += 1
            continue

        try:
            response = safe_get(url)

            try:
                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

                if "text/html" not in content_type:
                    logging.info("Bukan HTML: %s", url)
                    skipped += 1
                    continue

                final_url = normalize_url(response.url)

                if (
                    not final_url
                    or not host_allowed(
                        final_url, allowed_hosts
                    )
                ):
                    skipped += 1
                    continue

                html = read_limited(response).decode(
                    response.encoding or "utf-8",
                    errors="replace"
                )

            finally:
                response.close()

            page = extract_page(final_url, html)

            if not host_allowed(
                page["url"], allowed_hosts
            ):
                logging.warning(
                    "Canonical keluar dari daftar izin: %s",
                    page["url"]
                )
                skipped += 1
                continue

            save_to_supabase(page)

            indexed += 1

            logging.info(
                "[%s/%s] Berhasil: %s",
                indexed, MAX_PAGES, page["url"]
            )

            if depth < MAX_DEPTH:
                for link in page["links"]:
                    if (
                        link not in seen
                        and host_allowed(
                            link, allowed_hosts
                        )
                    ):
                        seen.add(link)
                        queue.append((link, depth + 1))

        except Exception as exc:
            failed += 1
            logging.warning(
                "Gagal memproses %s: %s",
                url, exc
            )

        time.sleep(DELAY_SECONDS)

    logging.info(
        "Crawler selesai | terindeks=%s | gagal=%s "
        "| dilewati=%s | antrean_sisa=%s",
        indexed, failed, skipped, len(queue)
    )


if __name__ == "__main__":
    main()

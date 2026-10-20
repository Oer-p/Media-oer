import os
import re
import time
import logging
import ipaddress
import socket
import xml.etree.ElementTree as ET

from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


# ============================================================
# OoeR CRAWLER - TAHAP 1
# Mengindeks sitemap dan situs pilihan ke Supabase
# ============================================================

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://jhopfzmyxuafdbwwkjbf.supabase.co"
).rstrip("/")

SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

MAX_PAGES = int(os.environ.get("MAX_PAGES", "30"))
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "2"))
CRAWL_DELAY = float(os.environ.get("CRAWL_DELAY", "2"))

USER_AGENT = "OoeRCrawler/1.0 (+https://oer-p.github.io/Media-oer/)"

INDEX_ENDPOINT = (
    SUPABASE_URL + "/rest/v1/oeer_search_index"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
})

robots_cache = {}
visited = set()
allowed_hosts = set()
last_request_time = {}


def valid_public_host(host):
    """Tolak alamat lokal atau IP privat."""
    if not host:
        return False

    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return False

    try:
        addresses = socket.getaddrinfo(host, None)
        if not addresses:
            return False

        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                return False

        return True
    except (socket.gaierror, ValueError, OSError):
        return False


def normalize_url(url):
    """Normalisasi URL dan hapus fragmen."""
    if not url:
        return None

    url = urldefrag(url.strip())[0]
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    if not parsed.hostname or parsed.username or parsed.password:
        return None

    if parsed.port not in (None, 80, 443):
        return None

    if not valid_public_host(parsed.hostname):
        return None

    return url


def host_allowed(url):
    parsed = urlparse(url)
    return parsed.hostname and parsed.hostname.lower() in allowed_hosts


def polite_wait(url):
    host = urlparse(url).hostname
    now = time.time()
    previous = last_request_time.get(host, 0)
    wait = CRAWL_DELAY - (now - previous)

    if wait > 0:
        time.sleep(wait)

    last_request_time[host] = time.time()


def get_response(url, timeout=20):
    """
    Ambil halaman dengan validasi setiap tujuan redirect.
    Redirect ke host di luar daftar yang diizinkan ditolak.
    """
    current = normalize_url(url)

    if not current or not host_allowed(current):
        logging.warning("URL di luar daftar situs: %s", url)
        return None

    for _ in range(6):
        if not current or not host_allowed(current):
            logging.warning("Redirect di luar daftar situs ditolak.")
            return None

        parsed = urlparse(current)
        if not valid_public_host(parsed.hostname):
            logging.warning("Host tidak publik: %s", parsed.hostname)
            return None

        polite_wait(current)

        try:
            response = session.get(
                current,
                timeout=timeout,
                allow_redirects=False,
                stream=True
            )

            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()

                if not location:
                    return None

                next_url = normalize_url(urljoin(current, location))

                if not next_url or not host_allowed(next_url):
                    logging.warning("Redirect diblokir: %s", next_url)
                    return None

                current = next_url
                continue

            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if not (
                "text/html" in content_type
                or "application/xhtml+xml" in content_type
                or "xml" in content_type
            ):
                response.close()
                return None

            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > 5_000_000:
                response.close()
                logging.warning("Halaman terlalu besar: %s", current)
                return None

            body = response.raw.read(5_000_001, decode_content=True)
            response.close()

            if len(body) > 5_000_000:
                logging.warning("Halaman terlalu besar: %s", current)
                return None

            response._content = body
            response._content_consumed = True
            response.url = current
            return response

        except (requests.RequestException, ValueError, OSError) as exc:
            logging.warning("Gagal mengambil %s: %s", current, exc)
            return None

    logging.warning("Terlalu banyak redirect: %s", url)
    return None


def load_sites():
    """Baca situs awal dari sites.txt."""
    seeds = []

    if not os.path.isfile("sites.txt"):
        logging.error("File sites.txt belum tersedia.")
        return seeds

    with open("sites.txt", "r", encoding="utf-8") as file:
        for line in file:
            item = line.strip()

            if not item or item.startswith("#"):
                continue

            url = normalize_url(item)
            if url:
                seeds.append(url)

    # Hanya host yang dicantumkan eksplisit di sites.txt
    for url in seeds:
        allowed_hosts.add(urlparse(url).hostname.lower())

    return list(dict.fromkeys(seeds))


def get_robots_parser(url):
    """Ambil robots.txt untuk setiap host."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = origin + "/robots.txt"

    if origin in robots_cache:
        return robots_cache[origin]

    parser = RobotFileParser()
    parser.set_url(robots_url)

    response = get_response(robots_url)

    if response is None:
        # Jika robots.txt tidak dapat diperiksa, jangan crawl host ini.
        parser.disallow_all = True
        logging.warning("Robots.txt tidak bisa diperiksa: %s", origin)
    else:
        try:
            parser.parse(response.text.splitlines())
        except Exception:
            parser.disallow_all = True
            logging.warning("Robots.txt gagal dibaca: %s", origin)

    robots_cache[origin] = parser
    return parser


def robots_allows(url):
    parser = get_robots_parser(url)

    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return False


def extract_sitemap_locations(xml_text):
    """Baca URL dari sitemap biasa maupun sitemap index."""
    locations = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return locations

    for element in root.iter():
        if element.tag.split("}")[-1].lower() == "loc":
            if element.text:
                locations.append(element.text.strip())

    return locations


def find_sitemap_urls(seed_urls):
    """Cari sitemap dari robots.txt dan alamat sitemap umum."""
    found = []
    sitemap_files = deque()
    seen_sitemaps = set()

    for seed in seed_urls:
        parsed = urlparse(seed)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        robots = get_robots_parser(seed)

        try:
            for sitemap_url in robots.site_maps() or []:
                sitemap_files.append(sitemap_url)
        except Exception:
            pass

        sitemap_files.append(origin + "/sitemap.xml")

    while sitemap_files and len(seen_sitemaps) < 100:
        sitemap_url = normalize_url(sitemap_files.popleft())

        if (
            not sitemap_url
            or not host_allowed(sitemap_url)
            or sitemap_url in seen_sitemaps
        ):
            continue

        seen_sitemaps.add(sitemap_url)

        if not robots_allows(sitemap_url):
            logging.info("Sitemap diblokir robots.txt: %s", sitemap_url)
            continue

        response = get_response(sitemap_url)
        if response is None:
            continue

        locations = extract_sitemap_locations(response.text)

        for location in locations:
            location = normalize_url(urljoin(sitemap_url, location))

            if not location or not host_allowed(location):
                continue

            path = urlparse(location).path.lower()

            if path.endswith(".xml") or "sitemap" in path:
                sitemap_files.append(location)
            else:
                found.append(location)

    return list(dict.fromkeys(found))


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def extract_page(url, html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup([
        "script", "style", "noscript", "iframe",
        "svg", "canvas", "nav", "footer", "header"
    ]):
        tag.decompose()

    title = ""
    if soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))

    description = ""
    meta = soup.find(
        "meta",
        attrs={"name": re.compile(r"^description$", re.I)}
    )
    if meta:
        description = clean_text(meta.get("content", ""))

    if not description:
        meta = soup.find(
            "meta",
            attrs={"property": "og:description"}
        )
        if meta:
            description = clean_text(meta.get("content", ""))

    keywords = ""
    meta_keywords = soup.find(
        "meta",
        attrs={"name": re.compile(r"^keywords$", re.I)}
    )
    if meta_keywords:
        keywords = clean_text(meta_keywords.get("content", ""))

    content_root = soup.find("main") or soup.find("article") or soup.body or soup
    content = clean_text(content_root.get_text(" ", strip=True))
    content = content[:30000]

    links = []

    for anchor in soup.find_all("a", href=True):
        absolute = normalize_url(urljoin(url, anchor["href"]))

        if absolute and host_allowed(absolute):
            links.append(absolute)

    return {
        "url": url,
        "title": title or url,
        "description": description[:2000],
        "content": content,
        "keywords": keywords[:2000],
        "links": list(dict.fromkeys(links))
    }


def save_to_supabase(page):
    """Simpan hasil indeks menggunakan service role di server Actions."""
    if not SUPABASE_KEY:
        raise RuntimeError(
            "Secret SUPABASE_SERVICE_ROLE_KEY belum dikonfigurasi."
        )

    payload = {
        "url": page["url"],
        "title": page["title"],
        "description": page["description"],
        "content": page["content"],
        "keywords": page["keywords"],
        "source_type": "website",
        "language": "id",
        "is_indexed": True,
        "last_crawled_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    }

    response = requests.post(
        INDEX_ENDPOINT,
        params={"on_conflict": "url"},
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer " + SUPABASE_KEY,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"
        },
        json=payload,
        timeout=30
    )

    if not response.ok:
        logging.error(
            "Supabase gagal (%s) untuk %s: %s",
            response.status_code,
            page["url"],
            response.text[:500]
        )
        response.raise_for_status()


def main():
    if not SUPABASE_KEY:
        raise RuntimeError(
            "Secret SUPABASE_SERVICE_ROLE_KEY belum diatur di GitHub Actions."
        )

    if MAX_PAGES < 1:
        raise ValueError("MAX_PAGES harus minimal 1.")

    seeds = load_sites()

    if not seeds:
        raise RuntimeError(
            "sites.txt kosong atau tidak berisi URL yang valid."
        )

    logging.info("Host yang diizinkan: %s", ", ".join(sorted(allowed_hosts)))
    logging.info("Memuat sitemap...")

    sitemap_urls = find_sitemap_urls(seeds)

    queue = deque()
    for url in seeds + sitemap_urls:
        queue.append((url, 0))

    indexed = 0

    while queue and indexed < MAX_PAGES:
        url, depth = queue.popleft()
        url = normalize_url(url)

        if not url or url in visited or not host_allowed(url):
            continue

        visited.add(url)

        if not robots_allows(url):
            logging.info("Dilewati robots.txt: %s", url)
            continue

        response = get_response(url)

        if response is None:
            continue

        content_type = response.headers.get("Content-Type", "").lower()

        if "html" not in content_type and "xhtml" not in content_type:
            continue

        page = extract_page(response.url, response.text)

        if not page["content"]:
            logging.info("Tidak ada isi halaman: %s", url)
            continue

        try:
            save_to_supabase(page)
            indexed += 1
            logging.info(
                "Terindeks %d/%d: %s",
                indexed, MAX_PAGES, page["url"]
            )
        except Exception:
            logging.exception("Gagal menyimpan: %s", page["url"])

        if depth < MAX_DEPTH:
            for link in page["links"]:
                if link not in visited:
                    queue.append((link, depth + 1))

    logging.info(
        "Selesai. Halaman berhasil diindeks: %d. URL dikunjungi: %d",
        indexed, len(visited)
    )


if __name__ == "__main__":
    main()

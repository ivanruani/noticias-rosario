#!/usr/bin/env python3
"""
Genera data.json con las 15 noticias mas recientes de Rosario3 y La Capital.

Pensado para correr solo, disparado por GitHub Actions cada cierto tiempo
(ver .github/workflows/update-news.yml). No requiere ningun servidor propio:
el archivo index.html de este mismo repo simplemente lee el data.json que
este script deja escrito.

Fuentes:
- La Capital: tiene RSS real (titulo, link, fecha) pero sin imagen, asi que
  para la imagen visitamos cada nota y leemos su etiqueta og:image.
- Rosario3: no publica RSS, asi que leemos directamente su portada y
  tomamos los primeros enlaces a notas en el orden en que aparecen (que es
  el orden editorial/cronologico de la propia web). Para la imagen y para
  desempatar notas del mismo dia usamos tambien og:image y el orden de
  aparicion en portada.
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}
TIMEOUT = 20
MAX_ITEMS = 15

LACAPITAL_RSS = "https://www.lacapital.com.ar/rss/ultimas-noticias.xml"
ROSARIO3_HOME = "https://www.rosario3.com/"

# Enlaces de notas de Rosario3 terminan siempre con -YYYYMMDD-NNNN.html
ARTICLE_LINK_RE = re.compile(r"-(\d{4})(\d{2})(\d{2})-\d{4}\.html?$")


def safe_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  ! error al descargar {url}: {e}", file=sys.stderr)
        return None


def get_og_image(url):
    """Visita una nota y devuelve su imagen principal (og:image), si existe."""
    r = safe_get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("meta", property="og:image")
    if tag and tag.get("content"):
        return tag["content"].strip()
    tag = soup.find("meta", attrs={"name": "twitter:image"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def fetch_lacapital():
    print("Descargando RSS de La Capital...")
    r = safe_get(LACAPITAL_RSS)
    items = []
    if not r:
        return items

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"  ! error parseando RSS de La Capital: {e}", file=sys.stderr)
        return items

    for item in root.findall(".//item")[:MAX_ITEMS]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()

        if not title or not link:
            continue

        try:
            pub_dt = parsedate_to_datetime(pub_raw)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pub_dt = datetime.now(timezone.utc)

        print(f"  -> {title[:60]}")
        image = get_og_image(link)
        items.append({
            "source": "La Capital",
            "title": title,
            "link": link,
            "image": image,
            "pubDate": pub_dt.astimezone(timezone.utc).isoformat(),
            "approxTime": False,
        })

    return items


ROSARIO3_FALLBACK_URLS = [
    ROSARIO3_HOME,
    "https://www.rosario3.com/seccion/ultimas-noticias/",
]

BLOCK_MARKERS = (
    "just a moment", "attention required", "cloudflare", "captcha",
    "access denied", "are you a human", "bot detection",
)


def fetch_rosario3():
    items = []
    candidates = []
    seen = set()

    for url in ROSARIO3_FALLBACK_URLS:
        print(f"Descargando {url} ...")
        r = safe_get(url)
        if not r:
            continue

        lowered = r.text.lower()
        looks_blocked = any(marker in lowered for marker in BLOCK_MARKERS)
        print(
            f"  status={r.status_code} bytes={len(r.content)} "
            f"posible_bloqueo={looks_blocked}"
        )

        soup = BeautifulSoup(r.text, "html.parser")
        found_here = 0
        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"])
            if not ARTICLE_LINK_RE.search(full):
                continue
            if full in seen:
                continue
            text = a.get_text(strip=True)
            if not text or len(text) < 12:
                # descarta enlaces "vacios" (el <a> que solo envuelve la
                # imagen, botones de compartir, etc.)
                continue
            seen.add(full)
            candidates.append((full, text))
            found_here += 1
            if len(candidates) >= MAX_ITEMS:
                break

        print(f"  -> {found_here} enlaces de notas encontrados en esta pagina")

        if not found_here:
            # Ayuda a diagnosticar si el bloqueo es un challenge anti-bot,
            # un cambio de plantilla, etc.
            snippet = re.sub(r"\s+", " ", r.text)[:300]
            print(f"  primeros caracteres de la respuesta: {snippet!r}")

        if len(candidates) >= MAX_ITEMS:
            break

    for rank, (link, title) in enumerate(candidates):
        print(f"  -> {title[:60]}")
        match = ARTICLE_LINK_RE.search(link)
        if match:
            y, m, d = map(int, match.groups())
            base_date = datetime(y, m, d, 12, 0, tzinfo=timezone.utc)
        else:
            base_date = datetime.now(timezone.utc)

        # Rosario3 no publica la hora exacta en portada: usamos el orden de
        # aparicion en la home (rank) para ordenar las notas de un mismo dia
        # de mas nueva a mas vieja.
        pub_dt = base_date - timedelta(minutes=rank)

        image = get_og_image(link)
        items.append({
            "source": "Rosario3",
            "title": title,
            "link": link,
            "image": image,
            "pubDate": pub_dt.isoformat(),
            "approxTime": True,
        })

    return items


def main():
    lacapital = fetch_lacapital()
    rosario3 = fetch_rosario3()

    data = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sites": {
            "rosario3": rosario3,
            "lacapital": lacapital,
        },
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(rosario3)} notas de Rosario3, {len(lacapital)} notas de La Capital")


if __name__ == "__main__":
    main()

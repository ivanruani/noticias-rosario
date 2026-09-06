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
        # Algunos sitios no declaran bien el charset en la respuesta y
        # requests cae a ISO-8859-1 por defecto, lo que arruina las tildes.
        # Estos sitios son en espanol y en la practica siempre UTF-8.
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "iso-8859-1".upper()):
            r.encoding = "utf-8"
        return r
    except Exception as e:
        print(f"  ! error al descargar {url}: {e}", file=sys.stderr)
        return None


def get_og_image(url):
    """Visita una nota y devuelve su imagen principal (og:image), si existe."""
    r = safe_get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    tag = soup.find("meta", property="og:image")
    if tag and tag.get("content"):
        return tag["content"].strip()
    tag = soup.find("meta", attrs={"name": "twitter:image"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def get_og_meta(url):
    """Visita una nota y devuelve (titulo, imagen) leyendo sus meta og:*.

    En Rosario3 el link de la portada muchas veces envuelve solo la miniatura
    (sin texto visible), asi que el titulo real lo sacamos de la propia nota
    en vez de confiar en el texto del <a> de la portada.
    """
    r = safe_get(url)
    if not r:
        return None, None
    soup = BeautifulSoup(r.text, "lxml")

    title = None
    tag = soup.find("meta", property="og:title")
    if tag and tag.get("content"):
        title = tag["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    image = None
    tag = soup.find("meta", property="og:image")
    if tag and tag.get("content"):
        image = tag["content"].strip()
    if not image:
        tag = soup.find("meta", attrs={"name": "twitter:image"})
        if tag and tag.get("content"):
            image = tag["content"].strip()

    return title, image


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

# Un poco mas del limite final: algunas notas fallan al visitarlas (timeout,
# 404, etc.) y esto da margen para igual completar los 15 finales.
CANDIDATE_POOL = MAX_ITEMS + 10


def fetch_rosario3():
    items = []
    candidate_links = []
    seen = set()

    for url in ROSARIO3_FALLBACK_URLS:
        print(f"Descargando {url} ...")
        r = safe_get(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "lxml")
        found_here = 0
        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"])
            if not ARTICLE_LINK_RE.search(full):
                continue
            if full in seen:
                continue
            # OJO: en Rosario3 muchos de estos <a> solo envuelven la
            # miniatura (sin texto visible), asi que NO filtramos por texto
            # del link acá -- el titulo real se saca de la propia nota
            # (ver get_og_meta), no de la portada.
            seen.add(full)
            candidate_links.append(full)
            found_here += 1
            if len(candidate_links) >= CANDIDATE_POOL:
                break

        print(f"  -> {found_here} enlaces de notas encontrados en esta pagina")

        if len(candidate_links) >= CANDIDATE_POOL:
            break

    for rank, link in enumerate(candidate_links):
        if len(items) >= MAX_ITEMS:
            break

        title, image = get_og_meta(link)
        if not title:
            print(f"  ! sin titulo, se descarta: {link}")
            continue

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

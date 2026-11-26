"""Scrape upcoming events from Montreal venues and build a calendar feed.

Run:  python scrape.py
Outputs (in ./docs, served by GitHub Pages):
  events.json   - all events, used by index.html
  calendar.ics  - subscribable iCalendar feed, Events tab
  museums.ics   - Museums tab feed
  movies.ics    - Movies tab feed
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event as ICalEvent

TZ = ZoneInfo("America/Montreal")
OUT = Path(__file__).parent / "docs"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9,fr;q=0.8"})

FR_MONTHS = {"janv": 1, "janvier": 1, "févr": 2, "fevr": 2, "février": 2, "fevrier": 2, "mars": 3,
             "avr": 4, "avril": 4, "mai": 5, "juin": 6, "juil": 7, "juillet": 7, "août": 8, "aout": 8,
             "sept": 9, "septembre": 9, "oct": 10, "octobre": 10, "nov": 11, "novembre": 11,
             "déc": 12, "dec": 12, "décembre": 12, "decembre": 12}
EN_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


@dataclass
class Ev:
    source: str
    title: str
    start: datetime | date          # aware datetime, or date for all-day
    url: str
    end: datetime | date | None = None   # for exhibitions: the last day (inclusive)
    location: str = ""
    kind: str = ""                       # "exhibition", or a museum event type
    free: bool | None = None             # True = free, False = paid, None = unknown
    price: str = ""                      # short display text, e.g. "$10" or "$15–20"
    price_lo: float | None = None        # lowest ticket price (0 when free), for the "Under $20" filter
    genres: list[str] = field(default_factory=list)   # e.g. ["Techno", "Electronic"]
    blurb: str = ""                      # short description from the venue (used for tags; not published)
    image: str = ""                      # flyer / poster / photo URL, when the source has one
    desc: str = ""                       # the event's own description, for the detail panel only (not used for tags)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


def money(lo: float | None, hi: float | None = None) -> str:
    """'$10', '$15–20' (rounded to whole dollars)."""
    if lo is None:
        return ""
    a, b = round(lo), round(hi) if hi is not None else None
    return f"${a}" if b is None or b == a else f"${a}–{b}"


def priced(e: "Ev", free: bool | None, lo: float | None = None, hi: float | None = None, text: str = "") -> "Ev":
    """Attach price info to an event. A zero price counts as free."""
    if free is None and lo is not None:
        free = (hi or lo) == 0
    e.free = free
    e.price = "" if free else (text or money(lo, hi))
    e.price_lo = 0.0 if free else lo
    return e


FREE_TEXT = re.compile(r"\b(free|gratuit[e]?|entrée libre|free entry|free admission|no cover)\b", re.I)


def price_from_text(text: str) -> tuple[bool | None, float | None, float | None]:
    """(free, lowest, highest) from a short price string like 'GRATUIT // FREE', '15$ PWYC', '$10.00', '10-20'."""
    text = clean(text)
    if not text:
        return None, None, None
    if FREE_TEXT.search(text) and not re.search(r"\d", text):
        return True, None, None
    nums = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:[.,]\d{1,2})?)", text)]
    if nums:
        return max(nums) == 0, min(nums), max(nums)
    return None, None, None


# ----------------------------------------------------------------- helpers
def get(url, **kw) -> requests.Response:
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r


def soup(url) -> BeautifulSoup:
    return BeautifulSoup(get(url).text, "html.parser")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def iso(s: str) -> datetime:
    """Parse ISO string; naive values are treated as Montreal local time."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def local(d: date, t: time | None) -> datetime | date:
    return datetime.combine(d, t, TZ) if t else d


def guess_year(month: int, day: int) -> int:
    """For dates shown without a year: assume the next occurrence (allowing ~2 months of past)."""
    today = date.today()
    y = today.year
    if date(y, month, day) < today - timedelta(days=60):
        y += 1
    return y


def parse_time(text: str) -> time | None:
    """First clock time in free text: '10PM', '6:30 PM', '20 h 30', '22h', '10p.m.'."""
    t = text.lower().replace(".", "")
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3) == "pm":
            h += 12
        return time(h % 24, mi)
    m = re.search(r"\b(\d{1,2})\s*h\s*(\d{2})?\b", t) or re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m and int(m.group(1)) < 24:
        return time(int(m.group(1)), int(m.group(2) or 0))
    return None


# ----------------------------------------------------------- Resident Advisor
def ra_query(query: str) -> dict:
    r = SESSION.post("https://ra.co/graphql", json={"query": query}, timeout=30,
                     headers={"Content-Type": "application/json", "Referer": "https://ra.co/"})
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


RA_FIELDS = "id title content startTime endTime contentUrl cost venue { name } genres { name } images { filename type }"


def ra_events(kind: str, ra_id: str, source: str) -> list[Ev]:
    """kind = 'venue' or 'promoter'."""
    d = ra_query(f'{{ {kind}(id: "{ra_id}") {{ events(type: LATEST, limit: 100) {{ {RA_FIELDS} }} }} }}')
    out = []
    for e in d[kind]["events"]:
        ev = Ev(source, e["title"], iso(e["startTime"]), "https://ra.co" + e["contentUrl"],
                iso(e["endTime"]) if e.get("endTime") else None, (e.get("venue") or {}).get("name", ""))
        ev.genres = genres_in(" ".join(g["name"] for g in e.get("genres") or []))   # RA's own tags, when set
        ev.image = next((i["filename"] for i in e.get("images") or [] if i.get("type") == "FLYERFRONT"),
                        (e.get("images") or [{}])[0].get("filename", "") if e.get("images") else "")
        ev.desc = e.get("content") or ""
        out.append(priced(ev, *price_from_text(e.get("cost") or "")))
    return out


# ------------------------------------------------------------------ sources
def sat() -> list[Ev]:
    q = """query($after: String) { events(first: 50, after: $after,
             where: {language: EN, futureItemsOnly: true}) {
        nodes { title link datePicker event { infocontent {
            startdate enddate dayhourlength category { slug } tarifs { montant } espace { ... on Espace { title } } } } }
        pageInfo { hasNextPage endCursor } } }"""
    out, after = [], None
    while True:
        r = SESSION.post("https://cms.sat.qc.ca/wp/graphql", json={"query": q, "variables": {"after": after}},
                         timeout=30)
        r.raise_for_status()
        ev = r.json()["data"]["events"]
        for n in ev["nodes"]:
            ic = n["event"]["infocontent"]
            if (ic.get("category") or {}).get("slug") != "concerts":   # "Concerts & DJs" only
                continue
            hours = clean(re.sub(r"<[^>]+>", " ", ic.get("dayhourlength") or ""))
            t = parse_time(hours)
            espace = ic.get("espace") or {}
            loc = "SAT" + (f" – {espace['title']}" if isinstance(espace, dict) and espace.get("title") else "")
            amounts = [float(x["montant"]) for x in (ic.get("tarifs") or []) if x.get("montant") is not None]
            dates = n.get("datePicker") or [ic["startdate"]]
            for ds in dates:
                d = datetime.strptime(ds, "%d/%m/%Y").date()
                e = Ev("SAT", n["title"], local(d, t), n["link"], location=loc)
                out.append(priced(e, None, min(amounts), max(amounts)) if amounts else e)
        if not ev["pageInfo"]["hasNextPage"]:
            return out
        after = ev["pageInfo"]["endCursor"]


def favourite_library() -> list[Ev]:
    s = soup("https://www.favouritelibrary.com/")
    sec = s.find(id="shows")
    out = []
    for h3 in sec.find_all("h3"):
        block = h3.find_parent("div")
        spans = [clean(x.get_text(" ")) for x in block.find_all("span")]
        when = next((x for x in spans if re.search(r"\b\d{4}\b", x)), None)
        if not when:
            continue
        m = re.search(r"([A-Za-z]+) (\d{1,2}), (\d{4})", when)
        d = date(int(m.group(3)), EN_MONTHS[m.group(1).lower()], int(m.group(2)))
        loc = re.sub(r"\s+,", ",", next((x for x in spans if x != when and "," in x), ""))
        out.append(Ev("Favourite Library", clean(h3.get_text()), local(d, parse_time(when)),
                      "https://www.favouritelibrary.com/#shows", location=loc))
    return out


def casa_del_popolo() -> list[Ev]:
    base = "https://casadelpopolo.com"
    s = soup(base + "/en")
    out, seen = [], set()
    for card in s.select("div.p-6"):
        heads = card.select(".heading")
        more = card.find("a", href=re.compile(r"^/en/events/\d{4}-"))
        if len(heads) < 3 or not more or more["href"] in seen:
            continue
        seen.add(more["href"])
        m = re.search(r"([A-Za-z]+) (\d{1,2})\w*, (\d{4})", heads[0].get_text())
        if not m:
            continue
        d = date(int(m.group(3)), EN_MONTHS[m.group(1).lower()], int(m.group(2)))
        divs = [clean(x.get_text()) for x in card.find_all("div", recursive=False)]
        t = next((parse_time(x) for x in divs if parse_time(x)), None)
        ev = Ev("Casa del Popolo", clean(heads[1].get_text()), local(d, t), base + more["href"],
                location=clean(heads[2].get_text()))
        box = card.parent   # the card's picture sits a level or two above the text block
        for _ in range(3):
            if box.find("img") or box.parent is None:
                break
            box = box.parent
        img = box.find("img", src=True)
        ev.image = img["src"] if img and img["src"].startswith("http") else ""
        cost = next((x for x in divs if re.fullmatch(r"(\$\s?[\d.,]+.*|.*\b(free|gratuit)\b.*)", x, re.I)
                     and len(x) < 40), "")
        out.append(priced(ev, *price_from_text(cost)))
    return out


def red_room() -> list[Ev]:
    s = soup("https://leredroom.com/evenements/")
    out = []
    for it in s.select(".nectar-post-grid-item"):
        title = clean(it.select_one(".post-heading").get_text())
        when = clean((it.select_one(".meta-excerpt") or it).get_text())
        m = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)", when)
        if not m or m.group(2).lower().rstrip(".") not in FR_MONTHS:
            continue
        mo, dy = FR_MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1))
        a = it.select_one(".post-heading a[href]")
        url = a["href"].split("#")[0] if a else "https://leredroom.com/evenements/"
        out.append(Ev("Le Red Room", title, date(guess_year(mo, dy), mo, dy), url, location="Le Red Room"))
    return out


def eventbrite_organizer(org_url: str, source: str) -> list[Ev]:
    """All upcoming events listed on an Eventbrite organizer page (eventbrite.ca/o/...)."""
    s = soup(org_url)
    data = json.loads(s.find("script", id="__NEXT_DATA__").string)
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("_type") == "event" and o.get("start_date") and o.get("url"):
                found[o.get("id") or o["url"]] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    out = []
    for e in found.values():
        tz = ZoneInfo(e.get("timezone") or "America/Montreal")
        st = datetime.fromisoformat(f"{e['start_date']}T{e.get('start_time') or '00:00:00'}").replace(tzinfo=tz)
        en = None
        if e.get("end_date"):
            en = datetime.fromisoformat(f"{e['end_date']}T{e.get('end_time') or '23:59:00'}").replace(tzinfo=tz)
        venue = e.get("primary_venue") or {}
        ev = Ev(source, e.get("name") if isinstance(e.get("name"), str) else e.get("name", {}).get("text", ""),
                st.astimezone(TZ), e["url"], en.astimezone(TZ) if en else None,
                venue.get("name", "") if isinstance(venue, dict) else "")
        ev.image = ((e.get("image") or {}).get("url") or "") if isinstance(e.get("image"), dict) else ""
        ta = e.get("ticket_availability") or {}
        lo = (ta.get("minimum_ticket_price") or {}).get("major_value")
        hi = (ta.get("maximum_ticket_price") or {}).get("major_value")
        out.append(priced(ev, True if ta.get("is_free") else None,
                          float(lo) if lo else None, float(hi) if hi else None))
    return out


def _rich_text(o):
    """All the text strings in a CMS rich-text JSON tree (Payload / Lexical)."""
    if isinstance(o, dict):
        if isinstance(o.get("text"), str):
            yield o["text"]
        for v in o.values():
            yield from _rich_text(v)
    elif isinstance(o, list):
        for v in o:
            yield from _rich_text(v)


def turbo_haus() -> list[Ev]:
    base = "https://turbohaus.ca"
    today = date.today()
    months = [(today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1) for i in range(3)]
    # The month grid has dates (recurring shows already expanded) but often no times;
    # the CMS API has each show's start time, keyed by slug.
    slug_time: dict[str, time] = {}
    slug_price: dict[str, str] = {}
    try:
        api = get(f"{base}/api/events", params={"limit": 5000, "depth": 0, "select[slug]": "true",
                                                 "select[startsAt]": "true", "select[priceText]": "true",
                                                 "select[description]": "true"}).json()
        for doc in api.get("docs", []):
            if doc.get("slug") and doc.get("priceText"):
                slug_price[doc["slug"]] = doc["priceText"]
            elif doc.get("slug"):   # no price field: many say it in the text ("$10 + fees", "Gratuit / Free")
                desc = " ".join(_rich_text(doc.get("description")))
                tokens = re.findall(r"\d+(?:[.,]\d{2})?\s?\$|\$\s?\d+(?:[.,]\d{2})?|\bgratuit\w*|\bfree\b|\bpwyc\b", desc, re.I)
                if tokens:
                    slug_price[doc["slug"]] = " ".join(tokens)
            if doc.get("slug") and doc.get("startsAt"):
                t = iso(doc["startsAt"]).time()
                if t != time(0, 0):          # midnight = no time set
                    slug_time[doc["slug"]] = t
    except Exception as ex:
        print(f"    (Turbo Haüs time lookup failed: {ex})")
    out, seen = [], set()
    for y, mo in months:
        s = soup(f"{base}/calendrier/{y}/{mo}?lang=en")
        for a in s.select("a[href^='/calendrier/']"):
            m = re.match(r"/calendrier/(\d{4})/(\d{1,2})/(\d{1,2})/([^/?#]+)", a["href"])
            if not m or a["href"] in seen:
                continue
            title_el = a.select_one("div.font-medium")
            title = a.get("title") or (clean(title_el.get_text()) if title_el else "")
            if not title:
                continue
            seen.add(a["href"])
            t_el = a.select_one(".font-mono")
            t = (parse_time(t_el.get_text()) if t_el else None) or slug_time.get(m.group(4))
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            ev = Ev("Turbo Haüs", title, local(d, t), base + a["href"], location="Turbo Haüs")
            out.append(priced(ev, *price_from_text(slug_price.get(m.group(4), ""))))
    # prefer entries that carry a time (the list view) over the bare grid cell for the same event
    best = {}
    for e in out:
        k = e.url
        if k not in best or (best[k].all_day and not e.all_day):
            best[k] = e
    return list(best.values())


def wix_events(url: str, source: str, location: str) -> list[Ev]:
    """A Wix Events page: each event's data has "scheduling":{"config":{… "startDate", "endDate"} … "title"."""
    html = get(url).text
    out, seen = [], set()
    pat = re.compile(r'"scheduling":\{"config":\{[^{}]*?"startDate":"([^"]+)","endDate":"([^"]+)".*?'
                     r'"title":"((?:[^"\\]|\\.)*)"', re.S)
    for m in pat.finditer(html):
        title = clean(json.loads(f'"{m.group(3)}"'))
        key = (title, m.group(1))
        if key in seen:
            continue
        seen.add(key)
        out.append(Ev(source, title, iso(m.group(1)), url, iso(m.group(2)), location))
    return out


def le_belmont() -> list[Ev]:
    return wix_events("https://www.lebelmont.ca/evenements", "Le Belmont", "Le Belmont")


def montreal_art_center() -> list[Ev]:
    """Montreal Art Center & Museum (1844 rue William; Wix events): film club, screenings, open mics,
    exhibition openings. Kinds: films → "Screening", exhibits → "Vernissage", the rest → "Event"."""
    out = wix_events("https://www.montrealartcenter.com/our-events", "Montreal Art Center",
                     "Montreal Art Center, 1844 rue William")
    for e in out:
        e.kind = ("Screening" if re.search(r"film club|projection|screening|cin[ée]", e.title, re.I) else
                  "Vernissage" if re.search(r"exhibit|exposition|vernissage", e.title, re.I) else "Event")
        if e.kind == "Screening":
            e.title = re.sub(r"^\s*projection\s*:\s*", "", e.title, flags=re.I)   # 'Projection: Clueless' -> 'Clueless'
        elif e.end and (e.end.date() - e.start.date()).days >= 1:   # a show running over several days
            e.start, e.end, e.kind = e.start.date(), e.end.date(), "exhibition"
    return out


def mbam_series(slug_prefix: str, source: str = "MBAM") -> list[Ev]:
    """Montreal Museum of Fine Arts: each date of a series has its own page (/en/activities/la-buvette-03-12-26/),
    linked from the museum's calendar (/en/calendar/?page=N, a second or less per page). Date from the page's
    schema.org Event, time and price from its text. (The sitemap used before takes ~50 s to build and no longer
    lists these pages.)"""
    urls, seen_pages = [], set()
    for n in range(1, 13):
        html = get(f"https://www.mbam.qc.ca/en/calendar/?page={n}").text
        found = re.findall(r'href="(?:https://www\.mbam\.qc\.ca)?(/en/activities/[^"#?]+)"', html)
        if not found or frozenset(found) in seen_pages:   # past the last page (some sites repeat the last one)
            break
        seen_pages.add(frozenset(found))
        urls += ["https://www.mbam.qc.ca" + u for u in found if u.startswith(f"/en/activities/{slug_prefix}")]
    urls = list(dict.fromkeys(urls))
    out = []
    for u in urls:
        try:
            s = soup(u)
        except requests.RequestException:
            continue
        ld = None
        for sc in s.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(sc.string or "", strict=False)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("@type") == "Event" and d.get("startDate"):
                ld = d
                break
        if not ld:
            continue
        text = clean(s.get_text(" "))
        t = None
        m = re.search(r"\d{4}\s+at\s+(\d{1,2}:\d{2}\s*[ap]m)", text, re.I)
        if m:
            t = parse_time(m.group(1))
        ev = Ev(source, clean(ld.get("name") or ""), local(date.fromisoformat(ld["startDate"][:10]), t), u,
                location="Montreal Museum of Fine Arts, 1380 rue Sherbrooke O", kind="Event")
        length = re.search(r"Length\s+(\d+)h(\d{2})?", text)
        if length and t:
            ev.end = ev.start + timedelta(hours=int(length.group(1)), minutes=int(length.group(2) or 0))
        price = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text)
        out.append(priced(ev, None, float(price.group(1))) if price else ev)
    return out


# ------------------------------------------------------ classical & opera
MONTH_RX = r"(January|February|March|April|May|June|July|August|September|October|November|December)"


def clock(s: str) -> time | None:
    """'7:30 p.m.', '07:30 pm', '19h30', '14:00' -> time."""
    m = re.search(r"(\d{1,2})(?::|\s?h\s?)(\d{2})?\s*([ap])?\.?\s*m?\b", s or "", re.I)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) and m.group(3).lower() == "p" and h < 12:
        h += 12
    if m.group(3) and m.group(3).lower() == "a" and h == 12:
        h = 0
    return time(h, mi) if h < 24 and mi < 60 else None


def salle_bourgie() -> list[Ev]:
    """Bourgie Hall (the Museum of Fine Arts' concert hall): its concert list (/en/bourgie-hall/?page=N) links each
    concert's page; the page has the date(s) and time ('Thursday December 10, 2026 at 07:30 pm'), the prices
    ('General public: $75') and a schema.org description. Talks are left out."""
    from concurrent.futures import ThreadPoolExecutor
    cards, seen = {}, set()
    for n in range(1, 9):
        s = soup(f"https://www.mbam.qc.ca/en/bourgie-hall/?page={n}")
        found = 0
        for a in s.find_all("a", href=re.compile(r"^/en/activities/")):
            u = "https://www.mbam.qc.ca" + a["href"].split("?")[0]
            if u in seen:
                continue
            seen.add(u)
            found += 1
            series = a.find(string=re.compile(r"^\s*●"))
            cards[u] = clean(series).lstrip("● ") if series else ""
        if not found:
            break

    def read(item):
        u, series = item
        if re.search(r"conference|talk|lecture|causerie", series, re.I):
            return []
        try:
            s = soup(u)
        except requests.RequestException:
            return []
        ld = {}
        for sc in s.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(sc.string or "", strict=False)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("@type") == "Event":
                ld = d
        for t in s(["script", "style"]):
            t.decompose()
        text = clean(s.get_text(" "))
        title = clean(ld.get("name") or (s.find("h1").get_text(" ") if s.find("h1") else ""))
        if not title:
            return []
        when = dict.fromkeys(re.findall(MONTH_RX + r"\s+(\d{1,2}),\s+(\d{4})\s+at\s+(\d{1,2}:\d{2}\s*[ap]m)", text, re.I))
        public = re.search(r"General public:\s*\$\s?(\d+(?:\.\d{2})?)", text)
        amounts = [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d{2})?)", text[:3000])]
        out = []
        for mo, dy, yr, hm in when:
            ev = Ev("Salle Bourgie", title, local(date(int(yr), EN_MONTHS[mo.lower()], int(dy)), clock(hm)), u,
                    location="Bourgie Hall, 1339 rue Sherbrooke O")
            ev.blurb = series
            ev.desc = clean(ld.get("description") or "")
            ev.image = ld.get("image") or ""
            if public:
                priced(ev, None, float(public.group(1)))
            elif amounts:
                priced(ev, None, min(amounts), max(amounts))
            elif re.search(r"\bfree\b", text[:3000], re.I):
                priced(ev, True)
            out.append(ev)
        return out

    with ThreadPoolExecutor(6) as pool:
        return [e for evs in pool.map(read, cards.items()) for e in evs]


MCGILL_SKIP = re.compile(r"colloqui|lecture|master ?class|workshop|audition|conference|symposium|seminar|info session|"
                         r"open house|\btalk\b|panel|webinar|deadline|convocation|orientation|meeting|lunch & learn", re.I)


def mcgill_schulich() -> list[Ev]:
    """McGill's Schulich School of Music: its calendar page holds the whole season in one page (title, start and end
    of each event). Talks, colloquia, masterclasses and auditions are left out. Student recitals and class
    concerts are free. (Only this one page is read: the site's other listings sit behind a bot check.)"""
    s = soup("https://www.mcgill.ca/music/events/calendar")
    out, seen = [], set()
    for a in s.find_all("a", href=re.compile(r"/music/channels/event/"), attrs={"start": True}):
        title = clean(a.get("title") or "")
        if not title or MCGILL_SKIP.search(title):
            continue
        try:
            start = datetime.strptime(a["start"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            end = datetime.strptime(a["end"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ) if a.get("end") else None
        except ValueError:
            continue
        url = "https://www.mcgill.ca" + a["href"]
        if (url, start) in seen:
            continue
        seen.add((url, start))
        ev = Ev("McGill Schulich", title, start, url, end if end and end > start else None,
                "Schulich School of Music, McGill (555 rue Sherbrooke O)")
        if re.search(r"recital|class concert|studio class|ensembles?$", title, re.I):
            priced(ev, True)
        out.append(ev)
    return out


def opera_de_montreal() -> list[Ev]:
    """Opéra de Montréal: the season page lists each production; its page has every performance
    ('May 1, 2027 - 19h30'), the hall ('Venue: …') and the lowest price ('Starting at $39').
    School shows and postponed productions are left out."""
    base = "https://operademontreal.com"
    s = soup(base + "/en/programs")
    prods = {}
    for card in s.select("article.events__card"):
        a = card.find("a", href=re.compile(r"/en/programs/"))
        if not a or "spectacles-scolaires" in " ".join(card.get("class", [])):
            continue
        if re.search(r"postponed|report[ée]", card.get_text(" "), re.I):
            continue
        style = card.select_one(".events__card-style")
        place = card.select_one(".events__card-place")
        prods[a["href"].strip()] = (clean(style.get_text(" ")) if style else "", clean(place.get_text(" ")) if place else "")
    out = []
    for u, (style, card_place) in prods.items():
        try:
            p = soup(u)
        except requests.RequestException:
            continue
        og = p.find("meta", property="og:image")
        h1 = p.find("h1")
        for t in p(["script", "style", "nav", "header", "footer"]):
            t.decompose()
        text = clean(p.get_text(" "))
        title = clean(h1.get_text(" ")) if h1 else u.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        venue = re.search(r"Venue:\s*(.+?)\s+(?:Language|Audio|Season|Starting|Opera style|$)", text)
        price = re.search(r"Starting at \$\s?(\d+)", text)
        about = next((clean(x.get_text(" ")) for x in p.find_all("p") if len(clean(x.get_text(" "))) > 120), "")
        for mo, dy, yr, hh, mm in dict.fromkeys(re.findall(MONTH_RX + r"\s+(\d{1,2}),\s+(\d{4})\s*-\s*(\d{1,2})h(\d{2})", text)):
            ev = Ev("Opéra de Montréal", title, local(date(int(yr), EN_MONTHS[mo.lower()], int(dy)), time(int(hh), int(mm))),
                    u, location=clean(venue.group(1)) if venue else card_place)
            ev.blurb = f"opera {style}"
            ev.desc = about
            ev.image = og["content"] if og and og.get("content") else ""
            out.append(priced(ev, None, float(price.group(1))) if price else ev)
    return out


PDA_CACHE = OUT / "pda_cache.json"   # Place des Arts event page -> what it says; each page is read at most once a day
PDA_AWAY = re.compile(r"Fernand-Lindsay|Joliette|Mascouche|Lanaudi|Matha|M[ée]lanie|Alphonse|Ambroise|"
                      r"Jean-Pierre Ferland|Ar[ôo]mes et saveurs|Grand D[ée]rangement|CRAPO", re.I)
PDA_KEEP = re.compile(r"classical music|op[ée]ra|lyrique", re.I)   # not "Classical Ballet"


def place_des_arts() -> list[Ev]:
    """Place des Arts (OSM, Orchestre Métropolitain, Opéra, chamber festivals…): every event page is in the site's
    sitemap; each page carries its discipline ('Classical Music', 'Romantic Opera'…), hall and producer in a small
    data block, and lists its performances with time and price. Only classical music and opera are kept.
    Pages are read at most once a day (remembered in docs/pda_cache.json)."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        cache = json.loads(PDA_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    today = datetime.now(TZ).date()
    recent = (today - timedelta(days=150)).isoformat()
    index = get("https://www.placedesarts.com/sitemap.xml").text
    pages = []
    for sm in re.findall(r"<loc>([^<]*section-event[^<]*)</loc>", index):
        xml = get(sm).text
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
            en = re.search(r'hreflang="en-ca" href="([^"]+)"', block)
            mod = re.search(r"<lastmod>([^<]+)", block)
            if en and mod and mod.group(1)[:10] >= recent:
                pages.append(en.group(1))
    pages = list(dict.fromkeys(pages))

    def read(u):
        try:
            s = BeautifulSoup(get(u).content, "html.parser")
        except requests.RequestException:
            return u, None
        gtm = {}
        tag = s.find("script", id="eventGTM")
        if tag:
            try:
                gtm = json.loads(tag.string or "{}")
            except ValueError:
                pass
        info = {"day": today.isoformat(), "discipline": " • ".join(gtm.get("eventDiscipline") or []),
                "sub": " • ".join(gtm.get("eventSubCategory") or [])}
        if not PDA_KEEP.search(info["discipline"] + " " + info["sub"]):
            return u, info
        og_img = s.find("meta", property="og:image")
        og_desc = s.find("meta", property="og:description")
        perfs = []
        for p in s.select("div.sb-performance"):
            d = p.select_one(".sb-performance__date")
            h = p.select_one(".hour")
            pr = p.select_one(".sb-performance__price")
            perfs.append([clean(d.get_text(" ")) if d else "", clean(h.get_text(" ")) if h else "",
                          clean(pr.get_text(" ")) if pr else "", bool(p.find(attrs={"data-tag": "sold_out"}))])
        info.update(title=clean(gtm.get("eventName") or ""), place=", ".join(gtm.get("eventPlace") or []),
                    producer=clean(gtm.get("eventProducer") or ""), perfs=perfs,
                    image=og_img["content"] if og_img and og_img.get("content") else "",
                    desc=clean(og_desc["content"]) if og_desc and og_desc.get("content") else "")
        return u, info

    todo = [u for u in pages if (cache.get(u) or {}).get("day") != today.isoformat()]
    with ThreadPoolExecutor(8) as pool:
        for u, info in pool.map(read, todo):
            if info is not None:
                cache[u] = info
    PDA_CACHE.write_text(json.dumps({u: cache[u] for u in pages if u in cache}, ensure_ascii=False, indent=0),
                         encoding="utf-8")
    out = []
    for u in pages:
        info = cache.get(u) or {}
        if not info.get("perfs") or not info.get("title"):
            continue
        place = info.get("place") or ""
        if PDA_AWAY.search(place):   # Festival de Lanaudière and other concerts outside Montreal
            continue
        if place and "place des arts" not in place.lower() and place in (
                "Maison symphonique", "Salle Wilfrid-Pelletier", "Théâtre Maisonneuve", "Cinquième Salle",
                "Salle Claude-Léveillée", "Théâtre Jean-Duceppe", "Piano Nobile"):
            place += ", Place des Arts"
        for day, hour, price, sold_out in info["perfs"]:
            m = re.search(MONTH_RX + r"\s+(\d{1,2}),\s+(\d{4})", day)
            if not m:
                continue
            d = date(int(m.group(3)), EN_MONTHS[m.group(1).lower()], int(m.group(2)))
            ev = Ev("Place des Arts", info["title"], local(d, clock(hour)), u, location=place.replace("Salle Bourgie", "Bourgie Hall"))
            ev.blurb = f"{info.get('discipline', '')} • {info.get('sub', '')} • {info.get('producer', '')}"
            ev.desc = info.get("desc") or ""
            ev.image = info.get("image") or ""
            out.append(priced(ev, *price_from_text(price)) if price else ev)
    return out


CLASSICAL_PRIORITY = ["Opéra de Montréal", "Salle Bourgie", "McGill Schulich", "Place des Arts"]


def dedupe_concerts(events: list[Ev]) -> list[Ev]:
    """The same performance from two sources (an Opéra production also sold by Place des Arts, a festival concert
    at Bourgie Hall): same day, starting within 90 minutes, sharing a word of the title. The organizer's own
    listing is kept (CLASSICAL_PRIORITY order)."""
    rank = {s: i for i, s in enumerate(CLASSICAL_PRIORITY)}
    kept: list[Ev] = []
    for e in sorted(events, key=lambda e: rank.get(e.source, 99)):
        words = {w for w in plain_text(e.title).split() if len(w) > 3}
        if isinstance(e.start, datetime) and any(
                o.source != e.source and isinstance(o.start, datetime) and abs(o.start - e.start) <= timedelta(minutes=90)
                and words & set(plain_text(o.title).split()) for o in kept):
            continue
        kept.append(e)
    return kept


# Kinds of classical concert (the "Type" row in Classical & Opera), from the title and the source's own category
CLASSICAL_TYPES = [
    ("Opera", r"op[ée]ra|operetta|op[ée]rette|\bbuffa\b|zarzuela|lyrique"),
    ("Orchestral", r"orchestr|symphon|philharm|sinfonia|concerto|\bosm\b|\bnagano\b"),
    ("Chamber Music", r"chamber|de chambre|quartet|quatuor|\btrio\b|quintet|sonata|sonate|\bduo\b|strings"),
    ("Choral & Vocal", r"choir|choral|ch(?:oe|œ)ur|chorale|\bvoice\b|\bvoix\b|lieder|\blied\b|song interpretation|"
                       r"soprano|mezzo|tenor|ténor|baritone|baryton|countertenor|messiah|oratorio|cantat|spirituals|vocal"),
    ("Solo & Recitals", r"\b(?:violin|viola|cello|bassoon|flute|clarinet|oboe|horn|trumpet|trombone|tuba|harp|saxophone)\b.*"
                        r"\bclass concert|\bpiano|\borgan\b|\borgue\b|guitar|guitare|harpsichord|clavecin|claviers|r[ée]cital|recital"),
    ("Early Music", r"baroque|early music|musique ancienne|\bviols?\b|cappella antica|renaissance|\bbach\b|handel|"
                    r"haendel|vivaldi|seicento|violons du roy|monteverdi|purcell"),
    ("Contemporary", r"contemporary|contemporain|new music|musique nouvelle|d'aujourd'hui|electroacoust|cirmmt|"
                     r"composition|premi[eè]re|cr[ée]ation"),
    ("Film in Concert", r"film soundtrack|animation|live[- ]to[- ]film|cin[ée][- ]?concert"),
    ("Jazz", r"jazz|\bswing\b|big band|buena vista"),
    ("Family", r"young audiences|famil|jeune public|\bkids\b|enfants|peter and the wolf"),
    ("Holiday Season", r"no[eë]l|christmas|holiday|f[êe]tes|messiah|nutcracker"),
]
_CLASSICAL_RX = [(g, re.compile(p, re.I)) for g, p in CLASSICAL_TYPES]


def classical_types(e: Ev) -> list[str]:
    text = f"{e.title} {e.blurb}"
    return [g for g, rx in _CLASSICAL_RX if rx.search(text)]


def mccord_after_hours() -> list[Ev]:
    """McCord Stewart Museum 'After Hours': the page's 'Next event' spot says 'Stay tuned!' until a date is set;
    when it shows an English date ('Thursday, January 22, 2027 … 6 pm'), that becomes the event."""
    url = "https://www.musee-mccord-stewart.ca/en/young-mccord/mccord-after-hours/"
    text = clean(soup(url).get_text(" "))
    i = text.find("Next event")
    part = text[i:i + 400] if i >= 0 else ""
    part = part.split("A few photos")[0]
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
                  r"(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?", part)
    if not m:
        return []
    mo, dy = EN_MONTHS[m.group(1).lower()], int(m.group(2))
    d = date(int(m.group(3)) if m.group(3) else guess_year(mo, dy), mo, dy)
    return [Ev("McCord Museum", "After Hours at the McCord", local(d, parse_time(part[m.end():m.end() + 80])), url,
               location="McCord Stewart Museum, 690 rue Sherbrooke O", kind="Event")]


def _luma_ev(e: dict, source: str, ticket_info: dict | None = None) -> Ev:
    geo = e.get("geo_address_info") or {}
    loc = geo.get("full_address") or geo.get("address") or geo.get("city") or ""
    ev = Ev(source, e["name"], iso(e["start_at"]), "https://luma.com/" + e["url"],
            iso(e["end_at"]) if e.get("end_at") else None, loc)
    ev.image = e.get("cover_url") or ""
    ti = ticket_info or {}
    if ti.get("is_free"):
        return priced(ev, True)
    lo = (ti.get("price") or {}).get("cents")
    hi = (ti.get("max_price") or {}).get("cents") if isinstance(ti.get("max_price"), dict) else None
    return priced(ev, None, lo / 100 if lo is not None else None, hi / 100 if hi is not None else None)


def luma_calendar(page_url: str, source: str) -> list[Ev]:
    """A Luma calendar page (luma.com/<calendar>): upcoming events from its embedded page data."""
    s = soup(page_url)
    data = json.loads(s.find("script", id="__NEXT_DATA__").string)
    found, tickets = {}, {}

    def walk(o):
        if isinstance(o, dict):
            if "start_at" in o and "name" in o and "url" in o and "api_id" in o:
                found[o["api_id"]] = o
            if isinstance(o.get("event"), dict) and isinstance(o.get("ticket_info"), dict):
                tickets[o["event"].get("api_id")] = o["ticket_info"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return [_luma_ev(e, source, tickets.get(k)) for k, e in found.items()]


def luma_user(user_api_id: str, source: str) -> list[Ev]:
    """Upcoming events hosted by a Luma user profile (luma.com/user/<name>)."""
    out, cursor = [], None
    while True:
        params = {"user_api_id": user_api_id, "period": "future", "pagination_limit": 50}
        if cursor:
            params["pagination_cursor"] = cursor
        d = get("https://api.lu.ma/user/profile/events-hosting", params=params).json()
        out += [_luma_ev(x["event"], source, x.get("ticket_info")) for x in d.get("entries", [])]
        if not d.get("has_more") or not d.get("next_cursor"):
            return out
        cursor = d["next_cursor"]


def laugh_it_off() -> list[Ev]:
    d = get("https://www.laughitoffmtl.ca/api/events").json()
    horizon = datetime.now(TZ) + timedelta(days=60)   # weekly shows are listed a year out; keep 2 months
    out = []
    for e in d.get("events", []):
        st = iso(e["startUtc"]) if e.get("startUtc") else iso(e["start"])
        if st > horizon:
            continue
        en = iso(e["endUtc"]) if e.get("endUtc") else None
        ev = Ev("Laugh It Off", e["name"], st, e["url"], en, e.get("venue") or "")
        ev.image = e.get("image") or ""
        out.append(priced(ev, True if e.get("isFree") else None, e.get("priceMin"), e.get("priceMax")))
    return out


def newspeak() -> list[Ev]:
    """Newspeak's site links each show to Universe; Universe's listing API has the dates."""
    html = get("https://www.newspeakmtl.com/").text
    slugs = dict.fromkeys(re.findall(r"universe\.com/events/([a-z0-9-]+-[A-Z0-9]{6})", html))
    out = []
    for slug in slugs:
        try:
            d = get(f"https://www.universe.com/api/v2/listings/{slug}").json()
        except requests.RequestException:
            continue
        lst = d.get("listing") or {}
        for e in d.get("events", []):
            if e.get("state") not in (None, "normal"):
                continue
            ev = Ev("Newspeak", lst.get("title") or slug, iso(e["start_time"]),
                    f"https://www.universe.com/events/{slug}",
                    iso(e["end_time"]) if e.get("end_time") else None, lst.get("venue_name") or "Newspeak")
            ev.image = lst.get("image_url_500") or lst.get("image_url") or ""
            ev.desc = universe_about(lst)
            out.append(priced(ev, *universe_rates(d)))
    return out


UNIVERSE_BOILERPLATE = re.compile(r"^(portes|doors|spectacle|show|d[ée]but|start)\s*:|^\d+\+$|rembours|refund|presale|pr[ée]vente|"
                                  r"sign ?up|laylo|^billets|^tickets|pr[ée]sente(nt)?$|presents?$|^ages?\b", re.I)


def universe_about(lst: dict) -> str:
    """A Universe listing's description without the door times, age limit, refund policy and presale links
    (often that's all there is: then nothing)."""
    title = plain_text(lst.get("title") or "")
    lines = [clean(x) for x in (lst.get("description") or "").splitlines()]
    keep = [x for x in lines if x and not UNIVERSE_BOILERPLATE.search(x) and plain_text(x) != title]
    text = "\n".join(keep)
    return text if len(text) > 60 else ""


def universe_rates(d: dict) -> tuple[bool | None, float | None, float | None]:
    """(free, lowest, highest) from a Universe listing response. Uses every ticket type ("rates"):
    the listing's own "price" can be the VIP price."""
    lst = d.get("listing") or {}
    if lst.get("free"):
        return True, None, None
    prices = [float(r["price"]) for r in d.get("rates") or [] if isinstance(r.get("price"), (int, float))]
    if prices:
        return None, min(prices), max(prices)
    return None, lst.get("price"), None


def universe_price(slug: str) -> tuple[bool | None, float | None, float | None]:
    """(free, lowest, highest) for a Universe event (universe.com/events/<slug>)."""
    try:
        return universe_rates(get(f"https://www.universe.com/api/v2/listings/{slug}").json())
    except (requests.RequestException, ValueError):
        return None, None, None


def squarespace_shows(base: str, source: str, location: str) -> list[Ev]:
    """Squarespace events page (Bar Le Ritz PDB, Théâtre Fairmount: same group, same layout). The item title
    is just the date; the show's name is the first heading in its description. Prices come from the
    Universe ticket link when there is one."""
    d = get(f"{base}/", params={"format": "json"}).json()
    out = []
    for it in d.get("upcoming", []):
        body = BeautifulSoup(it.get("body") or "", "html.parser")
        head = body.find(["h1", "h2", "h3", "h4"])
        title = clean(head.get_text(" ")) if head else clean(it.get("title"))
        start = datetime.fromtimestamp(it["startDate"] / 1000, TZ)
        end = datetime.fromtimestamp(it["endDate"] / 1000, TZ) if it.get("endDate") else None
        ev = Ev(source, title, start, base + it["fullUrl"], end, location)
        ev.image = (it["assetUrl"] + "?format=500w") if it.get("assetUrl") else ""   # Squarespace size variant
        if head:
            head.extract()
        ev.blurb = re.sub(r"https?://\S+", "", clean(body.get_text(" ")))[:400]   # "Présenté par …", style notes
        uni = next((m.group(1) for a in body.find_all("a", href=True)
                    if (m := re.search(r"universe\.com/events/([a-z0-9-]+-[A-Z0-9]{6})", a["href"]))), None)
        if uni:
            priced(ev, *universe_price(uni))
        else:
            priced(ev, *price_from_text(" ".join(re.findall(r"\d+\s?\$|\$\s?\d+|gratuit|free", body.get_text(" "), re.I))))
        out.append(ev)
    return out


def quai_des_brumes() -> list[Ev]:
    """Quai des Brumes /calendrier (Modern Events Calendar list). Each entry shows '26 Nov' and '9:00 pm';
    the year and month come from its CSS class (mec-toggle-YYYYMM-…). Its JSON-LD is malformed, so it's not used."""
    s = soup("https://quaidesbrumes.ca/calendrier/")
    out, seen = [], set()
    for art in s.select("article.mec-event-article"):
        ym = next((m for c in art.get("class", []) if (m := re.match(r"mec-toggle-(\d{4})(\d{2})-", c))), None)
        a = art.select_one(".mec-event-title a[href]")
        day = art.select_one(".mec-start-date-label")
        if not (ym and a and day):
            continue
        d = re.search(r"\d{1,2}", day.get_text())
        if not d:
            continue
        t_el = art.select_one(".mec-start-time")
        when = local(date(int(ym.group(1)), int(ym.group(2)), int(d.group())), parse_time(t_el.get_text()) if t_el else None)
        url = re.sub(r"[?&]fbclid=[^&]*", "", a["href"])
        key = (clean(a.get_text()), str(when))
        if key in seen:
            continue
        seen.add(key)
        out.append(Ev("Quai des Brumes", clean(a.get_text()), when, url, location="Quai des Brumes, 4481 Saint-Denis"))
    return out


def jsonld_event(url: str) -> dict | None:
    """The schema.org Event a page describes in its <script type="application/ld+json">, if any."""
    for sc in soup(url).find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else data.get("@graph", [data])):
            if isinstance(d, dict) and d.get("@type") in ("Event", "MusicEvent") and d.get("startDate"):
                return d
    return None


def new_city_gas() -> list[Ev]:
    """New City Gas: event links from the events page, then each event page's schema.org data (times, prices)."""
    s = soup("https://newcitygas.com/en/events/")
    urls = dict.fromkeys(a["href"].split("#")[0] for a in s.select("a[href*='/en/event/']"))
    out = []
    for url in urls:
        try:
            d = jsonld_event(url)
        except requests.RequestException:
            continue
        if not d:
            continue
        # (no prices: every page lists "General Admission 25.00", NYE included, which looks like a template
        #  placeholder; real prices are on Tixr, which blocks automated access)
        ev = Ev("New City Gas", clean(d.get("name") or ""), iso(d["startDate"]), url,
                iso(d["endDate"]) if d.get("endDate") else None, "New City Gas, 950 rue Ottawa")
        ev.image = d.get("image") if isinstance(d.get("image"), str) else ""
        out.append(ev)
    return out


def next_flight_text(html: str) -> str:
    """The data a Next.js app page streams in `self.__next_f.push([1, "..."])` script chunks, joined."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    return "".join(json.loads(f'"{c}"') for c in chunks)


def boiler_room() -> list[Ev]:
    """boilerroom.tv/city/montreal lists every Montreal show (past ones too; old ones drop at the cutoff)."""
    base = "https://boilerroom.tv"
    t = next_flight_text(get(f"{base}/city/montreal/").text)
    out, seen = [], set()
    for m in re.finditer(r'"start":"([^"]+)"', t):
        before = t[max(0, m.start() - 1200):m.start()]
        titles = re.findall(r'"children":"((?:[^"\\]|\\.){3,160})"\}\]', before)
        hrefs = re.findall(r'"href":"(/session/[^"]+)"', before)
        if not titles:
            continue
        url = base + hrefs[-1] if hrefs else f"{base}/city/montreal/"
        if url in seen:
            continue
        seen.add(url)
        title = json.loads(f'"{titles[-1]}"')
        if "boiler room" not in title.lower():
            title = f"Boiler Room {title}"
        out.append(Ev("Boiler Room", title, iso(m.group(1)), url, location="Montréal"))
    if not out and '"start"' not in t:
        raise RuntimeError("no events found on the page (layout changed?)")
    return out


# ------------------------------------------------------------ bar nights
BADSLAVA = "https://badslava.com/"
_badslava_details: dict[str, dict[str, str]] = {}


def _badslava_detail(event_id: str) -> dict[str, str]:
    """details.php fields: 'Event Type', 'Event Frequency', 'Event Cost', ..."""
    if event_id not in _badslava_details:
        info = {}
        try:
            for td in soup(f"{BADSLAVA}details.php?id={event_id}").select("td"):
                b = td.find("b")
                if b and b.get_text().strip().endswith(":"):
                    label = b.get_text().strip().rstrip(":").strip()
                    b.extract()
                    info[label] = clean(td.get_text(" "))
        except requests.RequestException:
            pass
        _badslava_details[event_id] = info
    return _badslava_details[event_id]


def badslava(path: str, source: str, default_type: str, weeks: int = 4) -> list[Ev]:
    """badslava.com weekly listings for Montreal (one week shown; weekly nights are repeated for `weeks` weeks).
    These are bar nights, so events get no link."""
    s = soup(BADSLAVA + path)
    out, day = [], None
    for tr in s.select("table tr"):
        th = tr.find("th")
        if th:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2})", th.get_text())
            day = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
            continue
        tds = tr.find_all("td")
        a = tr.select_one("a[href*='details.php']")
        if not day or len(tds) < 2 or not a:
            continue
        t = parse_time(tds[0].get_text())
        bar = clean(a.find("b").get_text()) if a.find("b") else ""
        address = clean(a.get_text(" ")).replace(bar, "", 1).strip()
        info = _badslava_detail(re.search(r"id=(\d+)", a["href"]).group(1))
        kind = info.get("Event Type") or default_type
        if default_type.split()[0].lower() not in kind.lower():   # comedy page says just "Open Mic"
            kind = f"{default_type.split()[0]} {kind}"
        free, lo, hi = price_from_text(info.get("Event Cost", ""))
        repeat = weeks if info.get("Event Frequency", "").lower() == "weekly" else 1
        for w in range(repeat):
            ev = Ev(source, f"{kind} at {bar}", local(day + timedelta(weeks=w), t), "",
                    location=f"{bar}, {address}" if address else bar)
            out.append(priced(ev, free, lo, hi))
    return out


def tickettailor(org: str, source: str) -> list[Ev]:
    """An organizer's Ticket Tailor box office (tickettailor.com/events/<org>): each listed event's page has a
    schema.org Event with start/end, venue, every ticket price, flyer and the start of the description."""
    box = f"https://www.tickettailor.com/events/{org}"
    links = dict.fromkeys(urljoin_(box, a["href"]) for a in soup(box).find_all("a", href=re.compile(rf"/events/{org}/\d+")))
    out = []
    for url in links:
        page = BeautifulSoup(get(url).content, "html.parser")
        for sc in page.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(sc.string or "")
            except ValueError:
                continue
            for d in data.get("@graph", [data]) if isinstance(data, dict) else data:
                if d.get("@type") != "Event" or "Cancelled" in d.get("eventStatus", ""):
                    continue
                place = d.get("location") or {}
                addr = place.get("address") or {}
                where = ", ".join(x for x in dict.fromkeys(
                    [place.get("name", ""), addr.get("streetAddress", ""), addr.get("postalCode", "")]) if x)
                ev = Ev(source, clean(d["name"]), iso(d["startDate"]), url,
                        iso(d["endDate"]) if d.get("endDate") else None, where)
                ev.image = (d.get("image") or [""])[0] if isinstance(d.get("image"), list) else d.get("image") or ""
                ev.desc = clean(d.get("description", ""))
                if len(ev.desc) >= 140 and not ev.desc.endswith((".", "!", "?")):   # Ticket Tailor cuts it short
                    ev.desc = ev.desc.rsplit(" ", 1)[0] + "…"
                prices = [float(o["price"]) for o in d.get("offers") or [] if o.get("price") is not None]
                out.append(priced(ev, prices == [0], min(prices), max(prices)) if prices else ev)
    return out


def dieseonze() -> list[Ev]:
    """Dièse Onze (jazz club, 4115-A St-Denis): its calendar page lists every show with date anchor, time,
    line-up and cover. The site only answers over plain http."""
    url = "http://www.dieseonze.com/calendrier.php?l=fr"
    s = BeautifulSoup(get(url).content, "html.parser")
    out = []
    for tr in s.select("#table_whole_calendar tr"):
        a = tr.find("a", attrs={"name": re.compile(r"^\d{4}-\d{2}-\d{2}$")})
        tds = tr.find_all("td", recursive=False)
        if not a or len(tds) < 2 or not tds[1].find("b"):
            continue
        title = clean(tds[1].find("b").get_text(" "))
        if re.search(r"ferm[ée]|closed|d[ée]tails? [àa] venir$", title, re.I) or title.upper() == "SPECTACLE JAZZ":
            continue
        m = re.search(r"(\d{1,2})h(\d{2})", tds[0].get_text())
        t = time(int(m.group(1)), int(m.group(2))) if m else None
        body = tds[1].find("span") or tds[1]
        lines = [clean(p.get_text(" ")) for p in body.find_all("p")] or [clean(body.get_text(" "))]
        lines = [x for x in lines if x]
        prices = [float(x) for x in re.findall(r"(\d{1,3})\s*\$", tds[1].get_text(" "))]
        ev = Ev("Dièse Onze", tidy_case(title), local(date.fromisoformat(a["name"]), t),
                f"{url}#{a['name']}", location="Dièse Onze, 4115-A rue St-Denis")
        # the line-up ("Coral Egan - voix · Dan Thouin - piano") and set times, without the booking boilerplate
        ev.desc = "\n".join(x for x in lines if not re.search(r"r[ée]servation|ouverture des portes|\$", x, re.I))
        out.append(priced(ev, None, min(prices), max(prices)) if prices else ev)
    return out


def bistro_a_jojo() -> list[Ev]:
    """Bistro à Jojo (blues bar, 1627 St-Denis), live music 7 nights: its public Google Calendar. The early sets
    (5 pm, solo/duo acoustic) are free with a tip jar, says the bar; evening bands have no price listed."""
    out = google_calendar("bistroajojo.montreal@gmail.com", "Bistro à Jojo", "Bistro à Jojo, 1627 rue St-Denis",
                          days=21)   # two or three sets a night: three weeks is plenty
    for e in out:
        if e.start.hour < 20:
            priced(e, True)
            e.desc = e.desc or "Early acoustic set (blues, rock, folk, chanson). Free, tips welcome."
    return out


def livenation_venue(venue: str, source: str, location: str) -> list[Ev]:
    """A venue's Live Nation page (livenation.com/venue/<id>/<name>-events): a schema.org event per show Live Nation
    or evenko sells there. (Songkick has more, but refuses requests from GitHub's servers.)"""
    s = BeautifulSoup(get(f"https://www.livenation.com/venue/{venue}").content, "html.parser")
    out = []
    for sc in s.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if "Event" not in str(d.get("@type")) or re.search(r"Cancelled|Postponed", d.get("eventStatus", "")):
                continue
            ev = Ev(source, clean(d["name"]), iso(d["startDate"]), d.get("url", ""), location=location)
            img = d.get("image")
            ev.image = img[0] if isinstance(img, list) and img else img if isinstance(img, str) else ""
            desc = clean(d.get("description", ""))
            ev.desc = desc if says_more(desc, ev) else ""   # usually just "Islandman Wednesday, March 10…"
            offers = d.get("offers") or []
            prices = [float(o["price"]) for o in (offers if isinstance(offers, list) else [offers])
                      if str(o.get("price", "")).replace(".", "", 1).isdigit()]
            out.append(priced(ev, None, min(prices), max(prices)) if prices else ev)
    return out


def bootlegger() -> list[Ev]:
    """Bootlegger (cocktail bar, 3481 St-Laurent; WP Event Manager): weekly series ("Groovy Jazz Wednesday",
    "Friday & Saturday DJ Nights", "Sunday Live Jazz…") listed with a date range. Each series page's schema.org
    Event gives the first night (time, end, description, picture); it's repeated weekly to the range's last day,
    on both days for "Friday & Saturday". The Tuesday comedy is Laugh It Off's, already read from its own site."""
    base = "https://barbootlegger.com/event/"
    s = soup(base)
    out = []
    for card in s.select("a.wpem-event-action-url[href]"):
        url = card["href"]
        text = clean(card.get_text(" "))
        if re.search(r"comed|humou?r|stand.?up", text + url, re.I):
            continue
        rng = re.findall(r"(\d{4}-\d{2}-\d{2}) @ (\d{1,2}:\d{2} [AP]M)", text)
        if not rng:
            continue
        last = date.fromisoformat(rng[-1][0])
        page = BeautifulSoup(get(url).content, "html.parser")
        ld = None
        for sc in page.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(sc.string or "")
            except ValueError:
                continue
            for x in d.get("@graph", [d]) if isinstance(d, dict) else d:
                if x.get("@type") == "Event" and x.get("startDate"):
                    ld = x
        if not ld:
            continue
        first, end = iso(ld["startDate"]), iso(ld["endDate"]) if ld.get("endDate") else None
        length = (end - first) if end and end > first else timedelta(hours=3)
        title = clean(ld["name"])
        offsets_title = title
        # "Groovy Jazz Wednesday — Jazz is back at the Bootlegger" -> "Groovy Jazz Wednesday"
        title = re.sub(r"\s+at (?:the )?Bootlegger.*$", "", re.split(r"\s+[—–]\s+", title)[0], flags=re.I)
        offsets = [0, 1] if re.search(r"friday\s*(?:&|and|et)\s*saturday|vendredi\s*(?:&|et)\s*samedi", title, re.I) else [0]
        title = offsets_title if not title else title
        desc = clean(ld.get("description", ""))
        img = ld.get("image")
        img = (img[0] if isinstance(img, list) and img else img.get("url") if isinstance(img, dict) else img) or ""
        day = first
        while day.date() <= last:
            for k in offsets:
                start = day + timedelta(days=k)
                if start.date() <= last + timedelta(days=k):
                    ev = Ev("Bootlegger", title, start, url, start + length,
                            "Bootlegger, 3481 boul. Saint-Laurent")
                    ev.desc, ev.image = desc, img if isinstance(img, str) else ""
                    out.append(priced(ev, True) if FREE_TEXT.search(desc) else ev)
            day += timedelta(weeks=1)
    return out


def tidy_case(title: str) -> str:
    """'CORAL EGAN TRIO' -> 'Coral Egan Trio' (titles typed in capitals); mixed-case titles are left alone."""
    if title != title.upper():
        return title
    small = {"de", "du", "des", "la", "le", "les", "et", "à", "au", "avec", "and", "the", "of", "with", "joue", "invite"}
    words = title.lower().split()
    return " ".join(w if i and w in small else w.upper() if len(w) <= 2 and w.isalpha() else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))


def google_calendar(calendar_id: str, source: str, location: str, days: int = 35,
                    skip: str = r"^(?:[àa] venir|tba|tbd|ferm[ée]|closed)\b") -> list[Ev]:
    """A venue's public Google Calendar (its iCal feed): one-off events plus weekly/daily repeats for the next
    `days` days. Changed or cancelled single dates of a repeat are respected."""
    from urllib.parse import quote
    from icalendar import Calendar as ICal
    from dateutil.rrule import rrulestr
    feed = f"https://calendar.google.com/calendar/ical/{quote(calendar_id)}/public/basic.ics"
    cal = ICal.from_ical(get(feed).content)
    now = datetime.now(TZ)
    until = now + timedelta(days=days)
    overridden = set()   # (uid, original start) of single dates that were moved or edited
    for v in cal.walk("VEVENT"):
        if v.get("recurrence-id"):
            rid = v.decoded("recurrence-id")
            overridden.add((str(v.get("uid")), rid if isinstance(rid, datetime) else rid))
    out = []

    def add(v, start, end):
        title = clean(str(v.get("summary") or "")).strip(" -–")
        if not title or re.search(skip, title, re.I) or str(v.get("status", "")).upper() == "CANCELLED":
            return
        ev = Ev(source, title, start.astimezone(TZ) if isinstance(start, datetime) else start, "",
                end.astimezone(TZ) if isinstance(end, datetime) else None, location)
        ev.desc = clean(str(v.get("description") or ""))
        out.append(ev)

    for v in cal.walk("VEVENT"):
        start = v.decoded("dtstart")
        end = v.decoded("dtend") if v.get("dtend") else None
        if not isinstance(start, datetime):
            continue   # all-day notes ("vacances", "fermé")
        length = (end - start) if end else timedelta(hours=2)
        if v.get("rrule"):
            rule = rrulestr(v.get("rrule").to_ical().decode(), dtstart=start)
            exdates = set()
            for ex in (v.get("exdate") if isinstance(v.get("exdate"), list) else [v.get("exdate")] if v.get("exdate") else []):
                exdates |= {d.dt for d in ex.dts}
            for occ in rule.between(now - length, until, inc=True):
                if occ in exdates or (str(v.get("uid")), occ) in overridden:
                    continue
                add(v, occ, occ + length)
        elif now - length <= start <= until:
            add(v, start, end)
    return out


def urljoin_(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


TNM_DAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
# short names for listings whose name is a whole sentence
TNM_NAMES = {"le vieux/ the old dublin pub & resturant": "The Old Dublin Pub",
             "origine brass / 4 origines - microbrasserie & restaurant": "Origine Brass (4 Origines, Dorval)",
             "4 origines - microbrasserie / pointe-st-charles": "4 Origines (Pointe-St-Charles)",
             "mckibbins irish pub": "McKibbin's (St-Laurent)"}
TNM_SKIP_TOWNS = re.compile(r"\b(Laval|Saint-Hubert|Longueuil|Brossard)\b", re.I)   # off the island


def trivianearme(weeks: int = 4) -> list[Ev]:
    """trivianearme.net/montreal: weekly bar trivia (venue, day, time, often the price). Fills in the bars badslava
    doesn't list; the same bar on the same night from badslava or WEEKLY_BY_HAND wins (see dedupe_bar_nights).
    Nights held every other week are left out: the listing doesn't say which weeks."""
    s = BeautifulSoup(get("https://trivianearme.net/montreal").content, "html.parser")
    today = datetime.now(TZ).date()
    out, seen = [], set()
    for a in s.find_all("a", href=re.compile(r"^/montreal/venues/")):
        h3 = a.find("h3")
        if not h3 or a["href"] in seen:
            continue
        seen.add(a["href"])
        card = a.find_parent("div", class_="bg-surface")
        if not card:
            continue
        name = clean(h3.get_text())
        lines = [clean(x) for x in card.stripped_strings]
        lines = lines[lines.index(name) + 1:] if name in lines else lines
        addr = next((x for x in lines if re.match(r"\d+\s", x)), "")
        if not addr or TNM_SKIP_TOWNS.search(addr):
            continue
        slots = [re.fullmatch(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) at (\d{1,2}:\d{2} [AP]M)", x)
                 for x in lines]
        notes = [x for x in lines if x != addr and len(x) > 25 and not x.startswith("+")]
        desc = max(notes, key=len, default="")   # the venue's note: "Trivia every Monday at 7:30 PM. $5 per person…"
        if re.search(r"every other|bi-?weekly|every (?:second|2nd)|toutes les deux", desc, re.I):
            continue
        name = TNM_NAMES.get(name.lower()) or re.split(r"\s+[-/|]\s+", name)[0]
        street = re.sub(r",?\s*(Montr[ée]al|Pointe-Claire|Dorval)?,?\s*(Quebec|QC)\b.*$", "", addr).strip(" ,")
        town = (re.search(r"(Pointe-Claire|Dorval)", addr) or [None])[0] or "Montreal"
        loc = f"{name}, {street} {town} QC"
        # an entry fee ("$5 per person", "($3 entry)"), not a prize ("$50 bar tab prize"), times or phone numbers
        fee = re.search(r"\$\s?(\d+(?:\.\d\d)?)\s*(?:per\b|/|entry|cover|each|pp\b|par\b)|(?:entry|cover|cost)\D{0,12}\$\s?(\d+)",
                        desc, re.I)
        free, lo = (False, float(fee.group(1) or fee.group(2))) if fee else \
            (True, 0.0) if re.search(r"\bfree\b|\bgratuite?\b", desc, re.I) else (None, None)
        for m in filter(None, slots):
            t = datetime.strptime(m.group(2), "%I:%M %p").time()
            first = today + timedelta(days=(TNM_DAYS[m.group(1).lower()] - today.weekday()) % 7)
            for w in range(weeks):
                ev = Ev("Trivia Nights", f"Trivia Night at {name}", local(first + timedelta(weeks=w), t), "",
                        location=loc)
                ev.desc = desc if not desc.startswith("http") else ""
                out.append(priced(ev, free, lo))
    return out


# Raw room / place names from the sources -> one clean name. Matched on the whole location (case-insensitive);
# add a line when a new spelling shows up.
ROOM_NAMES = {
    "casa del popolo": "Casa del Popolo",
    "sala resto": "La Sala Rossa",            # the Sala Rossa's restaurant room
    "sat – other": "SAT",
    "sat – sat space": "SAT",                 # the main hall; "SAT" alone means the same building
    "sat – satosphere": "SAT – Satosphère",
    "esc, h2x 2t3": "2023 boul. Saint-Laurent",   # LATEX.'s venue; the organizer calls it just "ESC"
    "esc": "2023 boul. Saint-Laurent",
    "2000 av. northcliffe, montréal, qc h4a 3k5, canada": "Soft Studio Days, 2000 av. Northcliffe #216",
    "soft studio days, 2000 av. northcliffe #216, montréal, qc h4a 3k5, canada": "Soft Studio Days, 2000 av. Northcliffe #216",
    "montréal": "", "montreal": "",   # says nothing: the whole site is Montreal
}


def tidy_rooms(events: list[Ev]) -> None:
    for e in events:
        key = clean(e.location).lower()
        if key in ROOM_NAMES:
            e.location = ROOM_NAMES[key]
        else:   # strip a trailing ", Montréal, QC H2X 1Y1, Canada" that some sources add
            e.location = re.sub(r",\s*Montr[ée]al(?:,\s*(?:QC|Qu[ée]bec))?(?:\s+H\d\w\s?\d\w\d)?(?:,\s*Canada)?\s*$", "",
                                clean(e.location))


TWO_READERS = {"L'Escogriffe"}   # venues read from two places (Live Nation + RA)


def merge_repeats(events: list[Ev]) -> list[Ev]:
    """The same show from two readers of one venue (L'Escogriffe on Songkick and RA): same chip, same day,
    starting within 2 hours, sharing a word of the title. The first reader's copy is kept."""
    out = []
    for e in events:
        if e.source in TWO_READERS and isinstance(e.start, datetime) and any(
                o.source == e.source and isinstance(o.start, datetime) and abs(o.start - e.start) <= timedelta(hours=2)
                and set(plain_text(o.title).split()) & {w for w in plain_text(e.title).split() if len(w) > 3}
                for o in out[-60:]):
            continue
        out.append(e)
    return out


def dedupe_bar_nights(events: list[Ev]) -> list[Ev]:
    """The same bar's trivia (or karaoke…) on the same night from two sources: keep the first one listed.
    Bars are matched by street number + a word of the street name ("5752 Av de Monkland" = "5752 Monkland Ave"),
    since bar names are spelled differently from site to site."""
    out, seen = [], []
    for e in events:
        m = re.search(r",\s*(\d+)\s+([^,]+)", e.location)
        if not m:
            out.append(e)
            continue
        words = {w for w in plain_text(m.group(2)).split() if len(w) >= 4 and w not in ("montreal", "rue", "avenue")}
        day = e.start.date() if isinstance(e.start, datetime) else e.start
        if any(s == e.source and n == m.group(1) and d == day and words & ws for s, n, d, ws in seen):
            continue
        seen.append((e.source, m.group(1), day, words))
        out.append(e)
    return out


# Weekly nights known first-hand, with no website to read.
# (filter chip, title, weekday 0=Mon, start time, location, free)
WEEKLY_BY_HAND = [
    ("Trivia Nights", "Trivia Night at Honey Martin", 1, time(21, 30),
     "Honey Martin, 5916 Sherbrooke St W Montreal QC", True),
]


# One-off events known first-hand (organizers that only announce on Instagram / Zeffy / Linktree, which
# can't be read automatically). The tab follows the source name: "Heures ouvrables" lands in Music & Dance.
# Past dates drop off by themselves.
# (source, title, date, start time or None, ticket link or "", location, free (True/False/None), price text)
ONE_OFF_BY_HAND: list[tuple] = [
    # ("Heures ouvrables", "Heures ouvrables 008", date(2027, 3, 5), time(22, 0),
    #  "https://www.zeffy.com/fr-CA/ticketing/…", "Venue name, address", None, "$20"),
]


# Igloofest 2027 (Quai Jacques-Cartier, Old Port), all 12 nights. Line-up and set times from igloofest.ca's
# weekend pages, checked 2026-11-26. General admission from $49 + tax.
# (date, headliners for the title, the night's line-up by stage)
IGLOO = "https://igloofest.ca/en/festival/2027-week-end-{}"
IGLOOFEST_2027 = [
    (date(2027, 1, 14), "KAYTRANADA, Make A Dance", 1,
     "Sapporo stage: KAYTRANADA 10 pm · Lou Phelps 8:30 pm · Taima 7:30 pm\nVidéotron stage (Ferias): Make A Dance 8:45 pm · silktits 7:30 pm"),
    (date(2027, 1, 15), "James Hype, Prïa b2b Zac Martel b2b NAB", 1,
     "Sapporo stage: James Hype 11 pm · Tini Gessler 9:30 pm · The Holy 7:30 pm\nVidéotron stage (AMPER): Prïa b2b Zac Martel b2b NAB 7:30 pm"),
    (date(2027, 1, 16), "Chris Lorenzo, SALOME", 1,
     "Sapporo stage: Chris Lorenzo 10:30 pm · SAPIAN 9 pm · Anna Wilder 7:30 pm\nVidéotron stage: SALOME 9:30 pm · Xia 8:30 pm · No Police 7:30 pm"),
    (date(2027, 1, 21), "Fatboy Slim", 2,
     "Sapporo stage: Fatboy Slim 9:30 pm · Mystery guest 8:30 pm · Lia Plutonic 7:30 pm"),
    (date(2027, 1, 22), "KETTAMA, Chippy Nonstop b2b Nino Brown b2b Young Teesh (sold out)", 2,
     "Sapporo stage: KETTAMA 11 pm · Robbie Doherty 9:30 pm · Elvis Guetta 8:30 pm · syd woz 7:30 pm\n"
     "Vidéotron stage (Pep Rally): Chippy Nonstop b2b Nino Brown b2b Young Teesh 7:30 pm"),
    (date(2027, 1, 23), "Ninajirachi, Mowie b2b the bald girl", 2,
     "Sapporo stage: Ninajirachi 11 pm · Frost Children (DJ set) 10 pm · MGNA CRRRTA (DJ set) 9 pm · Isla Den (DJ set) 7:30 pm\n"
     "Vidéotron stage: Mowie b2b the bald girl 10 pm · swedm® (Varg²™ + jamesjamesjames) 8:30 pm · Beamskii 7:30 pm"),
    (date(2027, 1, 28), "Mosimann", 3,
     "Sapporo stage: Mosimann 9:30 pm · Tony Romera 8:30 pm · V!A 7:30 pm"),
    (date(2027, 1, 29), "Seven Lions, Chris Luno", 3,
     "Sapporo stage: Seven Lions 11:15 pm · Trivecta 9:45 pm · MUST DIE! 7:30 pm · PØPTRT 7:30 pm\n"
     "Vidéotron stage: Chris Luno 9:30 pm · Moti (CR) 8:30 pm · DUZA 7:30 pm"),
    (date(2027, 1, 30), "deadmau5, Rodriguez Jr. (DJ set)", 3,
     "Sapporo stage: deadmau5 10:30 pm · TDJ 9 pm · Claireyy 7:30 pm\nVidéotron stage (Sun & Earth): Rodriguez Jr. (DJ set) 9 pm · Hicky & Kalo 7:30 pm"),
    (date(2027, 2, 4), "Fcukers (DJs), Marie Davidson (DJ set)", 4,
     "Sapporo stage: Fcukers (DJs) 9:30 pm · Marie Davidson (DJ set) 8 pm · Field Note 7:30 pm"),
    (date(2027, 2, 5), "Boris Brejcha, tINI", 4,
     "Sapporo stage: Boris Brejcha 11 pm · DREYA V 9:30 pm · Kolophane 7:30 pm\nVidéotron stage (KNTC): tINI 9 pm · Dimitry b2b Lara Bee 7:30 pm"),
    (date(2027, 2, 6), "Subtronics, Miss Bashful", 4,
     "Sapporo stage: Subtronics 11:15 pm · Taiki Nulight 9:30 pm · NAË 7:30 pm\nVidéotron stage (Hauterageous): Miss Bashful 10:15 pm · Siren Mars 8:45 pm · HŸDRA 7:30 pm"),
]


# Oyster deals (Oysters tab). Only deals confirmed by the place itself or a 2026 source; checked 2026-11-26.
# Deals change with the season and supply, so each listing says where it was confirmed.
# (place, address, weekdays 0=Mon, start, end or None for "to closing", price per oyster or None,
#  price text, conditions, where it was confirmed, link)
OYSTER_DEALS = [
    ("Bar Dominion", "1243 rue Metcalfe", [6, 0], time(16, 0), None, 1.0, "$1 / oyster",
     "All evening, Sundays and Mondays.", "Restomontreal, Jan 2026",
     "https://www.restomontreal.ca/article/bar-dominion-makes-sundays-and-mondays-delicious/2496/en/"),
    ("La Part des Anges", "5425 rue Chapleau", [2, 3, 4, 5, 6], time(17, 0), None, 1.0, "$1 / oyster",
     "Every day it's open (Wed–Sun), 5 pm to closing, while supplies last. Up to 12 per person at $1, then regular "
     "price; $15 minimum spend per person.", "the restaurant's own site",
     "https://www.lapartdesanges.ca/huitres-1-montreal-rosemont"),
    ("Joséphine", "4007 rue Saint-Denis", [2], time(17, 0), None, 1.25, "$1.25 / oyster",
     "Wednesdays. Not combinable with other offers.", "the restaurant's promotions page (Restomontreal)",
     "https://www.restomontreal.ca/resto/josephine-montreal/12440/en/promos/"),
    ("Joséphine", "4007 rue Saint-Denis", [1, 3, 4, 5], time(17, 0), time(19, 0), None, "half price",
     "Happy hour (5 à 7), Tuesday to Saturday: oysters at half price. (On Wednesdays the $1.25 deal applies "
     "instead.)", "the restaurant's promotions page (Restomontreal)",
     "https://www.restomontreal.ca/resto/josephine-montreal/12440/en/promos/"),
    ("Chez Victoire", "1453 avenue du Mont-Royal Est", [2], time(17, 0), None, 1.0, "$1 / oyster",
     "Wednesdays.", "Tastet, Oct 2026", "https://tastet.ca/en/lists/where-to-eat-great-oysters-in-montreal/"),
    ("Bootlegger", "3481 boul. Saint-Laurent", [6], time(17, 0), None, 1.0, "$1 / oyster",
     "Sundays (jazz night), while supplies last; the bar says to come early and reserve. Depends on arrivals.",
     "the bar's own site (Sept 2026) and Restomontreal (Aug 2026)",
     "https://barbootlegger.com/one-dollar-oysters-montreal/"),
    ("Le Majestique", "4105 boul. Saint-Laurent", [0, 1, 2, 3, 4, 5, 6], time(17, 0), time(19, 0), 2.5,
     "$2.50 / oyster", "Happy hour, until 7 pm (the source doesn't say which days; call ahead).",
     "Tastet, Oct 2026", "https://tastet.ca/en/lists/where-to-eat-great-oysters-in-montreal/"),
]
OYSTER_PLACES = {d[0] for d in OYSTER_DEALS}


def oyster_deals(weeks: int = 3) -> list[Ev]:
    today = datetime.now(TZ).date()
    out = []
    for place, addr, days, start, end, each, label, terms, where, url in OYSTER_DEALS:
        for w in range(weeks):
            for wd in days:
                d = today + timedelta(days=(wd - today.weekday()) % 7 + 7 * w)
                s = local(d, start)
                title = f"{'Half-price' if each is None else label.split(' /')[0]} oysters at {place}"
                ev = Ev(place, title, s, url, local(d, end) if end else None, f"{place}, {addr}", kind="Oyster deal")
                ev.desc = f"{terms}\n\nConfirmed by {where}; checked Nov 26, 2026. Deals change with the season, so call ahead."
                ev.genres = ["Food & Drink"]
                out.append(priced(ev, False, each, text=label))
    return out


def by_hand(weeks: int = 4) -> list[Ev]:
    today = datetime.now(TZ).date()
    out = []
    for d, heads, we, lineup in IGLOOFEST_2027:
        ev = Ev("Igloofest", f"Igloofest: {heads}", local(d, time(19, 30)), IGLOO.format(we),
                location="Quai Jacques-Cartier, Old Port")
        ev.desc = lineup
        out.append(priced(ev, False, 49.0, text="from $49 + tax"))
    for source, title, weekday, t, loc, free in WEEKLY_BY_HAND:
        first = today + timedelta(days=(weekday - today.weekday()) % 7)
        for w in range(weeks):
            out.append(priced(Ev(source, title, local(first + timedelta(weeks=w), t), "", location=loc), free))
    for source, title, d, t, url, loc, free, price in ONE_OFF_BY_HAND:
        ev = Ev(source, title, local(d, t), url, location=loc)
        out.append(priced(ev, True) if free else priced(ev, *price_from_text(price or "")))
    for source, title, start, end, url, loc, free in SEASONAL_BY_HAND:
        out.append(priced(Ev(source, title, start, url, end, loc, "exhibition"), free))
    return out


# Events running for days or weeks (holiday markets…), shown under "Ongoing" in their tab. Dates checked
# on mtl.org (Tourisme Montréal) on 2026-11-25. (source, title, first day, last day, link, location, free)
SEASONAL_BY_HAND: list[tuple] = [
    ("Holiday markets", "Le Grand Marché de Noël de Montréal", date(2026, 11, 13), date(2027, 1, 3),
     "https://www.mtl.org/fr/quoi-faire/festivals-et-evenements/le-grand-marche-de-noel",
     "Place des Festivals, Quartier des spectacles", True),
    ("Holiday markets", "Le Village de Noël de Montréal", date(2026, 11, 26), date(2026, 12, 24),
     "https://www.mtl.org/fr/quoi-faire/festivals-et-evenements/le-village-de-noel-de-montreal",
     "Place du Marché Atwater, 3325 rue Saint-Ambroise", True),
]


# ------------------------------------------------------------------ museums
EN_ABBR = {m[:3]: i for m, i in EN_MONTHS.items()}


def _en_date(s: str) -> date:
    m = re.match(r"([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})", s.strip())
    return date(int(m.group(3)), EN_ABBR[m.group(1).lower()], int(m.group(2)))


MUSEUM_FREE = re.compile(r"free of charge|\bit[’']s free\b|\bis free\b|\bfree (?:bilingual|admission|entry|event|"
                         r"activity|presentation|meeting|guided|tour|workshop)|\bgratuit", re.I)


def museum_price(url: str) -> tuple[bool | None, float | None, float | None]:
    """An event page's 'Prices' list ('Adult : $15.00', 'Children : Free'), or wording like 'free of charge'.
    Free only when every listed price is free."""
    main = soup(url).find("main")
    text = clean(main.get_text(" ")) if main else ""
    text = text.split(" Address ")[0]
    sec = re.search(r"\bPrices\b(.*?)(?:\bDates\b|$)", text)
    if sec:
        vals = re.findall(r":\s*(Free|\$\s?[\d.,]+)", sec.group(1), re.I)
        nums = [0.0 if v.lower() == "free" else float(v.strip("$ ").replace(",", "")) for v in vals]
        if nums:
            paid = [n for n in nums if n > 0]
            return (True, None, None) if not paid else (False, min(paid), max(paid))
    if MUSEUM_FREE.search(text):
        return True, None, None
    return None, None, None


def musees_montreal() -> list[Ev]:
    """museesmontreal.org listing: dated events (one day) and temporary/upcoming exhibitions (date range).
    Permanent exhibitions have no dates and are skipped."""
    api = "https://museesmontreal.org/en/api/public/exhibitions-and-events-list"
    out, page, last = [], 1, 1
    while page <= last:
        d = get(api, params={"page": page, "search": "", "filters": "{}"}).json()
        last = int(d.get("last_page") or 1)
        for x in d["data"]:
            fd = x.get("formatted_date") or ""
            if m := re.match(r"from (.+?) to (.+)$", fd):
                out.append(Ev("Musées Montréal", x["name"], _en_date(m.group(1)), x["url"],
                              _en_date(m.group(2)), x.get("museum_name") or "", "exhibition",
                              image=x.get("image_url") or ""))
            elif m := re.match(r"starting on (.+)$", fd):
                ev = Ev("Musées Montréal", x["name"], _en_date(m.group(1)), x["url"],
                        location=x.get("museum_name") or "", kind=x.get("type_text") or "Event",
                        image=x.get("image_url") or "")
                try:
                    priced(ev, *museum_price(x["url"]))
                except requests.RequestException:
                    pass
                out.append(ev)
        page += 1
    return out


# ---------------------------------------------------------------- galleries
_FR_DATE = re.compile(r"(\d{1,2})\s*(?:er)?\s+([a-zéû]+)\.?(?:,?\s+(\d{4}))?", re.I)


def fr_date(text: str, default_year: int | None = None) -> date | None:
    """'10 décembre 2026', '1 er avril 2026', '26 novembre' (year guessed) -> date."""
    m = _FR_DATE.search(text or "")
    if not m or m.group(2).lower().rstrip(".") not in FR_MONTHS:
        return None
    mo, dy = FR_MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1))
    y = int(m.group(3)) if m.group(3) else (default_year or guess_year(mo, dy))
    try:
        return date(y, mo, dy)
    except ValueError:
        return None


def dare_dare() -> list[Ev]:
    """DARE-DARE (artist-run centre): program entries with a start date (<time datetime>) and 'start — end' text.
    Calls for projects and the year-long season umbrella are skipped."""
    base = "https://www.dare-dare.org"
    s = soup(base + "/fr/")
    out = []
    for li in s.select("li"):
        h = li.find("h4")
        t = li.find("time", attrs={"datetime": True})
        a = li.select_one("a.more[href]")
        if not (h and t and a):
            continue
        title = clean(h.get_text())
        if re.match(r"appel|assembl[ée]e g[ée]n[ée]rale", title, re.I):
            continue
        try:
            start = date.fromisoformat(t["datetime"][:10])
        except ValueError:
            continue
        parts = re.split(r"\s+[—–-]\s+", clean(t.get_text()))
        end = fr_date(parts[1], start.year) if len(parts) > 1 else None
        if end and end < start:
            end = date(end.year + 1, end.month, end.day)
        if end and (end - start).days > 300:      # a whole season ("Programmation 2026-2027"), not a show
            continue
        sub = li.select_one(".sub")
        name = title + (f" — {clean(sub.get_text())}" if sub and clean(sub.get_text()) else "")
        if end and end > start:
            out.append(Ev("DARE-DARE", name, start, base + a["href"], end, "DARE-DARE", "exhibition"))
        else:
            out.append(Ev("DARE-DARE", name, start, base + a["href"], location="DARE-DARE", kind="Event"))
    return out


def fais_moi_lart() -> list[Ev]:
    """Fais-moi l'art 'En cours & à venir': blocks of  title / artist / 'Du 4 au 19 décembre 2026' /
    'Vernissage le 4 décembre 2026, de 18h à 21h'. Shows become exhibitions, vernissages become events."""
    url = "https://www.faismoilart.org/en-cours"
    s = soup(url)
    lines = [clean(l) for l in (s.find("main") or s).get_text("\n").split("\n") if clean(l)]
    meta = re.compile(r"^(?:en cours|à venir…?|station libre\s*\|?|www\.|https?://)", re.I)
    span = re.compile(r"^Du\s+(\d{1,2})(?:\s*(?:er)?\s+([a-zéû]+))?\s+au\s+(.+\d{4})", re.I)
    out = []
    for i, line in enumerate(lines):
        m = span.match(line)
        if not m:
            continue
        end = fr_date(m.group(3))
        if not end:
            continue
        mo = FR_MONTHS.get((m.group(2) or "").lower(), end.month)
        start = date(end.year - (1 if mo > end.month else 0), mo, int(m.group(1)))
        # title / artist = the 1–2 lines just above, stopping at headings, dates and notes ('Réception … 2027 (TBC)')
        names = []
        for l in reversed(lines[max(0, i - 3):i]):
            if meta.match(l) or span.match(l) or re.search(r"\d{4}|^vernissage|^r[ée]ception", l, re.I):
                break
            names.insert(0, l)
        names = names[-2:]
        title = " — ".join(names) if names else "Exposition"
        out.append(Ev("Fais-moi l'art", title, start, url, end, "Fais-moi l'art", "exhibition"))
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.lower().startswith("vernissage"):
            d = fr_date(nxt, end.year)
            if d:
                out.append(Ev("Fais-moi l'art", f"Vernissage: {title}", local(d, parse_time(nxt)), url,
                              location="Fais-moi l'art", kind="Vernissage"))
    return out


def occurrence() -> list[Ev]:
    """Occurrence (Squarespace pages). Exhibitions carry their dates in the title ('… | 06.11.-12.12.2026');
    events say the date in their text ('Le jeudi 26 novembre, de 17 h à 19 h'), so the newest few are opened."""
    base = "https://occurrence.ca"
    out = []
    for it in get(base + "/expositions", params={"format": "json"}).json().get("items", []):
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", it.get("title") or "")
        if not m:
            continue
        d1, m1, d2, m2, y = map(int, m.groups())
        end = date(y, m2, d2)
        start = date(y - (1 if m1 > m2 else 0), m1, d1)
        title = " — ".join(p.strip() for p in (it["title"][:m.start()]).split("|") if p.strip())
        out.append(Ev("Occurrence", title, start, base + it["fullUrl"], end, "Occurrence", "exhibition"))
    items = get(base + "/evenements", params={"format": "json"}).json().get("items", [])
    for it in items[:6]:   # newest first; older ones are past
        try:
            page = soup(base + it["fullUrl"])
        except requests.RequestException:
            continue
        text = clean((page.find("main") or page).get_text(" "))
        m = re.search(r"\bLe\s+(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche\s+)?\s*(\d{1,2}\s*(?:er)?\s+[a-zéû]+(?:\s+\d{4})?)"
                      r"(?:,?\s+de\s+(\d{1,2})\s*h\s*(\d{2})?)?", text, re.I)
        if not m:
            continue
        added = datetime.fromtimestamp((it.get("publishOn") or it.get("addedOn") or 0) / 1000, TZ).date()
        d = fr_date(m.group(1), None if re.search(r"\d{4}", m.group(1)) else added.year)
        if not d:
            continue
        if d < added - timedelta(days=30):   # 'jeudi 26 novembre' written in November means this year or next
            d = date(d.year + 1, d.month, d.day)
        t = time(int(m.group(2)), int(m.group(3) or 0)) if m.group(2) else None
        out.append(Ev("Occurrence", clean(it.get("title")), local(d, t), base + it["fullUrl"],
                      location="Occurrence", kind="Event"))
    return out


def foil() -> list[Ev]:
    """FOIL / Finer Objects in Life (gallery + café, Shopify). The homepage 'events' list has 'november 26' and a
    title per row; each row's detail panel (inside a <template>) has 'info: 6pm - 10pm'. No year: guessed."""
    base = "https://finerobjectsinlife.com"
    s = soup(base + "/")
    out = []
    for row in s.select("a.hev__row"):
        d_el, t_el = row.select_one(".hev__date"), row.select_one(".hev__title")
        if not (d_el and t_el):
            continue
        m = re.search(r"([a-z]+)\s+(\d{1,2})", d_el.get_text().lower())
        if not m or m.group(1) not in EN_MONTHS:
            continue
        mo, dy = EN_MONTHS[m.group(1)], int(m.group(2))
        info = ""
        tpl = s.find("template", attrs={"data-ev-tpl": row.get("data-ev")})
        if tpl:   # template content isn't parsed as children: re-read it
            inner = BeautifulSoup(tpl.decode_contents(), "html.parser")
            for dv in inner.select("dl.evd > div"):
                dt = dv.find("dt")
                if dt and clean(dt.get_text()).lower() == "info":
                    info = clean(dv.find("dd").get_text(" ")) if dv.find("dd") else ""
        t = parse_time(info)
        title = clean(t_el.get_text())
        kind = "Vernissage" if re.search(r"opening|vernissage", title, re.I) else "Event"
        out.append(Ev("FOIL", title, local(date(guess_year(mo, dy), mo, dy), t),
                      f"{base}/#event-{row.get('data-slug')}" if row.get("data-slug") else base + "/#events",
                      location="FOIL, 6560 rue Waverly", kind=kind))
    return out


def espace_transmission_site() -> list[Ev]:
    """espacetransmission.ca/événements (Cargo site): entries like '14.11.24. Performance multimedia …' or
    '27-01.10.24 POP MONTREAL Exposition …' (Sept 27 – Oct 1). Mostly an archive; only future dates survive."""
    url = "https://espacetransmission.ca/%C3%A9v%C3%A9nements"
    text = clean(soup(url).get_text(" "))
    out = []
    rx = re.compile(r"(\d{1,2})(?:-(\d{1,2}))?\.(\d{1,2})\.(\d{2})\.?\s+(.+?)(?=\s+Cr[ée]dits?\s+photos?|\s+\d{1,2}(?:-\d{1,2})?\.\d{1,2}\.\d{2}|$)")
    for m in rx.finditer(text):
        d1, d2, mo, yy, title = m.groups()
        y = 2000 + int(yy)
        try:
            if d2:   # a range ending on d2.mo; '27-01.10' starts in the previous month
                end = date(y, int(mo), int(d2))
                smo = int(mo) if int(d1) <= int(d2) else int(mo) - 1
                start = date(y if smo >= 1 else y - 1, smo if smo >= 1 else 12, int(d1))
            else:
                start, end = date(y, int(mo), int(d1)), None
        except ValueError:
            continue
        title = clean(re.sub(r"[\s,]*@[\w.]+", "", title)).strip(" ,") [:140]   # drop the Instagram handles
        if end and end > start:
            out.append(Ev("Espace Transmission", title, start, url, end, "Espace Transmission, 5435 av. des Érables", "exhibition"))
        else:
            out.append(Ev("Espace Transmission", title, start, url, location="Espace Transmission, 5435 av. des Érables",
                          kind="Event"))
    return out


def marches_publics() -> list[Ev]:
    """Marchés publics de Montréal events (Jean-Talon, Atwater, Maisonneuve…). Each event page gives weekend ranges
    like '4 - 6 décembre 2026'; each range becomes one multi-day entry. Toddler workshops are skipped."""
    base = "https://www.marchespublics-mtl.com"
    s = soup(base + "/fr/evenements")
    links = dict.fromkeys(a["href"] for a in s.find_all("a", href=True)
                          if re.match(r"^/fr/evenements/[a-z0-9\-]+$", a["href"]))
    out = []
    for path in links:
        if re.search(r"tout-petits|enfants|bambins", path):
            continue
        page = soup(base + path)
        main = page.find("main") or page
        h = main.find(["h1", "h2"])
        title = clean(h.get_text(" ")) if h else path.split("/")[-1]
        where = "Marché Jean-Talon" if "jean-talon" in path else "Marché Atwater" if "atwater" in path else \
            "Marché Maisonneuve" if "maisonneuve" in path else "Marchés publics de Montréal"
        if where not in title:
            title = f"{title} — {where}"
        text = clean(main.get_text(" "))
        for m in re.finditer(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([a-zéû]+)\s+(\d{4})", text):
            mo = FR_MONTHS.get(m.group(3).lower())
            if not mo:
                continue
            start, end = date(int(m.group(4)), mo, int(m.group(1))), date(int(m.group(4)), mo, int(m.group(2)))
            out.append(Ev("Marchés publics", title, start, base + path, end, where, "exhibition"))
    # the same weekend ranges sit in the site's menu on every page: keep one per (title, range)
    return list({(e.title, e.start, e.end): e for e in out}.values())


MTL_LIBRARY_KEEP = re.compile(
    r"conf[ée]rence|rencontre|ap[ée]ro|litt[ée]raire|auteur|autrice|lancement|causerie|club de lecture|"
    r"atelier d.[ée]criture|[ée]criture|po[ée]sie|slam|concert|spectacle|musique|projection|cin[ée]|film|"
    r"exposition|vernissage|th[ée][âa]tre|danse|table ronde|discussion|d[ée]bat|lecture", re.I)
MTL_LIBRARY_SKIP = re.compile(
    r"heure du conte|miniconte|conte|b[ée]b[ée]|tout-petits|0-\d+ mois|\d+-\d+ ans|enfants|famille|jeunesse|"
    r"ado|devoirs|ping!|initiation au num|num[ée]rique|tricot|[ée]checs|jeux|lego|yoga|aide|francisation|"
    r"conversation|cours d.anglais|club des petits|b[ée]n[ée]volat", re.I)


# City Events tab, "Other city venues": not council meetings or committee sittings, not fitness classes,
# conversation groups or tech help (the library kids/family filter above applies too)
CITY_ADMIN = re.compile(r"s[ée]ance (?:ordinaire |extraordinaire )?du (?:conseil|comit[ée])|conseil d.arrondissement|"
                        r"comit[ée] ex[ée]cutif|consultation|rencontre (?:du samedi )?avec (?:un |une )?[ée]lu", re.I)
CITY_SKIP = re.compile(r"yoga|tai-?chi|cardio|zumba|pilates|step\b|mise en forme|activit[ée] libre|bien-[êe]tre|"
                       r"aide technologique|cuisine z[ée]ro|recettes et conversation|scolaire|\[scolaire\]|ados?\b|"
                       r"parents?-|m[èe]re-b[ée]b|activit[ée]s? sportives?|danse urbaine|club des", re.I)


def montreal_ca(days: int = 14) -> list[Ev]:
    """montreal.ca/evenements (the City's listing, ~2000 entries). Kept: Maison de la culture events, and adult
    cultural events at the libraries (talks, author meetings, readings, concerts, screenings…; no storytime,
    homework help, chess…), in the next `days` days. Each kept event's page gives its price ('Gratuit')."""
    base = "https://montreal.ca"
    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=days)
    cards, page = [], 1
    while page <= 30:
        s = soup(f"{base}/evenements?page={page}")
        found = 0
        for a in s.find_all("a", href=True):
            if re.match(r"^/evenements/[a-z0-9\-]+-\d+$", a["href"]):
                box = a.find_parent(["li", "article", "div"])
                cards.append((a["href"], clean(a.get_text(" ")), clean(box.get_text(" ")) if box else ""))
                found += 1
        if not found:
            break
        page += 1
    out, seen = [], set()
    for href, title, txt in cards:
        if href in seen or re.search(r"\bDu\b.+\bau\b|annul[ée]|b[ée]n[ée]volat", txt, re.I):
            continue
        seen.add(href)
        d = fr_date(txt)
        if not d or not (today <= d <= horizon):
            continue
        tm = re.search(r"\bà\s+(\d{1,2})\s*h\s*(\d{2})?", txt)
        t = time(int(tm.group(1)), int(tm.group(2) or 0)) if tm else None
        place = clean(txt[tm.end():] if tm else txt.split(str(d.year))[-1])
        # the card link holds the whole card text: cut the title at the date ("… Mercredi 25 novembre 2026 à …")
        name = re.split(r"\s+(?:Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche)\s+\d", title or txt)[0].strip()
        if re.search(r"\bReport[ée]\b", name):   # postponed: no new date yet
            continue
        sold_out = bool(re.search(r"\s+Complet$", name))
        name = re.sub(r"\s+Complet$", "", name)
        is_mdc = bool(re.search(r"maison de la culture", place, re.I))
        is_lib = "Bibliothèque" in place or "MédiaLAB des bibliothèques" in place
        if is_mdc:
            src = "Maisons de la culture"
        elif is_lib:
            if not (MTL_LIBRARY_KEEP.search(name) and not MTL_LIBRARY_SKIP.search(name)):
                continue
            src = "Bibliothèques de Montréal"
        else:   # other city venues: cultural centres, community centres, halls, parks
            if CITY_ADMIN.search(name) or MTL_LIBRARY_SKIP.search(name) or CITY_SKIP.search(name) or place == "En ligne":
                continue
            src = "Other city venues"
        if sold_out:
            name += " (complet / sold out)"
        ev = Ev(src, name, local(d, t), base + href, location=place[:120])
        try:   # price and end time from the event page
            page = soup(base + href)
            pt = clean((page.find("main") or BeautifulSoup("", "html.parser")).get_text(" "))
            for sc in page.find_all("script", type="application/ld+json"):   # description -> tags (concert, film…)
                if '"Event"' in (sc.string or ""):
                    try:
                        ldd = json.loads(sc.string, strict=False)
                        img = ldd.get("image") if isinstance(ldd.get("image"), str) else ""
                        ev.image = img.replace("/w_1920,", "/w_600,")   # Cloudinary: ask for a smaller copy
                        desc = ldd.get("description") or ""
                        ev.blurb = clean(BeautifulSoup(desc, "html.parser").get_text(" "))[:600]
                    except ValueError:
                        pass
            if re.search(r"\bGratuit\b", pt):
                priced(ev, True)
            elif (pm := re.search(r"(\d+(?:[.,]\d{2})?)\s*\$", pt)):
                priced(ev, None, float(pm.group(1).replace(",", ".")))
            em = re.search(r"\bde\s+\d{1,2}\s*h\s*\d{0,2}\s+à\s+(\d{1,2})\s*h\s*(\d{2})?", pt)
            if em and t:
                ev.end = datetime.combine(d, time(int(em.group(1)) % 24, int(em.group(2) or 0)), TZ)
        except requests.RequestException:
            pass
        out.append(ev)
    return out


def ateliers_et_saveurs(days: int = 14) -> list[Ev]:
    """Ateliers & Saveurs public agendas (cooking, cocktail, wine classes). Montreal branches only, next `days`
    days, classes that still have places. (Their JSON feed answers 403, so the public pages are used.)"""
    base = "https://www.ateliersetsaveurs.com"
    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=days)
    out = []
    for agenda, kind in (("cours-de-cuisine", "Cooking class"), ("cours-de-cocktails", "Cocktail class"),
                         ("degustations-de-vin", "Wine tasting")):
        for p in range(1, 10):
            s = soup(f"{base}/agenda/{agenda}" + (f"?p={p}" if p > 1 else ""))
            boxes = s.select("div.inside-box")
            if not boxes:
                break
            past_horizon = False
            for box in boxes:
                parts = [clean(x) for x in box.get_text("\n").split("\n") if clean(x)]
                txt = " | ".join(parts)
                d = fr_date(txt, today.year)
                if not d:
                    continue
                if d < today - timedelta(days=60):
                    d = date(d.year + 1, d.month, d.day)
                if d > horizon:
                    past_horizon = True
                    continue
                if "Québec" in parts[:6] or re.search(r"\b0 places\b|Complet|Terminé", txt):
                    continue
                branch = next((b for b in ("Vieux-Montréal", "Plateau Mont-Royal", "Time Out Market") if b in txt), "")
                if not branch:
                    continue
                tm = re.search(r"(\d{1,2}):(\d{2})\s*à\s*(\d{1,2}):(\d{2})", txt)
                if not tm:
                    continue
                start = datetime.combine(d, time(int(tm.group(1)), int(tm.group(2))), TZ)
                end = datetime.combine(d, time(int(tm.group(3)), int(tm.group(4))), TZ)
                link = box.find("a", href=re.compile(r"/cours/detail/"))
                i = next((n for n, x in enumerate(parts) if x.startswith("Plus d")), None)
                # the dish is the last line before "Plus d'informations" that isn't a note ("Où trouver le matériel…")
                dish = next((x for x in reversed(parts[:i] if i else []) if not re.match(r"(?i)o[uù] trouver|\d|.*\?$", x)
                             and "$" not in x), "")
                cat = next((x for x in parts if x.isupper() and len(x) > 3), kind)
                title = f"{cat.title()}: {dish}" if dish else cat.title()
                ev = Ev("Ateliers & Saveurs", title[:140], start, base + link["href"] if link else f"{base}/agenda/{agenda}",
                        end, f"Ateliers & Saveurs {branch}", kind)
                pm = re.search(r"(\d+(?:[.,]\d{2})?)\s*\$", txt)
                out.append(priced(ev, None, float(pm.group(1).replace(",", "."))) if pm else ev)
            if past_horizon:
                break
    return list({(e.url, e.start): e for e in out}.values())


def linktree_dated(url: str, source: str, location: str = "") -> list[Ev]:
    """A Linktree page whose ticket links carry a date in their name, e.g. 'BILLETS | Heures ouvrables 007 (6 mars 2026)'."""
    nd = json.loads(soup(url).find("script", id="__NEXT_DATA__").string)
    props = nd["props"]["pageProps"]
    out = []
    for l in props.get("links") or (props.get("account") or {}).get("links") or []:
        title = clean(l.get("title") or "")
        d = fr_date(title) if re.search(r"\d{4}", title) else None
        if not d:
            continue
        name = re.sub(r"^\s*(?:billets?|tickets?)\s*\|\s*", "", title, flags=re.I)
        name = re.sub(r"\s*\([^)]*\d{4}[^)]*\)\s*$", "", name).strip() or source
        out.append(Ev(source, name, d, l.get("url") or url, location=location))
    return out


# ------------------------------------------------------------------- movies
CC_CINEMAS = {1: "Cinéma Beaubien", 2: "Cinéma du Parc", 3: "Cinéma du Musée"}


def cinema_cinema(days: int = 14) -> list[Ev]:
    """Cinéma Beaubien / du Parc / du Musée. The /horaire page embeds one day of showtimes (?date=YYYY-MM-DD)."""
    base = "https://cinemacinema.ca"
    rep = re.compile(r'\{cinema_id:(\d+),representation_id:(\d+),film_id:(\d+),titre:"((?:[^"\\]|\\.)*)",'
                     r'representation_date:"(\d{4}-\d{2}-\d{2})[^"]*",version:"([^"]*)",heure_debut:"([^"]*)"')
    out, seen = [], set()
    today = datetime.now(TZ).date()
    for i in range(days):
        day = today + timedelta(days=i)
        r = get(f"{base}/fr/horaire", params={"date": day.isoformat()})
        r.encoding = "utf-8"          # the server sends no charset
        html = r.text
        film_url = {m.group(2): base + m.group(1) for m in re.finditer(r'href="(/fr/films/[a-z0-9-]+-(\d+))"', html)}
        minutes = {m.group(2): int(m.group(1))
                   for m in re.finditer(r"duree:(\d+),representations:\[\{cinema_id:\d+,representation_id:\d+,"
                                        r"film_id:(\d+)", html)}
        posters = {m.group(1): m.group(2) for m in re.finditer(r'film_id:(\d+),[^{}]*?poster_url:"([^"]+)"', html)}
        for m in rep.finditer(html):
            cid, rid, fid, titre, ds, version, heure = m.groups()
            t = parse_time(heure)
            if rid in seen or not t:
                continue
            seen.add(rid)
            start = datetime.combine(date.fromisoformat(ds), t, TZ)
            title = json.loads(f'"{titre}"') + (f" ({version})" if version else "")
            cinema = CC_CINEMAS.get(int(cid), "Cinéma Cinéma")
            end = start + timedelta(minutes=minutes[fid]) if minutes.get(fid) else None
            out.append(Ev(cinema, title, start, film_url.get(fid, f"{base}/fr/horaire?date={ds}"), end, cinema,
                          # their image service resizes on the fly: a 300px poster instead of a ~2000px original
                          image=("https://cm-api.cinemacinema.ca/cdn-cgi/image/width=300,format=auto/" + posters[fid])
                                if posters.get(fid) else ""))
    return out


def cinema_moderne() -> list[Ev]:
    """Cinéma Moderne's /horaire month calendar (this month and next)."""
    base = "https://www.cinemamoderne.com/horaire/"
    today = date.today()
    nxt = date(today.year + today.month // 12, today.month % 12 + 1, 1)
    out, seen = [], set()
    for url in (base, f"{base}{nxt.year}/{nxt.month:02d}"):
        s = soup(url)
        for ev in s.select("div.cm-Cal__day__event"):
            a = ev.select_one(".cm-Cal__day__event__title a[href]")
            meta = ev.select_one(".cm-Card__meta .tablet-inline") or ev.select_one(".cm-Card__meta")
            title_el = ev.select_one(".cm-Card__title")
            if not (a and meta and title_el):
                continue
            m = re.search(r"(\d{1,2})\s+([a-zéû]+)", meta.get_text().lower())
            t = parse_time(meta.get_text())
            if not m or m.group(2) not in FR_MONTHS or not t:
                continue
            mo, dy = FR_MONTHS[m.group(2)], int(m.group(1))
            start = datetime.combine(date(guess_year(mo, dy), mo, dy), t, TZ)
            key = (a["href"], start)
            if key in seen:
                continue
            seen.add(key)
            dur = re.search(r"\b(\d{1,3})\s*minutes", ev.get_text(" "))
            out.append(Ev("Cinéma Moderne", clean(title_el.get_text(" ")), start, a["href"],
                          start + timedelta(minutes=int(dur.group(1))) if dur else None, "Cinéma Moderne"))
    return out


EVENT_SOURCES = {
    "SAT": sat,
    "Favourite Library": favourite_library,
    "Casa del Popolo": casa_del_popolo,
    "Le Red Room": red_room,
    "Vino Disco": lambda: ra_events("promoter", "133188", "Vino Disco"),
    "Bar Datcha": lambda: ra_events("venue", "94451", "Bar Datcha"),
    # Barbossa (3956A St-Laurent): its own site is a Facebook page; its club nights go on RA venue 179148
    "Barbossa": lambda: ra_events("venue", "179148", "Barbossa"),
    # jazz & blues rooms
    "Dièse Onze": dieseonze,
    "Bistro à Jojo": bistro_a_jojo,
    "Bootlegger": bootlegger,   # jazz Wed & Sun, DJs Fri & Sat (its Tuesday comedy comes from Laugh It Off)
    # LATEX. (latexrave.com): queer fetish rave, 20+; tickets on Ticket Tailor (organizer "slataprod")
    "LATEX.": lambda: tickettailor("slataprod", "LATEX."),
    "5 à Set": lambda: eventbrite_organizer("https://www.eventbrite.ca/o/5-a-set-120716281994", "5 à Set"),
    "Le Marché Vintage": lambda: eventbrite_organizer("https://www.eventbrite.ca/o/le-marche-vintage-65263991233",
                                                      "Le Marché Vintage"),
    "Stereo": lambda: ra_events("venue", "828", "Stereo"),
    "Salon Daomé": lambda: ra_events("venue", "153067", "Salon Daomé"),
    "Turbo Haüs": turbo_haus,
    "Le Belmont": le_belmont,
    "MTL Cookbook Club": lambda: luma_calendar("https://luma.com/mtlcookbookclub", "MTL Cookbook Club"),
    "Soft Studio Days": lambda: luma_user("usr-RktHUOe1HAU874v", "Soft Studio Days"),
    "Laugh It Off": laugh_it_off,
    "Newspeak": newspeak,
    "Boiler Room": boiler_room,
    "Bar Le Ritz PDB": lambda: squarespace_shows("https://www.barleritzpdb.com", "Bar Le Ritz PDB",
                                                 "Bar Le Ritz PDB, 179 Rue Jean-Talon O"),
    "Théâtre Fairmount": lambda: squarespace_shows("https://www.theatrefairmount.com", "Théâtre Fairmount",
                                                   "Théâtre Fairmount, 5240 Av. du Parc"),
    # L'Escogriffe's own site is a Facebook page. Live Nation lists the shows it sells there (Songkick has more but
    # refuses GitHub's servers); RA gets the odd club night.
    "L'Escogriffe": lambda: livenation_venue("KovZ917AIrp/l-escogriffe-bar-spectacle-events", "L'Escogriffe",
                                             "L'Escogriffe, 4461 rue St-Denis"),
    "L'Escogriffe (RA)": lambda: ra_events("venue", "124247", "L'Escogriffe"),
    "Ausgang Plaza": lambda: ra_events("venue", "158331", "Ausgang Plaza"),
    "Quai des Brumes": quai_des_brumes,
    "Sans Soleil": lambda: ra_events("venue", "203475", "Sans Soleil"),   # basement bar; site is Instagram only
    "Blue Room": lambda: ra_events("venue", "290582", "Blue Room"),       # 3956 Saint-Laurent, 3rd floor; Instagram only
    # (Heures ouvrables removed: its Linktree refuses requests from GitHub's servers; linktree_dated() is kept)
    # multi-use space (5435 av. des Érables); raves and parties go on RA venue 235410
    "Espace Transmission": lambda: ra_events("venue", "235410", "Espace Transmission"),
    # museum evenings out (DJ, bar, workshops) — listed under Other Events rather than Museums & Galleries
    "MBAM La Buvette": lambda: mbam_series("la-buvette"),
    "McCord After Hours": mccord_after_hours,
    # Other Events: city culture (Maisons de la culture + adult library events), markets, cooking classes
    "Ville de Montréal": montreal_ca,
    "Marchés publics": marches_publics,
    "Ateliers & Saveurs": ateliers_et_saveurs,
    # tixr.com/groups/stereobar blocks automated access; the same nights are on RA venue 138829
    "StereoBar": lambda: ra_events("venue", "138829", "StereoBar"),
    "Système": lambda: ra_events("venue", "201918", "Système"),   # systememtl.com links to RA club 201918
    "New City Gas": new_city_gas,
    # Eastern Bloc posts mostly on Instagram; its parties and Sight+Sound festival go on RA venue 147287
    "Eastern Bloc": lambda: ra_events("venue", "147287", "Eastern Bloc"),
    "Trivia Nights": lambda: badslava("trivia-nights.php?city=Montreal&state=QC", "Trivia Nights", "Trivia Night"),
    "Comedy Open Mics": lambda: badslava("open-mics.php?city=Montreal&state=QC&type=Comedy", "Comedy Open Mics",
                                         "Comedy Open Mic"),
    "Karaoke Nights": lambda: badslava("karaoke-nights.php?city=Montreal&state=QC", "Karaoke Nights", "Karaoke"),
    "Added by hand": by_hand,   # events carry their own chip name (e.g. "Trivia Nights")
    "Oyster deals": oyster_deals,   # Oysters tab; each place is its own chip
    # more bar trivia (after badslava and the by-hand list, so those win when both list the same night)
    "TriviaNearMe": trivianearme,
}


# ------------------------------------------------------------- cleanup step
# Festival passes / ticket bundles are listed like events but aren't one.
PASS_TITLES = re.compile(r"\bpass(?:e)?\s+(?:du|de|for|festival|week-?end|journ)|\bfestival\s+pass\b|\bforfait\b|"
                         r"\bpasseport\b|\b(?:day|weekend|week-end|full)\s+pass\b", re.I)
_MON = (r"(?:jan(?:uary|vier)?|feb(?:ruary)?|f[ée]v(?:rier)?|mar(?:ch|s)?|apr(?:il)?|avr(?:il)?|may|mai|jun(?:e)?|juin|"
        r"jul(?:y)?|juil(?:let)?|aug(?:ust)?|ao[uû]t?|sep(?:t(?:ember|embre)?)?|oct(?:ober|obre)?|nov(?:ember|embre)?|"
        r"dec(?:ember)?|d[ée]c(?:embre)?)\.?")
_DAY = r"\d{1,2}(?:st|nd|rd|th|er)?"
_DATE = rf"(?:(?:mon|tue|wed|thu|fri|sat|sun)\w*\.?,?\s+)?(?:{_MON}\s+{_DAY}|{_DAY}\s+{_MON})(?:,?\s+\d{{4}})?"
TRAILING_DATE = re.compile(rf"\s*(?:[-–—,|]|\bon\b|\ble\b)\s*{_DATE}\s*$", re.I)
# Several venues file every show under one place; drop "same time, same place" repeats for these
# (Laugh It Off lists each night as a series AND as a dated show; SAT adds a generic "Dômesicle Winter" next to each night)
SAME_SLOT_DUPES = {"Laugh It Off", "SAT"}
# Sources whose titles the scraper builds itself (badslava bar nights, hand-added nights)
BUILT_TITLES = {"Trivia Nights", "Comedy Open Mics", "Karaoke Nights"}


def _plain(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def tidy_title(e: Ev) -> str:
    """Take the venue and date back out of titles like 'X @ P'TIT OURS, 26 NOV' or 'Show - December 2'."""
    t = clean(e.title)
    t = TRAILING_DATE.sub("", t)
    m = re.search(r"\s+(?:@|\bat\b)\s+([^@]+)$", t)
    if m:   # '@ venue' or 'at venue' at the end, only when it names this venue (keeps "CIGARETTES @ SUNSET")
        where = _plain(f"{e.location} {e.source} casa del popolo sala rossa sotterenea p'tit ours toscadura")
        words = [w for w in re.findall(r"[a-z']{4,}", _plain(m.group(1))) if w not in ("montreal", "presents")]
        if words and all(w in where or re.fullmatch(_DATE, w, re.I) for w in words):
            t = t[:m.start()]
    t = TRAILING_DATE.sub("", t)
    return re.sub(r"[\s|/,:–—-]+$", "", t).strip() or clean(e.title)


def sane_date(e: Ev) -> bool:
    """Drop dates that can't be right: a show more than 18 months out, or ending before it starts.
    (Exhibitions may legitimately have opened years ago, e.g. the Planétarium's Aurorae, on since 2015.)"""
    start = e.start if isinstance(e.start, date) and not isinstance(e.start, datetime) else e.start.date()
    if e.kind != "exhibition" and start > date.today() + timedelta(days=550):
        return False
    if e.end is not None and type(e.end) is type(e.start) and e.end < e.start:
        return False
    return True


def clean_events(evs: list[Ev]) -> list[Ev]:
    out = []
    for e in evs:
        if SKIP_TITLES.match(e.title) or PASS_TITLES.search(e.title):
            continue
        if e.source not in BUILT_TITLES:   # titles the scraper writes itself ("Trivia Night at Louks Pub") stay as they are
            e.title = tidy_title(e)
        out.append(e)
    keep: dict[tuple, Ev] = {}
    for e in out:
        if e.source not in SAME_SLOT_DUPES:
            keep[(id(e),)] = e
            continue
        k = (e.source, e.start, e.location)
        best = keep.get(k)
        # prefer the one with price info, then the more specific (longer) title
        if best is None or (e.price_lo is not None, len(e.title)) > (best.price_lo is not None, len(best.title)):
            keep[k] = e
    return _same_lineup_dupes(list(keep.values()))


def _lineup(title: str) -> frozenset:
    """The names in a title, ignoring order, case, accents and joiners: 'SOMEBODY3LSE + SILKTITS' == 'Silktits & somebody3lse'."""
    stop = {"and", "et", "with", "avec", "feat", "the", "les", "presents", "présente", "presente"}
    return frozenset(w for w in re.findall(r"[a-z0-9]+", _plain(title)) if len(w) > 2 and w not in stop)


def _same_lineup_dupes(evs: list[Ev]) -> list[Ev]:
    """A venue listing the same lineup twice on one day (e.g. one copy at 8:00 AM, an AM/PM slip): keep the copy
    with a price, else the one starting at a normal show time (not 5–11 AM)."""
    def day(e):
        return e.start if not isinstance(e.start, datetime) else e.start.date()

    def score(e):
        odd_hour = isinstance(e.start, datetime) and 5 <= e.start.hour < 12
        return (e.price_lo is not None, not odd_hour, len(e.title))
    best: dict[tuple, Ev] = {}
    out = []
    for e in evs:
        names = _lineup(e.title)
        if e.kind == "exhibition" or len(names) < 2:
            out.append(e)
            continue
        k = (e.source, day(e), names)
        if k in best and clean(best[k].title).lower() == clean(e.title).lower():
            out.append(e)   # identical title at another time = a separate showing (films, early/late shows)
            continue
        if k not in best or score(e) > score(best[k]):
            best[k] = e
    return out + list(best.values())


# ---------------------------------------------------------------- genre tags
# Matched against titles (and RA's own genre names). Order = order shown on the page.
GENRES = [
    ("Techno", r"techno|\bacid\b|hard ?groove|\bminimal\b|industrial|\bebm\b"),
    ("House", r"\bhouse\b|amapiano"),
    ("Disco", r"disco"),
    ("Bass & DnB", r"\bbass\b|drum ?(?:&|and|n) ?bass|\bdnb\b|jungle|dubstep|\bdub\b|breakbeat|riddim"),
    ("Hip-hop & R&B", r"hip[ -]?hop|\brap\b|r&b|\brnb\b|\bsoul\b|\btrap\b"),
    ("Afro & Latin", r"\bafro|latin|salsa|cumbia|reggaeton|\bbaile\b|brazil|br[ée]sil|bossa|tropical|dancehall|kizomba|zouk"),
    ("Flamenco", r"flamenc"),
    ("Jazz & Blues", r"jazz|blues|\bswing\b|bebop|big band|quartet"),
    ("Classical & Choral", r"quatuor|\bquartet|orchestre|orchestra|symphon|baroque|classique|\bclassical|chorale|\bch(?:oe|œ)ur|\bchoir|op[ée]ra|r[ée]cital|\bpiano\b|violon|\bensemble\b"),
    ("Folk & Country", r"\bfolk|country|bluegrass|honky|string ?band|americana|banjo|sing ?along|yiddish|greek"),
    ("Punk & Hardcore", r"punk|hardcore|grunge|crust|\boi!"),
    ("Metal", r"metal|\bdoom\b|sludge|grindcore|thrash"),
    ("Rock & Indie", r"\brock\b|indie|shoegaze|psych|surf rock|garage rock"),
    ("Experimental", r"experiment|\bnoise\b|drone|ambient|modular|improv|avant|electroacoust|[ée]lectroacoustique|"
                     r"snare drum|musique actuelle|free jazz|\bconcr[eè]te?\b|contemporary music|sound art"),
    ("Pop & Throwback", r"\bpop\b|\b(?:19)?[6-9]0'?s\b|\b2000s?\b|\b2010s\b|tribute|hommage|cover set|throwback|dance party"),
    ("Live Music", r"\bconcerts?\b|chanson|chantons|\bchant\b|r[ée]cital|\blive music|musique live|balafon|guitare|\bband\b"),
    ("Electronic", r"electro|[ée]lectro|\bdj\b|dj set|\bb2b\b|\brave\b|all night long|club\b|\bbpm\b"),
    ("Dance", r"\bdance\b|\bdanse\b|ballroom|vogue|t-dance"),
    ("Comedy", r"comed|com[ée]die|stand-?up|humou?r|\broast\b"),
    ("Listening Party", r"listening party|s[ée]ance d'[ée]coute"),
    ("Holiday Season", r"no[eë]l|christmas|xmas|temps des f[êe]tes|holiday|r[ée]veillon|nouvel an|new year"),
    ("Markets & Fairs", r"\bmarch[ée]\b|\bmarket\b|bazaa?r|\bfoire\b|\bfair\b|temps des f[êe]tes"),
    ("Civic & Community", r"forum citoyen|rencontre citoyenne|consultation publique|caf[ée] r[ée]paration|repair caf|assembl[ée]e citoyenne|corvée|plantation"),
    ("Theatre & Shows", r"th[ée][âa]tre|\bproductions?\b|\bpi[èe]ce\b|cabaret|cirque|marionnette|\bconte\b|\bclown"),
    ("Film", r"\bfilms?\b|cin[ée]ma|documentaire|projection|court[- ]m[ée]trage|long[- ]m[ée]trage"),
    ("Talks & Quizzes", r"podcast|\bquiz\b|trivia|\blecture\b|\breading\b|conf[ée]rence|\bpanel\b|in conversation|artist talk|litt[ée]raire|auteur|autrice|causerie|cabaret scientifique|conf[ée]rence scientifique|vulgarisation|t[ée]moignage"),
    ("Art & Workshops", r"workshop|atelier|drawing|dessin|ceramic|c[ée]ramique|vision board|craft|figure|collage|knit|tricot|vernissage|exposition|exhibition|visite guid|peinture|dessin|cr[ée]ation|textile|broderie|couture|cricut|\b3d\b|fab(?:ric)?a(?:rt)?h[èe]que|sculpture|origami|cours de|bijou|poterie|gravure|photo"),
    ("Food & Drink", r"\bcookies?\b|\bcook|\bcuisine\b|\bfood\b|\bbouffe\b|\bwines?\b|\bvins?\b|\btasting\b|d[ée]gustation|potluck|\bbrunch\b"),
]
_GENRE_RX = [(g, re.compile(p, re.I)) for g, p in GENRES]
ELECTRONIC_KIDS = {"Techno", "House", "Disco", "Bass & DnB"}
# When a title says nothing about the music: what the venue mostly does.
VENUE_GENRES = {
    "LATEX.": ["Electronic", "Techno"], "Stereo": ["Electronic", "House"], "StereoBar": ["Electronic", "House"], "Sans Soleil": ["Electronic"],
    "Blue Room": ["Electronic"], "Heures ouvrables": ["Dance", "Electronic"], "New City Gas": ["Electronic"], "Système": ["Electronic"],
    "Salon Daomé": ["Electronic", "House"], "Bar Datcha": ["Electronic"], "Vino Disco": ["Disco", "House", "Electronic"],
    "Ausgang Plaza": ["House", "Electronic"], "Le Red Room": ["Electronic", "Techno"], "SAT": ["Electronic"],
    "Boiler Room": ["Electronic"], "Eastern Bloc": ["Electronic", "Experimental"], "Newspeak": ["Electronic"],
    "Igloofest": ["Electronic"], "Dièse Onze": ["Jazz & Blues"], "Bistro à Jojo": ["Jazz & Blues"], "Turbo Haüs": ["Punk & Hardcore", "Rock & Indie"], "Le Belmont": ["Pop & Throwback", "Dance"],
    # (no default for the live-band rooms — Casa del Popolo, Fairmount, Le Ritz, Quai des Brumes, L'Escogriffe:
    #  they book everything from folk metal to blues to improv, so an untagged show there stays untagged)
    "Laugh It Off": ["Comedy"], "Comedy Open Mics": ["Comedy"],
    "Soft Studio Days": ["Art & Workshops"], "MTL Cookbook Club": ["Food & Drink"], "5 à Set": ["Listening Party"],
    "MBAM": ["Food & Drink", "Art & Workshops", "Electronic"],   # La Buvette: bar, art workshop, DJ
    "McCord Museum": ["Food & Drink", "Art & Workshops"],        # After Hours: cocktails, workshops, performances
    "Favourite Library": ["Rock & Indie"],                       # Handmade house shows (indie / songwriters)
    "Le Marché Vintage": ["Markets & Fairs"],
    "Marchés publics": ["Markets & Fairs", "Food & Drink"], "Holiday markets": ["Markets & Fairs", "Food & Drink"],
    "Ateliers & Saveurs": ["Food & Drink", "Art & Workshops"],
}


def genres_in(text: str) -> list[str]:
    found = [g for g, rx in _GENRE_RX if rx.search(text or "")]
    if ELECTRONIC_KIDS & set(found) and "Electronic" not in found:
        found.append("Electronic")
    return found


# "Présenté par …" in a venue's description says a lot when the title is just a name.
PRESENTER_GENRES = [
    (re.compile(r"extensive enterprise", re.I), ["Metal"]),        # metal promoter (Korpiklaani, Elder…)
    (re.compile(r"heavy mtl", re.I), ["Rock & Indie"]),            # heavy rock / stoner
    (re.compile(r"blue skies turn black", re.I), ["Rock & Indie"]),
    (re.compile(r"what the dance|super taste", re.I), ["Pop & Throwback", "Dance"]),
]
# Stand-up shows billed under the comedian's name only (no "comedy" in the title).
COMEDY_PRESENTERS = re.compile(r"pr[ée]sent[ée] par (?:outback|juste pour rire|just for laughs|comedy works|"
                               r"zoofest|grimaldi)", re.I)
COMEDIANS = {"ivan decker", "zainab johnson"}   # add names here when one gets misfiled as music


def is_comedy(e: Ev) -> bool:
    name = _plain(e.title.split(":")[0]).strip()
    return name in COMEDIANS or bool(COMEDY_PRESENTERS.search(e.blurb)) or bool(
        re.search(r"\b(?:stand-?up|humoriste|comedian|com[ée]dien)\b", e.blurb, re.I))


JAZZ_ROOMS = {"Dièse Onze", "Bistro à Jojo"}


def tag_genres(e: Ev, tab: str) -> None:
    if tab in ("museums", "movies", "oysters"):
        return
    if tab == "classical":
        e.genres = classical_types(e)
        return
    tags = list(dict.fromkeys(e.genres + genres_in(e.title) + genres_in(e.blurb)))
    if is_comedy(e):
        tags = ["Comedy"]
    for rx, gs in PRESENTER_GENRES:
        if rx.search(e.blurb) and not [g for g in tags if g not in ("Dance", "Electronic")]:
            tags += [g for g in gs if g not in tags]
    non_music = ("Comedy", "Dance", "Listening Party", "Talks & Quizzes", "Art & Workshops", "Food & Drink",
                 "Markets & Fairs", "Theatre & Shows", "Film", "Civic & Community", "Holiday Season")
    music = [g for g in tags if g not in non_music]
    # venue defaults: music venues only in Music & Dance; other sources (comedy, workshops) everywhere,
    # and never on a night the title already marks as comedy / a talk / a quiz
    defaults = VENUE_GENRES.get(e.source, [])
    music_defaults = [g for g in defaults if g not in non_music]
    # (sources filed under Other Events on purpose — a house show, a museum evening — get their full default)
    music_ok = tab == "music" or e.source in OTHER_SOURCES
    if not music and not (set(tags) & {"Comedy", "Talks & Quizzes"}):
        tags += [g for g in defaults if (music_ok or g not in music_defaults) and g not in tags]
    elif not tags:
        tags += [g for g in defaults if g not in music_defaults]
    if tab != "music" and set(tags) & {"Comedy", "Talks & Quizzes"}:
        # a talk or comedy night outside Music & Dance: words like "house" in "communauté de la house danse"
        # describe the topic, not the music (keeps it out of the House filter)
        tags = [g for g in tags if g in non_music]
    if tab == "music":   # "Productions" in a promoter's name, "Cirque Cosmic" the DJ… not theatre
        tags = [g for g in tags if g != "Theatre & Shows"]
    if e.source in JAZZ_ROOMS:   # a "Quartet" at a jazz club isn't classical; "Matt Rock" plays the blues
        tags = [g for g in tags if g not in ("Classical & Choral", "Rock & Indie", "Pop & Throwback")]
        tags = tags + ["Jazz & Blues"] if "Jazz & Blues" not in tags else tags
    if tab == "music":
        music_tags = [g for g in tags if g not in non_music and g != "Live Music"]
        if music_tags:
            # "Live Music" only says it's a band when nothing better is known; a DJ called "DJ Cinéma" isn't a film,
            # a market held at a club isn't music. (Holiday Season stays: Christmas and New Year shows.)
            tags = [g for g in tags if g not in ("Live Music", "Film", "Markets & Fairs")]
    order = [g for g, _ in GENRES]
    e.genres = sorted(set(tags), key=order.index)


# Events are split into "Music and Dance" and "Other Events".
# Whole sources that are never music:
OTHER_SOURCES = {"MTL Cookbook Club", "Soft Studio Days", "Le Marché Vintage", "5 à Set", "Favourite Library",
                 "MBAM", "MBAM La Buvette", "McCord Museum", "McCord After Hours",
                 "Holiday markets", "Ateliers & Saveurs"}
# Run by the City of Montreal (montreal.ca): their own tab
FREE_TO_BARS = {"Bootlegger", "Bistro à Jojo"}   # their free nights go in Bar Nights; paid ones stay in Music & Dance
CITY_SOURCES = {"Ville de Montréal", "Maisons de la culture", "Bibliothèques de Montréal", "Other city venues",
                "Marchés publics"}   # the public markets (Jean-Talon, Atwater, Maisonneuve…)
# Recurring bar nights get their own tab:
BAR_SOURCES = {"Trivia Nights", "Comedy Open Mics", "Karaoke Nights", "Laugh It Off", "Added by hand", "TriviaNearMe"}
# Non-music nights at music venues (comedy, talks, quizzes...):
OTHER_TITLES = re.compile(r"comed|stand-?up|humou?r|\broast\b|podcast|\bquiz\b|trivia|\blecture\b|"
                          r"book launch|lancement de livre|open mic comedy|conf[ée]rence", re.I)
# Venue placeholders that aren't events at all:
SKIP_TITLES = re.compile(r"^\W*(ferm[ée]s?\s*//\s*closed|[ée]v[ée]nement priv[ée]\s*/\s*private event)\W*$", re.I)


def event_tab(e: Ev) -> str:
    if e.kind == "Oyster deal":
        return "oysters"
    if e.source in FREE_TO_BARS and e.free:   # free bar music (Jojo's 5 pm sets, Bootlegger's DJs) sits with the bar nights
        return "bars"
    if e.source in CITY_SOURCES:
        return "city"
    if e.source in BAR_SOURCES:
        return "bars"
    return "other" if e.source in OTHER_SOURCES or OTHER_TITLES.search(e.title) or is_comedy(e) else "music"


def source_tab(name: str) -> str:
    if name == "Oyster deals":
        return "oysters"
    if name in CITY_SOURCES:
        return "city"
    return "bars" if name in BAR_SOURCES else "other" if name in OTHER_SOURCES else "music"


# tab id -> (title, feed file); order = order on the page
TABS = {
    "music": ("Montreal Music", "music.ics"),
    "other": ("Montreal Social", "other.ics"),
    "bars": ("Montreal Bar", "bars.ics"),
    "oysters": ("Montreal Oyster Deals", "oysters.ics"),
    "museums": ("Montreal Exhibition", "museums.ics"),
    "classical": ("Montreal Traditional", "classical.ics"),
    "city": ("Montreal City", "city.ics"),
    "movies": ("Montreal Film", "movies.ics"),
}
# (sources, function choosing each event's tab, tab to file the source's status under)
SOURCE_GROUPS = [
    (EVENT_SOURCES, event_tab, source_tab),
    ({"Musées Montréal": musees_montreal, "DARE-DARE": dare_dare, "Occurrence": occurrence,
      "Fais-moi l'art": fais_moi_lart, "FOIL": foil,
      "Espace Transmission (site)": espace_transmission_site},
     lambda e: "museums", lambda name: "museums"),
    # Montreal Art Center: its film screenings go to Movies, exhibitions and events to Museums & Galleries
    ({"Montreal Art Center": montreal_art_center}, lambda e: "movies" if e.kind == "Screening" else "museums",
     lambda name: "museums"),
    # concert halls and the opera: their own tab, with kinds of concert instead of genres
    ({"Opéra de Montréal": opera_de_montreal, "Salle Bourgie": salle_bourgie, "McGill Schulich": mcgill_schulich,
      "Place des Arts": place_des_arts}, lambda e: "classical", lambda name: "classical"),
    ({"Cinéma Cinéma": cinema_cinema, "Cinéma Moderne": cinema_moderne}, lambda e: "movies", lambda name: "movies"),
]
# calendar.ics (the original feed) keeps carrying both event tabs, so existing subscriptions don't change
COMBINED = ("Montreal Events", "calendar.ics", {"music", "other"})


# ------------------------------------------------------------------- output
def short_about(text: str, limit: int = 420) -> str:
    """The first few sentences of a description: links, ***, hashtags and ticket boilerplate out, paragraphs kept."""
    import html
    text = html.unescape(html.unescape(text or ""))   # some pages escape twice ("&amp;nbsp;")
    text = re.sub(r"https?://\S+|www\.\S+|#\w+|\*{2,}|_{3,}|={3,}", " ", text)
    paras = [re.sub(r"[ \t\xa0]+", " ", x).strip() for x in re.split(r"\n", text)]
    paras = [x for x in paras if len(x) > 2 and not re.match(r"(?i)(billets|tickets|info|rsvp|line-?up)\s*[:|]?\s*$", x)]
    out = ""
    for x in paras:
        if len(out) + len(x) + 2 <= limit:
            out += ("\n\n" if out else "") + x
            continue
        if not out:   # one long paragraph: cut at a sentence (or word) boundary
            cut = x[:limit]
            stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            out = cut[:stop + 1] if stop > limit * .5 else cut.rsplit(" ", 1)[0] + "…"
        elif not out.endswith((".", "!", "?", "…")):
            out += "…"
        break
    return out.strip()


def to_json(e: Ev, tab: str) -> dict:
    d = asdict(e)
    d["start"] = e.start.isoformat()
    d["end"] = e.end.isoformat() if e.end else None
    d["all_day"] = e.all_day
    d["tab"] = tab
    d["id"] = stable_uid(e)
    blurb = re.sub(r"\s*\|?\s*BILLETS\s*\|\s*TICKETS\s*$", "", d.pop("blurb", "") or "").strip()
    about = short_about(d.pop("desc", "") or blurb)
    if about:   # a short "about" for the detail panel (not the whole description)
        d["about"] = about
    d["added"] = FIRST_SEEN.get(d["id"], "")
    if any(p.lower() in e.title.lower() for p in EDITOR_PICKS):
        d["pick"] = True
    return d


# Hand-picked highlights: any event whose title contains one of these gets an "Editor's pick" flag
# and is favoured for the "Tonight" cards. Example: "Carl Cox".
EDITOR_PICKS: list[str] = []

# id -> date the event was first seen (powers "New" badges and Tonight picks); kept between runs
FIRST_SEEN_FILE = OUT / "first_seen.json"
FIRST_SEEN: dict[str, str] = {}


def update_first_seen(events: list[Ev]) -> None:
    global FIRST_SEEN
    try:
        old = json.loads(FIRST_SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None   # first run: treat everything as already known, so nothing is flagged "new"
    today = datetime.now(TZ).date().isoformat()
    ids = {stable_uid(e) for e in events}
    if old is None:
        FIRST_SEEN = {i: "" for i in ids}
    else:
        FIRST_SEEN = {i: old.get(i, today) for i in ids}
    FIRST_SEEN_FILE.write_text(json.dumps(FIRST_SEEN, indent=0), encoding="utf-8")


def ics_entries(e: Ev, today: date):
    """(summary, start, end) for the feed. A months-long exhibition would cover the whole calendar,
    so it becomes two one-day entries instead: its opening day (if still ahead) and its last day."""
    if e.kind == "exhibition":
        if e.start >= today:
            yield f"Opens: {e.title} @ {e.location}", e.start, e.start + timedelta(days=1)
        if e.end and e.end != e.start:
            yield f"Last day: {e.title} @ {e.location}", e.end, e.end + timedelta(days=1)
        return
    if e.end and type(e.end) is type(e.start) and e.end > e.start:
        end = e.end
    else:
        end = e.start + (timedelta(days=1) if e.all_day else timedelta(hours=3))
    where = e.location if e.source == "Musées Montréal" and e.location else e.source
    yield f"{'FREE · ' if e.free else ''}{e.title} @ {where}", e.start, end


def build_ics(events: list[Ev], name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Montreal Event Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "America/Montreal")
    cal.add("refresh-interval;value=duration", "PT6H")
    now = datetime.now(timezone.utc)
    today = datetime.now(TZ).date()
    for e in events:
        for n, (summary, start, end) in enumerate(ics_entries(e, today)):
            v = ICalEvent()
            v.add("uid", f"{stable_uid(e)}{'-' + str(n) if n else ''}@mtl-events")
            v.add("dtstamp", now)
            v.add("summary", summary)
            v.add("dtstart", start)
            v.add("dtend", end)
            if e.location:
                v.add("location", e.location)
            if e.url:
                v.add("url", e.url)
            cost = "Free" if e.free else e.price
            v.add("description", "\n".join(x for x in (e.source, cost, e.url) if x))
            cal.add_component(v)
    return cal.to_ical()


def stable_uid(e: Ev) -> str:
    import hashlib
    key = e.url or f"{e.title}|{e.location}"   # link-less events (bar nights) are told apart by title and place
    return hashlib.sha1(f"{e.source}|{key}|{e.start.isoformat()}".encode()).hexdigest()[:20]


def last_moment(e: Ev) -> datetime:
    """When an event stops being worth listing: its start (or, for exhibitions, the end of its last day)."""
    d = e.end if e.kind == "exhibition" and e.end else e.start
    return d if isinstance(d, datetime) else datetime.combine(d, time(23, 59), TZ)


# ------------------------------------------------------------------ images & descriptions
IMAGE_CACHE = OUT / "image_cache.json"   # event page URL -> {"image": share image, "about": description}; kept between runs
NO_IMAGE_LOOKUP = ("mcgill.ca",   # its event pages sit behind a bot check when asked for many at once
                   "ra.co", "linktr.ee", "zeffy.com", "facebook.com", "tixr.com", "macm.org", "instagram.com")


def plain_text(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


FILLER_WORDS = set("""montreal montréal qc quebec québec canada tickets billets buy get event events show the a and at in
of on de la le les du des et à au en pm am est edt monday tuesday wednesday thursday friday saturday sunday lundi mardi
mercredi jeudi vendredi samedi dimanche january february march april may june july august september october november
december janvier février mars avril mai juin juillet août septembre octobre novembre décembre""".split())


def says_more(about: str, e: Ev) -> bool:
    """False for descriptions that only repeat the title, venue and date ("KILLED, OBELISK · Thursday, Nov 26 · Turbo Haüs")."""
    known = set(plain_text(f"{e.title} {e.source} {e.location}").split()) | FILLER_WORDS
    rest = [w for w in plain_text(about).split() if w not in known and not w.isdigit() and len(w) > 1]
    return len(rest) >= 6


def fill_page_info(events: list[Ev], budget: int = 250) -> None:
    """For events missing an image or a description, use the share image (og:image) and description
    (og:description) of the event's own page. Pages are fetched once and remembered in docs/image_cache.json;
    at most `budget` new pages per run. Listing pages shared by several different events are skipped (their
    image is the venue's logo), and a description that's the same on several of a site's pages (the site's
    own tagline) is dropped."""
    from collections import Counter, defaultdict
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urljoin, urlparse
    try:
        cache = json.loads(IMAGE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cache = {u: (v if isinstance(v, dict) else {"image": v}) for u, v in cache.items()}   # old format: url -> image
    titles = defaultdict(set)
    for e in events:
        if e.url:
            titles[e.url].add(e.title.lower())
    wanted = [e for e in events if e.url and len(titles[e.url]) <= 3 and not any(d in e.url for d in NO_IMAGE_LOOKUP)
              and e.kind != "Oyster deal"   # their links are guides and promo pages: the picture isn't the place's
              and (not e.image or not (e.desc or e.blurb))]
    todo = list(dict.fromkeys(e.url for e in wanted if "about" not in cache.get(e.url, {})))[:budget]

    def fetch(url):
        try:
            page = BeautifulSoup(get(url).content, "html.parser")   # bytes: let the page's own charset decide
        except Exception:
            return url, None   # try again next run

        def meta(*keys):
            for k in keys:
                m = page.find("meta", property=k) or page.find("meta", attrs={"name": k})
                if m and m.get("content", "").strip():
                    return m["content"].strip()
            return ""
        img = meta("og:image", "twitter:image")
        return url, {"image": urljoin(url, img) if img else "",
                     "about": meta("og:description", "twitter:description", "description")}

    with ThreadPoolExecutor(8) as pool:
        for url, info in pool.map(fetch, todo):
            if info is not None:
                cache[url] = info
    # a site's tagline shows up as the description of many of its pages: it's not about the event
    per_site = Counter((urlparse(u).netloc, v.get("about")) for u, v in cache.items() if v.get("about"))
    for e in wanted:
        c = cache.get(e.url) or {}
        if not e.image and c.get("image"):
            e.image = c["image"]
        a = c.get("about") or ""
        if not (e.desc or e.blurb) and len(a) > 40 and per_site[(urlparse(e.url).netloc, a)] < 3 and says_more(a, e):
            e.desc = a
    live = {e.url for e in events}
    IMAGE_CACHE.write_text(json.dumps({u: v for u, v in cache.items() if u in live}, ensure_ascii=False, indent=0),
                           encoding="utf-8")


IMG_DIR = OUT / "img"                  # our own small copies of event images (a venue's server can't break the page)
IMG_FAILED = IMG_DIR / "failed.json"   # image URL -> date it last failed to download; retried after 3 days
IMG_WIDTH = 480


def localize_images(events: list[Ev], budget: int = 900) -> None:
    """Download each event image once, shrink it to IMG_WIDTH px wide WebP in docs/img/ and point the event at
    that copy. Images that can't be fetched keep their original address (the page shows a fallback if those
    fail too). Copies no event uses any more are deleted."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from PIL import Image
    IMG_DIR.mkdir(exist_ok=True)
    try:
        failed = json.loads(IMG_FAILED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        failed = {}
    today = datetime.now(TZ).date()

    def name(u):
        return hashlib.sha1(u.encode()).hexdigest()[:16] + ".webp"
    urls = list(dict.fromkeys(e.image for e in events if e.image.startswith("http")))
    todo = [u for u in urls if not (IMG_DIR / name(u)).exists()
            and not (u in failed and (today - date.fromisoformat(failed[u])).days < 3)][:budget]

    def get(u):
        try:
            r = SESSION.get(u, timeout=20)
            r.raise_for_status()
            im = Image.open(BytesIO(r.content))
            im.seek(0)   # first frame of a GIF
            im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA", "PA") else im.convert("RGB")
            if im.mode == "RGBA":   # transparent logos sit on black, like the page
                bg = Image.new("RGB", im.size, (11, 11, 11))
                bg.paste(im, mask=im.split()[3])
                im = bg
            if im.width > IMG_WIDTH:
                im = im.resize((IMG_WIDTH, max(1, round(im.height * IMG_WIDTH / im.width))), Image.LANCZOS)
            im.save(IMG_DIR / name(u), "WEBP", quality=72, method=5)
            return u, True
        except Exception:
            return u, False

    with ThreadPoolExecutor(8) as pool:
        for u, ok in pool.map(get, todo):
            if ok:
                failed.pop(u, None)
            else:
                failed[u] = today.isoformat()
    for e in events:
        if e.image.startswith("http") and (IMG_DIR / name(e.image)).exists():
            e.image = "img/" + name(e.image)
    used = {e.image[4:] for e in events if e.image.startswith("img/")}
    for f in IMG_DIR.glob("*.webp"):
        if f.name not in used:
            f.unlink()
    live = set(urls)
    IMG_FAILED.write_text(json.dumps({u: d for u, d in failed.items() if u in live}, indent=0), encoding="utf-8")
    print(f"  images: {len(used)} local copies, {len(todo)} tried this run, {len(failed)} failing")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    cutoff = datetime.combine(datetime.now(TZ).date(), time(0), TZ)  # keep everything from today on

    def sort_key(e):
        return e.start if not e.all_day else datetime.combine(e.start, time(0), TZ)

    by_tab: dict[str, list[Ev]] = {t: [] for t in TABS}
    status = {}
    previous = previous_listings()
    for sources, pick_tab, home_tab in SOURCE_GROUPS:
        for name, fn in sources.items():
            try:
                evs = [e for e in fn() if last_moment(e) >= cutoff and sane_date(e)]
                evs = clean_events(evs)   # passes out, venue/date out of titles, same-slot repeats merged
                for e in evs:   # no price data, but the title says it's free
                    if e.free is None and e.kind != "exhibition" and FREE_TEXT.search(e.title):
                        e.free, e.price_lo = True, 0.0
                for e in evs:
                    tab = pick_tab(e)
                    tag_genres(e, tab)
                    by_tab[tab].append(e)
                status[name] = {"ok": True, "count": len(evs), "tab": home_tab(name)}
                print(f"  ok   {home_tab(name):8} {name:20} {len(evs):3} events")
            except Exception as ex:  # one broken site must not break the whole feed
                status[name] = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:300], "tab": home_tab(name)}
                print(f"  FAIL {home_tab(name):8} {name:20} {ex}")
                traceback.print_exc(limit=2)
                # couldn't check: keep what it listed last time (still upcoming) until a good read says otherwise
                kept = [(tab, e) for tab, e in previous.get(name, []) if tab in by_tab and last_moment(e) >= cutoff]
                for tab, e in kept:
                    by_tab[tab].append(e)
                if kept:
                    status[name]["kept"] = len(kept)
                    print(f"       kept {len(kept)} earlier listings")

    by_tab["bars"] = dedupe_bar_nights(by_tab["bars"])
    by_tab["classical"] = dedupe_concerts(by_tab["classical"])
    by_tab["music"] = merge_repeats(sorted(by_tab["music"], key=sort_key))
    tidy_rooms([e for evs in by_tab.values() for e in evs])
    fill_page_info([e for evs in by_tab.values() for e in evs])
    localize_images([e for evs in by_tab.values() for e in evs])
    update_first_seen([e for evs in by_tab.values() for e in evs])
    payload_events, tabs = [], {}
    for tab, (title, feed) in TABS.items():
        by_tab[tab].sort(key=sort_key)
        (OUT / feed).write_bytes(build_ics(by_tab[tab], title))
        payload_events += [to_json(e, tab) for e in by_tab[tab]]
        tabs[tab] = {"title": title, "feed": feed, "count": len(by_tab[tab])}
        print(f"  {tab:8} {len(by_tab[tab]):4}")
    title, feed, members = COMBINED
    (OUT / feed).write_bytes(build_ics(sorted((e for t in members for e in by_tab[t]), key=sort_key), title))

    alerts = check_health(status)
    payload = {"generated": datetime.now(TZ).isoformat(timespec="minutes"),
               "tabs": tabs, "sources": status, "genres": list(dict.fromkeys([g for g, _ in GENRES] + [g for g, _ in CLASSICAL_TYPES])), "events": payload_events}
    (OUT / "events.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(payload_events)} events written to {OUT}")
    ALERTS.unlink(missing_ok=True)
    if alerts:
        ALERTS.write_text("\n".join(alerts) + "\n", encoding="utf-8")   # the workflow turns this into a GitHub issue
        for a in alerts:
            print(f"::warning::{a}")
    return 0 if any(s["ok"] for s in status.values()) else 1


ALERTS = Path(__file__).parent / "alerts.md"


def previous_listings() -> dict[str, list[tuple[str, Ev]]]:
    """The last run's events (docs/events.json), by source, as (tab, Ev). A source that fails to load keeps these,
    so a one-off error (a timeout, "too many requests") doesn't empty it; only a good read removes a listing."""
    try:
        old = json.loads((OUT / "events.json").read_text(encoding="utf-8")).get("events", [])
    except (OSError, ValueError):
        return {}

    def when(v, all_day):
        if not v:
            return None
        return date.fromisoformat(v[:10]) if all_day else datetime.fromisoformat(v)
    out: dict[str, list[tuple[str, Ev]]] = {}
    for d in old:
        try:
            e = Ev(d["source"], d["title"], when(d["start"], d.get("all_day")), d.get("url", ""),
                   when(d.get("end"), d.get("all_day")), d.get("location", ""), d.get("kind", ""), d.get("free"),
                   d.get("price", ""), d.get("price_lo"), list(d.get("genres") or []), "", d.get("image", ""),
                   d.get("about", ""))
        except (KeyError, ValueError, TypeError):
            continue
        out.setdefault(d["source"], []).append((d.get("tab", ""), e))
    return out


def check_health(status: dict) -> list[str]:
    """Compare with the previous run (the events.json about to be replaced) and report, once each:
    - a source failing two runs in a row (one-off timeouts don't count)
    - a source that usually has events (3+) returning none two runs in a row.
    Streaks are kept in each source's status so the next run can continue them."""
    try:
        old = json.loads((OUT / "events.json").read_text(encoding="utf-8")).get("sources", {})
    except (OSError, ValueError):
        old = {}
    alerts = []
    for name, s in status.items():
        o = old.get(name, {})
        last = s.get("count") or o.get("last_count") or o.get("count") or 0
        s["last_count"] = last
        if not s["ok"]:
            s["fail_streak"] = o.get("fail_streak", 0) + 1
            if s["fail_streak"] == 2:
                alerts.append(f"- **{name}** has failed two updates in a row: `{s['error']}`")
        else:
            s["zero_streak"] = o.get("zero_streak", 0) + 1 if s["count"] == 0 else 0
            if s["zero_streak"] == 2 and last >= 3:
                alerts.append(f"- **{name}** found no events two updates in a row (it had {last} before). "
                              "Its site may have changed layout.")
            # empty for 2 days (8 updates) and not known to be quiet: say so once, until it has events again
            s["zero_alerted"] = bool(s["count"] == 0 and (o.get("zero_alerted") or s["zero_streak"] >= 8))
            if s["zero_streak"] >= 8 and not o.get("zero_alerted") and name not in QUIET_OK:
                alerts.append(f"- **{name}** has found no events for {s['zero_streak']} updates in a row. "
                              "If it's simply quiet, add it to `QUIET_OK` in scrape.py; otherwise its page may have changed.")
    return alerts


# Sources checked by hand and known to sit at zero for a while (checked 2026-11-25):
QUIET_OK = {
    "McCord After Hours",        # page says "Stay tuned!" until the next date is set
    "Boiler Room",               # no Montreal show announced (last: March 2025)
    "Eastern Bloc",              # RA venue; last event May 2025, announces mostly on Instagram
    "L'Escogriffe",              # Live Nation lists only the shows it sells there (a few a season)
    "L'Escogriffe (RA)",         # club nights on RA only occasionally (last: Aug 2026)
    "Le Marché Vintage",         # Eventbrite organizer between markets (last: Nov 21)
    "Espace Transmission",       # RA venue; last event July 2026
    "LATEX.",                    # a party every month or two; nothing listed between editions
    "Barbossa",                  # RA venue; posts a night or two at a time, announces mostly on Facebook
    "Espace Transmission (site)",  # site is an archive (newest entry Nov 2024); the reader parses it fine
}


if __name__ == "__main__":
    raise SystemExit(main())

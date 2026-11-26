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


RA_FIELDS = "id title startTime endTime contentUrl cost venue { name } genres { name }"


def ra_events(kind: str, ra_id: str, source: str) -> list[Ev]:
    """kind = 'venue' or 'promoter'."""
    d = ra_query(f'{{ {kind}(id: "{ra_id}") {{ events(type: LATEST, limit: 100) {{ {RA_FIELDS} }} }} }}')
    out = []
    for e in d[kind]["events"]:
        ev = Ev(source, e["title"], iso(e["startTime"]), "https://ra.co" + e["contentUrl"],
                iso(e["endTime"]) if e.get("endTime") else None, (e.get("venue") or {}).get("name", ""))
        ev.genres = genres_in(" ".join(g["name"] for g in e.get("genres") or []))   # RA's own tags, when set
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
        loc = next((x for x in spans if x != when and "," in x), "")
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
        ta = e.get("ticket_availability") or {}
        lo = (ta.get("minimum_ticket_price") or {}).get("major_value")
        hi = (ta.get("maximum_ticket_price") or {}).get("major_value")
        out.append(priced(ev, True if ta.get("is_free") else None,
                          float(lo) if lo else None, float(hi) if hi else None))
    return out


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
                                                 "select[startsAt]": "true", "select[priceText]": "true"}).json()
        for doc in api.get("docs", []):
            if doc.get("slug") and doc.get("priceText"):
                slug_price[doc["slug"]] = doc["priceText"]
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


def le_belmont() -> list[Ev]:
    html = get("https://www.lebelmont.ca/evenements").text
    out, seen = [], set()
    pat = re.compile(r'"scheduling":\{"config":\{[^{}]*?"startDate":"([^"]+)","endDate":"([^"]+)".*?'
                     r'"title":"((?:[^"\\]|\\.)*)"', re.S)
    for m in pat.finditer(html):
        title = json.loads(f'"{m.group(3)}"')
        key = (title, m.group(1))
        if key in seen:
            continue
        seen.add(key)
        out.append(Ev("Le Belmont", title, iso(m.group(1)), "https://www.lebelmont.ca/evenements",
                      iso(m.group(2)), "Le Belmont"))
    return out


def _luma_ev(e: dict, source: str, ticket_info: dict | None = None) -> Ev:
    geo = e.get("geo_address_info") or {}
    loc = geo.get("full_address") or geo.get("address") or geo.get("city") or ""
    ev = Ev(source, e["name"], iso(e["start_at"]), "https://luma.com/" + e["url"],
            iso(e["end_at"]) if e.get("end_at") else None, loc)
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
            out.append(priced(ev, *universe_rates(d)))
    return out


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
        out.append(Ev("New City Gas", clean(d.get("name") or ""), iso(d["startDate"]), url,
                      iso(d["endDate"]) if d.get("endDate") else None, "New City Gas, 950 rue Ottawa"))
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


# Weekly nights known first-hand, with no website to read.
# (filter chip, title, weekday 0=Mon, start time, location, free)
WEEKLY_BY_HAND = [
    ("Trivia Nights", "Trivia Night at Honey Martin", 1, time(21, 30),
     "Honey Martin, 5916 Sherbrooke St W Montreal QC", True),
]


def by_hand(weeks: int = 4) -> list[Ev]:
    today = datetime.now(TZ).date()
    out = []
    for source, title, weekday, t, loc, free in WEEKLY_BY_HAND:
        first = today + timedelta(days=(weekday - today.weekday()) % 7)
        for w in range(weeks):
            out.append(priced(Ev(source, title, local(first + timedelta(weeks=w), t), "", location=loc), free))
    return out


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
                              _en_date(m.group(2)), x.get("museum_name") or "", "exhibition"))
            elif m := re.match(r"starting on (.+)$", fd):
                ev = Ev("Musées Montréal", x["name"], _en_date(m.group(1)), x["url"],
                        location=x.get("museum_name") or "", kind=x.get("type_text") or "Event")
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
            out.append(Ev(cinema, title, start, film_url.get(fid, f"{base}/fr/horaire?date={ds}"), end, cinema))
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
    "L'Escogriffe": lambda: ra_events("venue", "124247", "L'Escogriffe"),   # its own site is a Facebook page
    "Ausgang Plaza": lambda: ra_events("venue", "158331", "Ausgang Plaza"),
    "Quai des Brumes": quai_des_brumes,
    "Sans Soleil": lambda: ra_events("venue", "203475", "Sans Soleil"),   # basement bar; site is Instagram only
    "Blue Room": lambda: ra_events("venue", "290582", "Blue Room"),       # 3956 Saint-Laurent, 3rd floor; Instagram only
    # party collective: its Linktree ticket links carry the date ("Heures ouvrables 007 (6 mars 2026)")
    "Heures ouvrables": lambda: linktree_dated("https://linktr.ee/heuresouvrables", "Heures ouvrables"),
    # multi-use space (5435 av. des Érables); raves and parties go on RA venue 235410
    "Espace Transmission": lambda: ra_events("venue", "235410", "Espace Transmission"),
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
    ("Folk & Country", r"\bfolk|country|bluegrass|honky|string ?band|americana|banjo|sing ?along|yiddish|greek"),
    ("Punk & Hardcore", r"punk|hardcore|grunge|crust|\boi!"),
    ("Metal", r"metal|\bdoom\b|sludge|grindcore|thrash"),
    ("Rock & Indie", r"\brock\b|indie|shoegaze|psych|surf rock|garage rock"),
    ("Experimental", r"experiment|\bnoise\b|drone|ambient|modular|improv|avant|electroacoust|[ée]lectroacoustique|"
                     r"snare drum|musique actuelle|free jazz|\bconcr[eè]te?\b|contemporary music|sound art"),
    ("Pop & Throwback", r"\bpop\b|\b(?:19)?[6-9]0'?s\b|\b2000s?\b|\b2010s\b|tribute|hommage|cover set|throwback|dance party"),
    ("Electronic", r"electro|[ée]lectro|\bdj\b|dj set|\bb2b\b|\brave\b|all night long|club\b|\bbpm\b"),
    ("Dance", r"\bdance\b|\bdanse\b|ballroom|vogue|t-dance"),
    ("Comedy", r"comed|com[ée]die|stand-?up|humou?r|\broast\b"),
    ("Listening Party", r"listening party|s[ée]ance d'[ée]coute"),
    ("Talks & Quizzes", r"podcast|\bquiz\b|trivia|\blecture\b|\breading\b|conf[ée]rence|\bpanel\b|in conversation|artist talk"),
    ("Art & Workshops", r"workshop|atelier|drawing|dessin|ceramic|c[ée]ramique|vision board|craft|figure|collage|knit|tricot"),
    ("Food & Drink", r"\bcookies?\b|\bcook|\bcuisine\b|\bfood\b|\bbouffe\b|\bwines?\b|\bvins?\b|\btasting\b|d[ée]gustation|potluck|\bbrunch\b"),
]
_GENRE_RX = [(g, re.compile(p, re.I)) for g, p in GENRES]
ELECTRONIC_KIDS = {"Techno", "House", "Disco", "Bass & DnB"}
# When a title says nothing about the music: what the venue mostly does.
VENUE_GENRES = {
    "Stereo": ["Electronic", "House"], "StereoBar": ["Electronic", "House"], "Sans Soleil": ["Electronic"],
    "Blue Room": ["Electronic"], "Heures ouvrables": ["Dance", "Electronic"], "New City Gas": ["Electronic"], "Système": ["Electronic"],
    "Salon Daomé": ["Electronic", "House"], "Bar Datcha": ["Electronic"], "Vino Disco": ["Disco", "House", "Electronic"],
    "Ausgang Plaza": ["House", "Electronic"], "Le Red Room": ["Electronic", "Techno"], "SAT": ["Electronic"],
    "Boiler Room": ["Electronic"], "Eastern Bloc": ["Electronic", "Experimental"], "Newspeak": ["Electronic"],
    "Turbo Haüs": ["Punk & Hardcore", "Rock & Indie"], "Le Belmont": ["Pop & Throwback", "Dance"],
    # (no default for the live-band rooms — Casa del Popolo, Fairmount, Le Ritz, Quai des Brumes, L'Escogriffe:
    #  they book everything from folk metal to blues to improv, so an untagged show there stays untagged)
    "Laugh It Off": ["Comedy"], "Comedy Open Mics": ["Comedy"],
    "Soft Studio Days": ["Art & Workshops"], "MTL Cookbook Club": ["Food & Drink"], "5 à Set": ["Listening Party"],
    "Le Marché Vintage": ["Art & Workshops"],
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


def tag_genres(e: Ev, tab: str) -> None:
    if tab in ("museums", "movies"):
        return
    tags = list(dict.fromkeys(e.genres + genres_in(e.title) + genres_in(e.blurb)))
    if is_comedy(e):
        tags = ["Comedy"]
    for rx, gs in PRESENTER_GENRES:
        if rx.search(e.blurb) and not [g for g in tags if g not in ("Dance", "Electronic")]:
            tags += [g for g in gs if g not in tags]
    non_music = ("Comedy", "Dance", "Listening Party", "Talks & Quizzes", "Art & Workshops", "Food & Drink")
    music = [g for g in tags if g not in non_music]
    # venue defaults: music venues only in Music & Dance; other sources (comedy, workshops) everywhere,
    # and never on a night the title already marks as comedy / a talk / a quiz
    defaults = VENUE_GENRES.get(e.source, [])
    music_defaults = [g for g in defaults if g not in non_music]
    if not music and not (set(tags) & {"Comedy", "Talks & Quizzes"}):
        tags += [g for g in defaults if (tab == "music" or g not in music_defaults) and g not in tags]
    elif not tags:
        tags += [g for g in defaults if g not in music_defaults]
    order = [g for g, _ in GENRES]
    e.genres = sorted(set(tags), key=order.index)


# Events are split into "Music and Dance" and "Other Events".
# Whole sources that are never music:
OTHER_SOURCES = {"MTL Cookbook Club", "Soft Studio Days", "Le Marché Vintage", "5 à Set", "Favourite Library"}
# Recurring bar nights get their own tab:
BAR_SOURCES = {"Trivia Nights", "Comedy Open Mics", "Karaoke Nights", "Laugh It Off", "Added by hand"}
# Non-music nights at music venues (comedy, talks, quizzes...):
OTHER_TITLES = re.compile(r"comed|stand-?up|humou?r|\broast\b|podcast|\bquiz\b|trivia|\blecture\b|"
                          r"book launch|lancement de livre|open mic comedy|conf[ée]rence", re.I)
# Venue placeholders that aren't events at all:
SKIP_TITLES = re.compile(r"^\W*(ferm[ée]s?\s*//\s*closed|[ée]v[ée]nement priv[ée]\s*/\s*private event)\W*$", re.I)


def event_tab(e: Ev) -> str:
    if e.source in BAR_SOURCES:
        return "bars"
    return "other" if e.source in OTHER_SOURCES or OTHER_TITLES.search(e.title) or is_comedy(e) else "music"


def source_tab(name: str) -> str:
    return "bars" if name in BAR_SOURCES else "other" if name in OTHER_SOURCES else "music"


# tab id -> (title, feed file); order = order on the page
TABS = {
    "music": ("Montreal Music & Dance", "music.ics"),
    "other": ("Montreal Other Events", "other.ics"),
    "bars": ("Montreal Bar Nights", "bars.ics"),
    "museums": ("Montreal Museums & Galleries", "museums.ics"),
    "movies": ("Montreal Movies", "movies.ics"),
}
# (sources, function choosing each event's tab, tab to file the source's status under)
SOURCE_GROUPS = [
    (EVENT_SOURCES, event_tab, source_tab),
    ({"Musées Montréal": musees_montreal, "DARE-DARE": dare_dare, "Occurrence": occurrence,
      "Fais-moi l'art": fais_moi_lart, "FOIL": foil,
      "Espace Transmission (site)": espace_transmission_site}, lambda e: "museums", lambda name: "museums"),
    ({"Cinéma Cinéma": cinema_cinema, "Cinéma Moderne": cinema_moderne}, lambda e: "movies", lambda name: "movies"),
]
# calendar.ics (the original feed) keeps carrying both event tabs, so existing subscriptions don't change
COMBINED = ("Montreal Events", "calendar.ics", {"music", "other"})


# ------------------------------------------------------------------- output
def to_json(e: Ev, tab: str) -> dict:
    d = asdict(e)
    d["start"] = e.start.isoformat()
    d["end"] = e.end.isoformat() if e.end else None
    d["all_day"] = e.all_day
    d["tab"] = tab
    d["id"] = stable_uid(e)
    d.pop("blurb", None)   # only used for tagging
    return d


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


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    cutoff = datetime.combine(datetime.now(TZ).date(), time(0), TZ)  # keep everything from today on

    def sort_key(e):
        return e.start if not e.all_day else datetime.combine(e.start, time(0), TZ)

    by_tab: dict[str, list[Ev]] = {t: [] for t in TABS}
    status = {}
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
               "tabs": tabs, "sources": status, "genres": [g for g, _ in GENRES], "events": payload_events}
    (OUT / "events.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(payload_events)} events written to {OUT}")
    ALERTS.unlink(missing_ok=True)
    if alerts:
        ALERTS.write_text("\n".join(alerts) + "\n", encoding="utf-8")   # the workflow turns this into a GitHub issue
        for a in alerts:
            print(f"::warning::{a}")
    return 0 if any(s["ok"] for s in status.values()) else 1


ALERTS = Path(__file__).parent / "alerts.md"


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
    return alerts


if __name__ == "__main__":
    raise SystemExit(main())

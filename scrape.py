"""Scrape upcoming events from Montreal venues and build a calendar feed.

Run:  python scrape.py
Outputs (in ./docs, served by GitHub Pages):
  events.json   - all events, used by index.html
  calendar.ics  - subscribable iCalendar feed (all venues)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass, asdict
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
    end: datetime | date | None = None
    location: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


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


RA_FIELDS = "id title startTime endTime contentUrl venue { name }"


def ra_events(kind: str, ra_id: str, source: str) -> list[Ev]:
    """kind = 'venue' or 'promoter'."""
    d = ra_query(f'{{ {kind}(id: "{ra_id}") {{ events(type: LATEST, limit: 100) {{ {RA_FIELDS} }} }} }}')
    out = []
    for e in d[kind]["events"]:
        out.append(Ev(source, e["title"], iso(e["startTime"]), "https://ra.co" + e["contentUrl"],
                      iso(e["endTime"]) if e.get("endTime") else None,
                      (e.get("venue") or {}).get("name", "")))
    return out


# ------------------------------------------------------------------ sources
def sat() -> list[Ev]:
    q = """query($after: String) { events(first: 50, after: $after,
             where: {language: EN, futureItemsOnly: true}) {
        nodes { title link datePicker event { infocontent {
            startdate enddate dayhourlength category { slug } espace { ... on Espace { title } } } } }
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
            dates = n.get("datePicker") or [ic["startdate"]]
            for ds in dates:
                d = datetime.strptime(ds, "%d/%m/%Y").date()
                out.append(Ev("SAT", n["title"], local(d, t), n["link"], location=loc))
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
        out.append(Ev("Casa del Popolo", clean(heads[1].get_text()), local(d, t), base + more["href"],
                      location=clean(heads[2].get_text())))
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
        out.append(Ev(source, e.get("name") if isinstance(e.get("name"), str) else e.get("name", {}).get("text", ""),
                      st.astimezone(TZ), e["url"], en.astimezone(TZ) if en else None,
                      venue.get("name", "") if isinstance(venue, dict) else ""))
    return out


def turbo_haus() -> list[Ev]:
    base = "https://turbohaus.ca"
    today = date.today()
    months = [(today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1) for i in range(3)]
    # The month grid has dates (recurring shows already expanded) but often no times;
    # the CMS API has each show's start time, keyed by slug.
    slug_time: dict[str, time] = {}
    try:
        api = get(f"{base}/api/events", params={"limit": 5000, "depth": 0, "select[slug]": "true",
                                                 "select[startsAt]": "true"}).json()
        for doc in api.get("docs", []):
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
            out.append(Ev("Turbo Haüs", title, local(d, t), base + a["href"], location="Turbo Haüs"))
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


def _luma_ev(e: dict, source: str) -> Ev:
    geo = e.get("geo_address_info") or {}
    loc = geo.get("full_address") or geo.get("address") or geo.get("city") or ""
    return Ev(source, e["name"], iso(e["start_at"]), "https://luma.com/" + e["url"],
              iso(e["end_at"]) if e.get("end_at") else None, loc)


def luma_calendar(page_url: str, source: str) -> list[Ev]:
    """A Luma calendar page (luma.com/<calendar>): upcoming events from its embedded page data."""
    s = soup(page_url)
    data = json.loads(s.find("script", id="__NEXT_DATA__").string)
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if "start_at" in o and "name" in o and "url" in o and "api_id" in o:
                found[o["api_id"]] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return [_luma_ev(e, source) for e in found.values()]


def luma_user(user_api_id: str, source: str) -> list[Ev]:
    """Upcoming events hosted by a Luma user profile (luma.com/user/<name>)."""
    out, cursor = [], None
    while True:
        params = {"user_api_id": user_api_id, "period": "future", "pagination_limit": 50}
        if cursor:
            params["pagination_cursor"] = cursor
        d = get("https://api.lu.ma/user/profile/events-hosting", params=params).json()
        out += [_luma_ev(x["event"], source) for x in d.get("entries", [])]
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
        out.append(Ev("Laugh It Off", e["name"], st, e["url"], en, e.get("venue") or ""))
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
            out.append(Ev("Newspeak", lst.get("title") or slug, iso(e["start_time"]),
                          f"https://www.universe.com/events/{slug}",
                          iso(e["end_time"]) if e.get("end_time") else None, lst.get("venue_name") or "Newspeak"))
    return out


SOURCES = {
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
}


# ------------------------------------------------------------------- output
def to_json(e: Ev) -> dict:
    d = asdict(e)
    d["start"] = e.start.isoformat()
    d["end"] = e.end.isoformat() if e.end else None
    d["all_day"] = e.all_day
    return d


def build_ics(events: list[Ev]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Montreal Event Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Montreal Events")
    cal.add("x-wr-timezone", "America/Montreal")
    cal.add("refresh-interval;value=duration", "PT6H")
    now = datetime.now(timezone.utc)
    for e in events:
        v = ICalEvent()
        v.add("uid", f"{stable_uid(e)}@mtl-events")
        v.add("dtstamp", now)
        v.add("summary", f"{e.title} @ {e.source}")
        v.add("dtstart", e.start)
        if e.end and type(e.end) is type(e.start) and e.end > e.start:
            v.add("dtend", e.end)
        elif e.all_day:
            v.add("dtend", e.start + timedelta(days=1))
        else:
            v.add("dtend", e.start + timedelta(hours=3))
        if e.location:
            v.add("location", e.location)
        v.add("url", e.url)
        v.add("description", f"{e.source}\n{e.url}")
        cal.add_component(v)
    return cal.to_ical()


def stable_uid(e: Ev) -> str:
    import hashlib
    return hashlib.sha1(f"{e.source}|{e.url}|{e.start.isoformat()}".encode()).hexdigest()[:20]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    cutoff = datetime.combine(datetime.now(TZ).date(), time(0), TZ)  # keep everything from today on
    all_events, status = [], {}
    for name, fn in SOURCES.items():
        try:
            evs = fn()
            evs = [e for e in evs if (e.start if not e.all_day else datetime.combine(e.start, time(23, 59), TZ)) >= cutoff]
            all_events += evs
            status[name] = {"ok": True, "count": len(evs)}
            print(f"  ok   {name:20} {len(evs):3} events")
        except Exception as ex:  # one broken site must not break the whole feed
            status[name] = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:300]}
            print(f"  FAIL {name:20} {ex}")
            traceback.print_exc(limit=2)

    def sort_key(e):
        return e.start if not e.all_day else datetime.combine(e.start, time(0), TZ)
    all_events.sort(key=sort_key)

    payload = {"generated": datetime.now(TZ).isoformat(timespec="minutes"),
               "sources": status, "events": [to_json(e) for e in all_events]}
    (OUT / "events.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "calendar.ics").write_bytes(build_ics(all_events))
    print(f"\n{len(all_events)} events written to {OUT}")
    return 0 if any(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

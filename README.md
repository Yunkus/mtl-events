# Montreal Events calendar

Scrapes upcoming events from a list of Montreal venues every 6 hours and publishes:

- `docs/index.html` – a filterable web page
- one calendar feed per tab, to subscribe to in Google / Apple / Outlook Calendar:
  `music.ics` (Music & Dance), `other.ics` (Other Events), `bars.ics` (Bar Nights), `museums.ics`, `movies.ics`.
  `calendar.ics` is the original feed and still has Music & Dance and Other Events together.

On the page you can also tick individual events (tap showtimes for movies). The bar at the bottom then
downloads just those as a `.ics` file (opens in Apple Calendar / Outlook, or import into Google Calendar),
or gives one "Add to Google Calendar" link per event. Selections are saved in your browser only.

## Free events

Events are marked **FREE** when the source says so: a price of zero, a free flag (Eventbrite, Luma,
Laugh It Off, Universe), "GRATUIT // FREE" (Turbo Haüs), a museum page whose prices are all free or that says
"free of charge", or a title that says free / gratuit. Paid events show their price when the source has it.
Le Red Room, Le Belmont, Favourite Library and the cinemas don't publish prices in a readable form.

## Music & Dance vs Other Events

Each event in the venue list below goes to **Music & Dance** unless:

- its source is in `OTHER_SOURCES` in `scrape.py` (MTL Cookbook Club, Soft Studio Days,
  Le Marché Vintage, 5 à Set), or
- its title matches `OTHER_TITLES` (comedy, stand-up, roast, podcast, quiz, trivia, readings…),
  which catches the non-music nights at music venues.

"Closed" and "private event" placeholders are dropped.

## Sources (Music & Dance and Other Events tabs)

| Venue | How it's fetched |
|---|---|
| SAT | SAT's own events API (cms.sat.qc.ca) – "Concerts & DJs" category only |
| Favourite Library | homepage "Shows" section |
| Casa del Popolo (+ Sala Rossa, Sotterenea, P'tit Ours, Toscadura) | homepage event list |
| Le Red Room | /evenements page (dates only, no times) |
| Vino Disco | Resident Advisor promoter 133188 (what their site embeds) |
| Bar Datcha | Resident Advisor venue 94451 |
| 5 à Set | Eventbrite organizer page |
| Le Marché Vintage | Eventbrite organizer page (vintage pop-up markets) |
| Stereo | Resident Advisor venue 828 (Tixr blocks automated access) |
| Salon Daomé | Resident Advisor venue 153067 (ra.co/clubs/153067) |
| Turbo Haüs | /calendrier month pages, next 3 months |
| Le Belmont | /evenements (Wix events) |
| MTL Cookbook Club | Luma calendar page (luma.com/mtlcookbookclub) |
| Soft Studio Days | Luma host profile (what their /events page links to) |
| Newspeak | Universe ticket links on newspeakmtl.com, dates from Universe |
| Boiler Room | boilerroom.tv/city/montreal (page data). It lists past shows too; only upcoming ones appear. |
| Bar Le Ritz PDB | barleritzpdb.com events (Squarespace `?format=json`); name from each event's heading, price from its Universe ticket page |
| Système | Resident Advisor venue 201918 (what systememtl.com links to) |
| New City Gas | newcitygas.com/en/events, then each event page's schema.org data (times). No prices: the pages show a placeholder and Tixr blocks automated access. |
| Eastern Bloc | Resident Advisor venue 147287 (parties, Sight+Sound). It mostly announces on Instagram, which can't be read automatically. |

### Museums tab (`docs/museums.ics`)

| Source | How it's fetched |
|---|---|
| Musées Montréal | museesmontreal.org listing API. Dated events plus temporary/upcoming exhibitions; permanent exhibitions have no dates and are skipped. In the feed, each exhibition appears twice: on its opening day (if it hasn't opened yet) and on its last day. |

### Movies tab (`docs/movies.ics`)

| Source | How it's fetched |
|---|---|
| Cinéma Beaubien, Cinéma du Parc, Cinéma du Musée | cinemacinema.ca `/fr/horaire?date=…`, next 14 days |
| Cinéma Moderne | `/horaire` month calendar, this month and next |

The movies feed has every showtime (several hundred). Subscribe to it only if you want that in your calendar.

### Bar Nights tab (`docs/bars.ics`)

| Source | How it's fetched |
|---|---|
| Trivia Nights, Comedy Open Mics, Karaoke Nights | badslava.com Montreal listings (trivia, comedy open mics, karaoke). The site shows one week; nights its detail page marks "Weekly" are repeated for 4 weeks. No links, since these are bar nights. Free/price comes from the detail page's "Event Cost". |
| Laugh It Off | the site's own events feed (comedy shows in bars, via Eventbrite), next 60 days only |
| Added by hand | `WEEKLY_BY_HAND` in `scrape.py`: weekly nights with no website (Tuesday trivia at Honey Martin, 9:30 PM, free). Add a line there to add another. |

**Not included: Aire Commune.** Its site has no dated events, and its Tixr page blocks automated access.

If a site changes its layout, that one source fails and the others keep working. The web page shows the broken one crossed out.

## One-time setup (GitHub, free)

1. Create a new **public** repository on github.com (for example `mtl-events`). Tick "Add a README file".
2. In the repository, click **Add file → Upload files**. Drag in `scrape.py`, `requirements.txt`, `README.md`,
   and the `docs` and `.github` folders, then click **Commit changes**.
   (Don't upload the `.claude` folder. It only holds local preview settings.)
3. On GitHub, open **Settings → Pages**. Set *Source* to **Deploy from a branch**, branch `main`, folder `/docs`. Save.
4. Open **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and save.
5. Open **Actions → Update events → Run workflow** to run it once now.

Your page will be at `https://<you>.github.io/mtl-events/`. Its buttons subscribe your calendar to the feed.
Google Calendar re-checks subscribed feeds every 12–24 hours, so new events can take that long to appear.

## Run locally

```
pip install -r requirements.txt
python scrape.py
```

## Adding a venue

Write a function in `scrape.py` that returns a list of `Ev(...)` and add it to `EVENT_SOURCES`
(or to the museums/movies entry in `TABS`).
If the venue is on Resident Advisor, you only need one line: `lambda: ra_events("venue", "<id>", "<Name>")`.

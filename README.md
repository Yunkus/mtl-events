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

## Cleanup, genres and filters

Every source goes through a cleanup step (`clean_events` in `scrape.py`):

- festival passes / ticket bundles ("Pass du Festival", "Forfait billet") are dropped
- titles lose a trailing date ("… - December 2", "… on November 30") and a trailing "@ venue" when it names the venue
- Laugh It Off and SAT list some nights twice at the same time and place (a series entry plus the dated show); only one is kept
- a venue listing the same lineup twice on one day under different wording (e.g. one copy at 8:00 AM) keeps one:
  the copy with a price, else the one at a normal show time. Identical titles at different times are kept
  (separate showings).
- dates that can't be right are dropped (a show more than 18 months out, or ending before it starts).
  Long-running exhibitions keep their real opening date (the Planétarium's *Aurorae* has run since 2015).

Genres (`GENRES` in `scrape.py`) come from keywords in the title and, for Théâtre Fairmount and Bar Le Ritz, the
show description; Resident Advisor's own genre tags; who presents the show (`PRESENTER_GENRES`: Extensive Enterprise
→ Metal, Blue Skies Turn Black → Rock & Indie…); and, only for venues that book one kind of night, a venue default
(`VENUE_GENRES`: Stereo, New City Gas and the other clubs → Electronic, Turbo Haüs → Punk and Rock). Live-band rooms
(Casa del Popolo, Fairmount, Le Ritz, Quai des Brumes) get no default, so a show with no clue stays untagged rather
than guessed.

Stand-up shows billed under a name only are caught by the presenter (Outback, Juste pour rire…), comedy words in
the description, or `COMEDIANS` in `scrape.py` (add a name there if one lands in Music & Dance).

On the page: Tonight, This weekend, Early shows / Late night (10 pm+), Free / Under $20 (events with no
known price stay listed, marked "price unknown"), and a Genre row on Music & Dance, Other Events and By Date. Every count follows the other active filters
and the search box, so a combination with no results shows 0 before you tap it. Venues with nothing upcoming
get no button.

## Alerts

If a source fails two updates in a row, or a venue that usually has 3+ events finds none two updates in a
row, the update opens a GitHub issue (you get an email). Venues that are simply quiet (Boiler Room, Eastern
Bloc, L'Escogriffe, Le Marché Vintage) don't trigger it.

## Music & Dance vs Other Events

Each event in the venue list below goes to **Music & Dance** unless:

- its source is in `OTHER_SOURCES` in `scrape.py` (MTL Cookbook Club, Soft Studio Days, Favourite Library,
  Le Marché Vintage, 5 à Set), or
- its title matches `OTHER_TITLES` (comedy, stand-up, roast, podcast, quiz, trivia, readings, conferences…),
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
| Bar Le Ritz PDB, Théâtre Fairmount | each site's Squarespace events (`?format=json`); name from each event's heading, price range from its Universe ticket types |
| Système | Resident Advisor venue 201918 (what systememtl.com links to) |
| New City Gas | newcitygas.com/en/events, then each event page's schema.org data (times). No prices: the pages show a placeholder and Tixr blocks automated access. |
| Eastern Bloc | Resident Advisor venue 147287 (parties, Sight+Sound). It mostly announces on Instagram, which can't be read automatically. |
| L'Escogriffe | Resident Advisor venue 124247 (its own site is a Facebook page; lists shows on RA only occasionally) |
| Ausgang Plaza | Resident Advisor venue 158331 |
| Quai des Brumes | quaidesbrumes.ca/calendrier (Modern Events Calendar list; year from each entry's CSS class) |
| StereoBar | Resident Advisor venue 138829 (its Tixr page blocks automated access) |
| Sans Soleil | Resident Advisor venue 203475 (its own site is an Instagram page) |
| Blue Room | Resident Advisor venue 290582 (its own site is an Instagram page) |
| Heures ouvrables | the collective's Linktree: ticket links with a date in their name ('Heures ouvrables 007 (6 mars 2026)') |

### Museums & Galleries tab (`docs/museums.ics`)

| Source | How it's fetched |
|---|---|
| Musées Montréal | museesmontreal.org listing API. Dated events plus temporary/upcoming exhibitions; permanent exhibitions have no dates and are skipped. In the feed, each exhibition appears twice: on its opening day (if it hasn't opened yet) and on its last day. |
| DARE-DARE | dare-dare.org program list (start date + 'start — end'); calls for projects and the season umbrella skipped |
| Occurrence | occurrence.ca: exhibition dates from their titles ('06.11.-12.12.2026'); events from the date in their text |
| Fais-moi l'art | faismoilart.org/en-cours: 'Du 4 au 19 décembre 2026' blocks → exhibitions, 'Vernissage le …' → events |

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

**Not included: Supersystème** (a graphic design studio's portfolio, not a venue), **Aire Commune.** Its site has no dated events, and its Tixr page blocks automated access.

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

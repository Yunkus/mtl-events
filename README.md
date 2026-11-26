# Montreal Events calendar

Scrapes upcoming events from a list of Montreal venues every 6 hours and publishes:

- `docs/index.html` – a filterable web page
- one calendar feed per tab, to subscribe to in Google / Apple / Outlook Calendar:
  `music.ics` (Music & Dance), `other.ics` (Other Events), `bars.ics` (Bar Nights), `city.ics` (City Events), `museums.ics`, `movies.ics`.
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

In Museums & Galleries, the views are On now / Coming up / Events, and Museums vs Galleries can be
switched separately (Musées Montréal counts as museums; the rest are galleries and art centres).

On the page: Tonight, This weekend, Early shows / Late night (10 pm+), Free / Under $20 (events with no
known price stay listed, marked "price unknown"), and a Genre row on Music & Dance (music styles only) and a
Type row on Other Events, City Events and By Date: the 8 biggest, the rest behind "More". Every count, including
the counts on the tabs, follows the other active filters and the search box, so a combination with no results
shows 0 before you tap it. Venues with nothing upcoming
get no button.

## Alerts

A source empty for 8 updates in a row (2 days) also opens an issue once, unless it's in `QUIET_OK` in
`scrape.py` (sources checked by hand and known to be quiet, like McCord After Hours between dates).

If a source fails two updates in a row, or a venue that usually has 3+ events finds none two updates in a
row, the update opens a GitHub issue (you get an email). Venues that are simply quiet (Boiler Room, Eastern
Bloc, L'Escogriffe, Le Marché Vintage) don't trigger it.

## Images, details, picks and the app

- **Images**: taken from each source's own data where it has one (RA flyers, Squarespace, Eventbrite, Luma,
  Universe, museums, montreal.ca, cinema posters, Casa del Popolo cards), otherwise from the event page's share
  image (`og:image`). Page lookups are remembered in `docs/image_cache.json`, at most 250 new pages per update.
  The update then downloads each image once and saves a 480 px wide WebP copy in `docs/img/` (about 20 KB each),
  so the page never depends on a venue's server; copies nothing uses any more are deleted. Images that can't be
  downloaded keep their original address and are retried after 3 days (`docs/img/failed.json`).
- **No image**: events without one (or whose image fails to load) get amber-on-black art with the venue's name;
  the pattern is chosen by genre, so all the techno nights match.
- **Detail panel**: tapping an event shows its image, time, place, price, genres and a short description (the
  source's own text, RA's event text, or the event page's `og:description`; site-wide taglines and text that only
  repeats the title are skipped), with Tickets, Map, Add to calendar, Share and "Listen": one row per act on the
  bill (split at +, /, w/, with, x, &, b2b and commas; "presents", tour and album-launch wording dropped), each
  with YouTube / Spotify / Bandcamp searches.
  Shared links look like `…/mtl-events/#e/<id>`.
- **Tonight**: up to 4 picture cards on top of Music & Dance, Other Events and City Events. Favoured: `EDITOR_PICKS` in
  `scrape.py` (title fragments; those events also get an "Editor's pick" badge), then newly listed events.
- **New**: `docs/first_seen.json` keeps the day each event first appeared; the page marks anything added since
  your previous visit, and a "New" filter shows only those.
- **Phones**: the header is compact (Subscribe is one button with a menu, the tabs are one row that scrolls
  sideways and the filters fold into one button), so Tonight shows without scrolling.
- **App**: `manifest.webmanifest` + `sw.js` make it installable (Add to Home Screen) and usable offline,
  including the images seen recently.
  Link previews use `og-image.jpg`.
- Past events are hidden by the visitor's own clock, not the time of the last update.

## Music & Dance vs Other Events

Each event in the venue list below goes to **Music & Dance** unless:

- its source is in `OTHER_SOURCES` in `scrape.py` (MTL Cookbook Club, Soft Studio Days, Favourite Library, MBAM La Buvette, McCord After Hours,
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
| Barbossa | Resident Advisor venue 179148 (its own site is a Facebook page; Fever lists no events). Few nights are posted at a time, so it is in `QUIET_OK`. |
| LATEX. | latexrave.com sells tickets through Ticket Tailor: the organizer's box office (tickettailor.com/events/slataprod) and each event page's schema.org data (times, venue, ticket prices, flyer). The `tickettailor()` reader works for any Ticket Tailor organizer. In `QUIET_OK` (nothing listed between editions). |
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

### Museums & Galleries tab (`docs/museums.ics`)

| Source | How it's fetched |
|---|---|
| Musées Montréal | museesmontreal.org listing API. Dated events plus temporary/upcoming exhibitions; permanent exhibitions have no dates and are skipped. In the feed, each exhibition appears twice: on its opening day (if it hasn't opened yet) and on its last day. |
| DARE-DARE | dare-dare.org program list (start date + 'start — end'); calls for projects and the season umbrella skipped |
| Occurrence | occurrence.ca: exhibition dates from their titles ('06.11.-12.12.2026'); events from the date in their text |
| Fais-moi l'art | faismoilart.org/en-cours: 'Du 4 au 19 décembre 2026' blocks → exhibitions, 'Vernissage le …' → events |
| FOIL (Finer Objects in Life) | homepage events list (date + title per row, time from each row's detail panel) |
| Espace Transmission | espacetransmission.ca/événements ('14.11.24. …' entries; currently only past ones) and, for its parties, Resident Advisor venue 235410 (Music & Dance) |
| MBAM (La Buvette) — Other Events tab | each date has its own page in the MBAM sitemap (`/activities/la-buvette-DD-MM-YY/`); date from its schema.org data, time / length / price from its text. `mbam_series()` can follow other series. |
| McCord After Hours — Other Events tab | the page's "Next event" spot; nothing is listed until it shows a date (now: "Stay tuned!") |
| Montreal Art Center | montrealartcenter.com/our-events (Wix events). Film screenings go to the Movies tab; exhibitions and events here. |

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
| Trivia Nights (more bars) | trivianearme.net/montreal: about 30 weekly bar quizzes, repeated for 4 weeks, with the venue's note (rounds, team size, entry fee) as the description. A bar already listed by badslava or by hand on the same night is skipped (matched by street address), so Honey Martin keeps its 9:30 start. Left out: quizzes held every other week (the listing doesn't say which weeks) and bars off the island (Laval, Saint-Hubert). |
| Laugh It Off | the site's own events feed (comedy shows in bars, via Eventbrite), next 60 days only |
| Added by hand | `WEEKLY_BY_HAND` in `scrape.py`: weekly nights with no website (Tuesday trivia at Honey Martin, 9:30 PM, free). Add a line there to add another. |
| Added by hand (one-off) | `ONE_OFF_BY_HAND` in `scrape.py`: single events from organizers whose pages can't be read automatically (e.g. Heures ouvrables on Instagram / Zeffy). The tab follows the source name; past dates drop off. |
| Ville de Montréal — **City Events tab** (`docs/city.ics`) | montreal.ca/evenements, next 14 days, everything put on by the City: every Maison de la culture event; adult cultural library events (talks, author meetings, readings, concerts, screenings; no storytime, homework help, chess…); and shows, concerts, talks, workshops and park events at other city venues (cultural and community centres, halls, parks). Left out: council meetings, fitness classes, conversation groups, kids'/teen activities, postponed events. Sold-out shows are marked. (biblio.montreal.ca itself sits behind a bot check.) |
| Marchés publics — Other Events | marchespublics-mtl.com events: each weekend range ('4 - 6 décembre 2026') is one multi-day entry; toddler workshops skipped |
| Ateliers & Saveurs — Other Events | the public agenda pages (cooking, cocktail, wine): Montreal branches, next 14 days, classes with places left. Their JSON feed answers 403, so it isn't used. |
| Holiday markets — Other Events | `SEASONAL_BY_HAND` in `scrape.py` (Grand Marché de Noël, Village de Noël at Atwater; dates from mtl.org). Multi-day entries show under "Ongoing". |

**Not included: Jeudis du MAC** (macm.org sits behind Cloudflare's bot check), **Heures ouvrables** (Linktree refuses requests from GitHub's servers), **Vidéographe** (its program loads from a data service that refuses automated requests), **Supersystème** (a graphic design studio's portfolio, not a venue), **Aire Commune.** Its site has no dated events, and its Tixr page blocks automated access.

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

# Montreal Events calendar

Scrapes upcoming events from a list of Montreal venues every 6 hours and publishes:

- `docs/index.html` – a filterable web page
- `docs/calendar.ics` – a feed you subscribe to in Google / Apple / Outlook Calendar

## Sources

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
| Laugh It Off | the site's own events feed (Eventbrite shows), next 60 days only |
| Newspeak | Universe ticket links on newspeakmtl.com, dates from Universe |

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

Write a function in `scrape.py` that returns a list of `Ev(...)` and add it to `SOURCES`.
If the venue is on Resident Advisor, you only need one line: `lambda: ra_events("venue", "<id>", "<Name>")`.

# Concert Ticket Price Collector

This script logs concert ticket prices once a day from **SeatGeek** (resale prices and listing counts) and **Ticketmaster** (face-value price ranges and sale status). The price history is saved in a local SQLite database, `ticket_prices.db`, so you can train a prediction model on it later.

It uses only the Python standard library, so there's nothing to install.

## Setup

1. Get API keys (both are free):
   - SeatGeek: https://seatgeek.com/account/develop. Create an app and copy its **Client ID**.
   - Ticketmaster: https://developer.ticketmaster.com. Register and copy your **Consumer Key**.
2. Copy `.env.example` to `.env` and paste in your keys.
3. Edit `config.json` to choose what to track:
   - `cities`: the cities to search. Remove all of them to search nationwide.
   - `keywords`: artist names, such as `["Taylor Swift", "Zach Bryan"]`. Leave it empty to get all concerts.
   - `days_ahead`: how far into the future to look.

## Run it

```
python collector.py              # collect from both sources
python collector.py --source seatgeek
python collector.py --stats      # see how much data you've collected
```

## Run it every day automatically

1. Open **Task Scheduler** and choose **Create Basic Task**.
2. Set the trigger to **Daily** at a fixed time (the same time every day keeps the snapshots comparable).
3. For the action, pick **Start a program** and choose `run_collector.bat` in this folder.

Output is appended to `collector.log`.

## Database tables

- **events**: one row per show, with name, performers, venue, city and date.
- **price_snapshots**: one row per show per day, with low, average, median and high price, listing count, popularity scores, `days_until_event` and sale status.

How each source fills the snapshot fields:

- **SeatGeek** gives low, average, median and high resale prices, the listing count and popularity scores.
- **Ticketmaster** gives only the face-value low/high range and the sale status (for example `onsale` or `offsale`). Many events don't publish a range at all.

Example query to see how one show's price changed over time:

```sql
SELECT snapshot_date, days_until_event, price_low, price_median, listing_count
FROM price_snapshots
WHERE source = 'seatgeek' AND event_id = '<id>'
ORDER BY snapshot_date;
```

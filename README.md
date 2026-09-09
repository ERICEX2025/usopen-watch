# US Open good-seat watcher

Watches Gametime for listings that are BOTH under your price AND in a seat
tier worth sitting in, then pushes them to your phone.

Phone alerts go through [ntfy](https://ntfy.sh): install the app, subscribe to
a topic nobody will guess, put it in `config.json`. (Topics are unauthenticated,
so whoever knows yours can page your phone. Keep it out of the repo.) Desktop
notifications fire via osascript too.

## Setup

Python 3, standard library only. No pip.

    cp config.example.json config.json     # set ntfy_topic, tune the rest
    ./watch.py --test                       # phone + desktop should both buzz
    ./watch.py --scan                       # what qualifies right now

To run it every 15 minutes, edit the paths in `launchd/com.eko.usopenwatch.plist`,
copy it to `~/Library/LaunchAgents/`, and load it:

    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.eko.usopenwatch.plist

## Why it filters by tier, not just price

At Arthur Ashe the tiers price out like this:

    Promenade   from $143     <- the only tier ever under $200
    Upper       from $298
    Lower       from $599
    Courtside   from $3491

So a $200 budget at Ashe buys exactly one thing: the top of a 24,000-seat
stadium. At Louis Armstrong (14,000 seats) the same $200 buys Courtside
Reserved. Armstrong is not cheaper on average, but its cheap seats are good
seats. That is the whole edge, and a cheapest-listing alert would miss it
completely by paging you about Ashe nosebleeds all fortnight.

## Commands

    ./watch.py            one check (what launchd runs every 15 min)
    ./watch.py --scan     what qualifies right now, cheapest first
    ./watch.py --tiers    every tier and price, per session
    ./watch.py --history  best qualifying price over time
    ./watch.py --test     test the notifications

## Day-of mode (tonight.json)

For a single session you are trying to get into cheap, pin it and drop the
tier filter. `tonight.example.json` is the example (copy it to `tonight.json`):
Ashe Session 22 on Sep 9 2026, any tier, all-in cap $60 per ticket, urgent
BUY NOW push at $40.

    ./watch.py --config tonight.json            one check
    ./watch.py --config tonight.json --scan     what qualifies now
    ./watch.py --config tonight.json --history  cheapest buyable seat, every run

A second launchd job, `com.eko.usopenwatch.tonight` (also in `launchd/`), runs
it every 2 minutes and logs to `tonight.log`. It goes quiet after `stop_after`;
remove it with

    launchctl bootout gui/$(id -u)/com.eko.usopenwatch.tonight

Extra keys: `event_ids` (pin sessions, always checked even when over cap),
`instant_buy_price` (urgent push, always fires on crossing), `stop_after`
(local ISO time), `db` (separate history file). Pinned mode logs the floor
price every run, so `--history` shows the trend rather than only hits. Pushes
now attach the seat view and tapping them opens the buy page.

## Tuning (config.json)

- `max_price` - all-in dollars per ticket, fees included
- `party_size` - must appear in a listing's purchasable lot sizes. A listing
  sold only as a pair has `lots: [2,4]` and is correctly skipped when this is 1
- `tiers` - the seat groups worth buying, per venue. A venue absent from this
  map is never checked. `"*"` accepts every tier
- `min_drop_dollars` - re-alert only after a further drop this large
- `imessage_to` - optional. Your own phone number or Apple ID email. Alerts
  are also sent as an iMessage from this Mac, which reaches the phone with
  nothing installed there. Messages must be signed in on the Mac

Ashe **Promenade is deliberately excluded**. Add it back if you decide
cheap-and-far beats not going.

## On timing

From ticketdata.com's 2025 numbers (price at this many days out vs price at
session time), waiting was a losing move in five of seven rounds:

    Round of 128   $153 -> $148    -3%
    Round of 64    $171 -> $282   +65%
    Round of 32    $266 -> $357   +34%
    Round of 16    $244 -> $428   +75%
    Quarterfinals  $173 -> $206   +19%
    Semifinals     $269 -> $451   +68%
    Finals         $486 -> $448    -8%

First round is the one round where holding out historically paid, and even then
only barely. If you are targeting early rounds, the risk is inventory drying up,
not price running away. Do not extend "prices drop late" past the first round.

## Notes

- One marketplace (Gametime). SeatGeek and Ticketmaster gate their APIs behind
  keys; Gametime's is open, which is why it is the source. If it starts 403ing,
  that is why.
- 15-minute cycle, so a listing can vanish before you act.
- Only pulls full listings for sessions whose floor price is already under your
  cap, so a run is about a dozen requests rather than 68.

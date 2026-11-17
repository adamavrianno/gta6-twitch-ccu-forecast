# GTA VI Twitch viewership forecaster

[![Competition](https://make.fun/badge/gta6-twitch-ccu-forecast-01.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01)
[![Rank](https://make.fun/badge/gta6-twitch-ccu-forecast-01/adamavrianno/gta6-twitch-ccu-forecast/rank.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01)
[![Reward](https://make.fun/badge/gta6-twitch-ccu-forecast-01/adamavrianno/gta6-twitch-ccu-forecast/reward.svg)](https://make.fun/bounty/gta6-twitch-ccu-forecast-01)

Entry for the [make.fun bounty](https://make.fun/bounty/gta6-twitch-ccu-forecast-01)
"Forecast GTA VI's launch-day Twitch viewers hour by hour": given the first
hour's viewer print for Grand Theft Auto VI on release day, forecast the
concurrent-viewer count for each of hours 2 to 24 as five quantiles
(5%, 25%, 50%, 75%, 95%).

## How it works

`main.py` (Python 3.13, standard library only) works in log space. A
launch-day Twitch curve is driven by the rolling midnight-local unlock:
only New Zealand / Australia / Asia and the UK are live in hour 1, the US
unlocks in hours 5–9 and Europe at hour 24, so the count dips in hours 2–4,
climbs through the day and peaks in the evening. For each past big launch
in the case's comparables list, the program takes its launch-day profile
relative to its own hour-1 count, and forecasts the target's median for
each hour as the hour-1 print times the typical profile at that hour, with
the quantile spread taken from how much those profiles disagree at that
hour. Comparables are weighted by resemblance to the target launch:
console-only launches count most, multi-platform launches with a
simultaneous PC unlock count less, early-access-contaminated launches count
least. The target game's own pre-release history is not a launch, so it
contributes no profile.

The model was tuned by leave-one-out over the comparables in the practice
cases: console-only launches score a mean pinball loss of about 0.21 on the
log scale, against about 0.32 for the house baseline on the same launches
(the baseline scores about 0.387 on the practice cases).

## Running it

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

It reads every `*.in` file in the input directory and writes one
`<id>.out` file per case: `{"hours": [{"hour": 2, "q": [q05, q25, q50, q75,
q95]}, ...]}` for hours 2 to 24. Deterministic: the same input always
gives the same bytes, and a run takes well under a second per case.

## License

MIT. See [LICENSE](LICENSE).

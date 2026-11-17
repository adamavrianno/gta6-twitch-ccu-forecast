#!/usr/bin/env python3
"""Forecast GTA VI's launch-day Twitch viewers hour by hour.

Reads every ``*.in`` case file in the input directory (``$MAKEFUN_INPUT_DIR``,
else ``/input/``) and writes one ``<id>.out`` forecast per case to the output
directory (``$MAKEFUN_OUTPUT_DIR``, else ``/output/``).  Each case gives the
first hour's viewer print plus a list of past big game launches ("comparables")
with their hourly viewer counts around their own release days.

Method, in log space.  A launch-day Twitch curve is driven by the rolling
midnight-local unlock: only New Zealand / Australia / Asia and the UK are live
in hour 1, the US unlocks in hours 5-9, Europe at hour 24, so the count
typically dips in hours 2-4, climbs through the day and peaks in the evening.
For each comparable we therefore take its own launch-day profile relative to
its own hour-1 count, r_h = ln(viewers_h / viewers_1), and forecast the
target's median for hour h as the hour-1 print times a typical profile at
that hour, with the quantile spread taken from how much those profiles
disagree at that hour.  Comparables are weighted by how much they resemble
the target launch: console-only launches with a rolling midnight-local
unlock count most, multi-platform launches with a simultaneous PC unlock
count less, and early-access-contaminated launches count least.  The target
game's own pre-release history is not a launch, so it contributes no profile.

The spread is the weighted spread of the profiles at that hour, inflated a
little for the uncertainty of a new game, with a floor so no hour is ever
forecast as a point.  Quantiles are the median plus/minus the standard
normal's quantiles times that spread, so the forecast is honest about what
it does not know: pinball loss on the log scale is a proper scoring rule,
and a too-narrow forecast pays for every hour outside it.

Standard library only, no network, no randomness, no clock: the same input
always gives the same bytes.  Degenerate input is handled by clamping
viewer counts to at least 1 and falling back to a flat forecast with a
widening spread when no comparable has a usable launch day.
"""

import json
import math
import os
import sys
from datetime import datetime

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# Standard normal quantiles at the five levels.
Z = [-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722]

# Model constants.  Tuned by leave-one-out over the comparables in the
# practice cases (see the validation notes, not shipped in this repo).
PARAMS = {
    # Weight of a comparable whose console/PC mix matches the target's.
    "w_match_console": 1.0,
    # Weight of a comparable whose console/PC mix differs from the target's.
    "w_mismatch_console": 0.1,
    # Weight of a comparable whose unlock timing matches the target's.
    "w_match_roll": 1.0,
    # Weight of a comparable whose unlock timing differs from the target's.
    # Set to 1.0: the unlock text is too noisy to split on (several
    # console-only launches are merely "unverified"), and leave-one-out
    # found no gain from penalising the mismatch.
    "w_mismatch_roll": 1.0,
    # Extra multiplier for early-access-contaminated comparables.
    "w_early": 0.25,
    # How the per-hour median profile is taken: "wmedian" or "wmean".
    "median": "wmedian",
    # How the per-hour spread is taken: "wsd" or "wiqr".
    "spread": "wiqr",
    # Multiplier on the per-hour log spread (honest widening).
    "spread_infl": 1.1,
    # Minimum log spread for any hour.
    "spread_floor": 0.15,
    # Maximum log spread for any hour (safety cap).
    "spread_cap": 1.0,
    # 0 = use each hour's profile as-is; 1 = light 3-hour smoothing of the
    # median profile (weights 0.25 / 0.5 / 0.25).
    "median_smooth": 0,
}

# The target of this competition, from the rules: PlayStation 5 and Xbox
# Series X|S only, unlocking at midnight local time country by country.
TARGET_CONSOLE_ONLY = True
TARGET_ROLLING = True


def _parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _clamp_viewers(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value) or value < 1.0:
        return 1.0
    return value


def _weighted_median(pairs):
    """Weighted median of (value, weight) pairs."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return ordered[len(ordered) // 2][0]
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= 0.5 * total:
            return value
    return ordered[-1][0]


def _weighted_mean(pairs):
    total = sum(w for _, w in pairs)
    if total <= 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total


def _weighted_sd(pairs, mean):
    total = sum(w for _, w in pairs)
    if total <= 0 or len(pairs) < 2:
        return 0.0
    var = sum(w * (v - mean) ** 2 for v, w in pairs) / total
    return math.sqrt(max(var, 0.0))


def _weighted_iqr_sd(pairs):
    """Robust spread: interquartile range / 1.349, from weighted quartiles."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0 or len(ordered) < 4:
        return 0.0

    def quantile(prob):
        acc = 0.0
        for value, weight in ordered:
            acc += weight
            if acc >= prob * total:
                return value
        return ordered[-1][0]

    return max(quantile(0.75) - quantile(0.25), 0.0) / 1.349


def _release_day(comp):
    """Hour -> viewers for a comparable's release day (UTC day of day_start).

    Returns an empty dict when the comparable has no launch day in its
    hourly data (for example the target game's own pre-release history).
    """
    try:
        day_start = _parse_ts(comp["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    out = {}
    for row in comp.get("hourly") or []:
        try:
            when = _parse_ts(row["from"])
        except (KeyError, TypeError, ValueError):
            continue
        offset = (when - day_start).total_seconds() / 3600.0
        if 0.0 <= offset < 24.0 and abs(offset - round(offset)) < 1e-6:
            hour = int(round(offset)) + 1
            out[hour] = _clamp_viewers(row.get("viewers"))
    return out


def _comparable_weight(comp, target_console_only, target_rolling, params):
    """How much this comparable resembles the target launch."""
    unlock = (comp.get("unlock") or "").lower()
    if "not a launch" in unlock:
        # The target game's own pre-release history: no launch profile.
        return 0.0
    platforms = (comp.get("platforms") or "").upper()
    comp_console_only = "PC" not in platforms
    comp_rolling = "midnight local" in unlock or "rolling" in unlock
    weight = params["w_match_console"] if comp_console_only == target_console_only \
        else params["w_mismatch_console"]
    weight *= params["w_match_roll"] if comp_rolling == target_rolling \
        else params["w_mismatch_roll"]
    if "early-access" in unlock or "contaminated" in unlock:
        weight *= params["w_early"]
    return weight


def _hour1_print(case):
    """The known hour-1 viewer print, with fallbacks for degenerate cases."""
    for row in case.get("known") or []:
        if row.get("hour") == 1:
            return _clamp_viewers(row.get("viewers"))
    hourly = case.get("history_hourly") or []
    if hourly:
        return _clamp_viewers(hourly[-1].get("viewers"))
    history = case.get("history") or []
    if history:
        return _clamp_viewers(history[-1].get("viewers"))
    return 1.0


def _flat_forecast(print_viewers):
    """Fallback: every hour at the hour-1 print, spread widening over the day.

    This is the house baseline's shape, used only when no comparable has a
    usable launch-day profile.
    """
    rows = []
    for hour in range(2, 25):
        spread = 0.15 * math.sqrt(hour - 1)
        quantiles = [print_viewers * math.exp(z * spread) for z in Z]
        rows.append(_clean_row(hour, quantiles))
    return {"hours": rows}


def _clean_row(hour, quantiles):
    """Clamp quantiles to (0, 1e9], enforce non-decreasing order, round."""
    cleaned = []
    previous = 0.0
    for value in quantiles:
        if not math.isfinite(value):
            value = previous if previous > 0 else 1.0
        value = min(max(value, 0.01), 1_000_000_000.0)
        value = round(value, 2)
        if value <= previous:
            value = previous if previous > 0 else value
        value = min(max(value, 0.01), 1_000_000_000.0)
        cleaned.append(value)
        previous = value
    return {"hour": hour, "q": cleaned}


def _stream_concentration(case, print_viewers):
    """Extra spread factor from the final case's live-stream snapshot.

    When the top streams are extremely concentrated (one stream carrying
    most of the category, or the top-20 sum disagreeing with the print),
    the day is more streamer-driven and less predictable, so the spread
    widens a little.  Returns 1.0 when the snapshot is absent (practice)
    or unremarkable, and never more than 1.15.
    """
    streams = case.get("streams") or []
    if not streams or print_viewers <= 0:
        return 1.0
    viewers = sorted((_clamp_viewers(s.get("viewers")) for s in streams),
                     reverse=True)
    top1_share = viewers[0] / print_viewers if viewers else 0.0
    top20_sum = sum(viewers)
    if top1_share > 0.5 or top20_sum > 1.25 * print_viewers:
        return 1.15
    if top1_share > 0.35:
        return 1.07
    return 1.0


def forecast_case(case, params=None, target_console_only=None,
                  target_rolling=None):
    """Forecast one case dict, returning {"hours": [...]}."""
    params = dict(PARAMS if params is None else params)
    if target_console_only is None:
        target_console_only = TARGET_CONSOLE_ONLY
    if target_rolling is None:
        target_rolling = TARGET_ROLLING

    print_viewers = _hour1_print(case)

    # Per-hour log-ratios r_h = ln(viewers_h / viewers_1) across comparables.
    by_hour = {hour: [] for hour in range(2, 25)}
    for comp in case.get("comparables") or []:
        weight = _comparable_weight(comp, target_console_only,
                                    target_rolling, params)
        if weight <= 0:
            continue
        day = _release_day(comp)
        if len(day) < 24 or 1 not in day:
            continue
        hour1 = day[1]
        for hour in range(2, 25):
            if hour in day:
                by_hour[hour].append(
                    (math.log(day[hour] / hour1), weight))

    usable = [hour for hour in range(2, 25) if by_hour[hour]]
    if not usable:
        return _flat_forecast(print_viewers)

    # Per-hour median profile and spread.
    medians = {}
    spreads = {}
    for hour in range(2, 25):
        pairs = by_hour[hour]
        if not pairs:
            continue
        if params["median"] == "wmean":
            centre = _weighted_mean(pairs)
        else:
            centre = _weighted_median(pairs)
        if params["spread"] == "wiqr":
            spread = _weighted_iqr_sd(pairs)
        else:
            spread = _weighted_sd(pairs, centre)
        medians[hour] = centre
        spreads[hour] = min(max(spread, params["spread_floor"]),
                            params["spread_cap"])

    # Fill any missing hour from its neighbours so every hour is forecast.
    for hour in range(2, 25):
        if hour not in medians:
            near = [h for h in range(2, 25) if h in medians]
            closest = min(near, key=lambda h: abs(h - hour))
            medians[hour] = medians[closest]
            spreads[hour] = spreads[closest]

    if params["median_smooth"]:
        smoothed = {}
        for hour in range(2, 25):
            left = medians.get(hour - 1, medians[hour])
            right = medians.get(hour + 1, medians[hour])
            smoothed[hour] = 0.25 * left + 0.5 * medians[hour] + 0.25 * right
        medians = smoothed

    extra = _stream_concentration(case, print_viewers)
    rows = []
    for hour in range(2, 25):
        spread = spreads[hour] * params["spread_infl"] * extra
        quantiles = [print_viewers * math.exp(medians[hour] + z * spread)
                     for z in Z]
        rows.append(_clean_row(hour, quantiles))
    return {"hours": rows}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input/"
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output/"
    os.makedirs(output_dir, exist_ok=True)

    names = sorted(name for name in os.listdir(input_dir)
                   if name.endswith(".in"))
    for name in names:
        path = os.path.join(input_dir, name)
        with open(path, "r", encoding="utf-8") as handle:
            case = json.load(handle)
        forecast = forecast_case(case)
        case_id = case.get("id") or name[:-3]
        out_path = os.path.join(output_dir, case_id + ".out")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(forecast, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

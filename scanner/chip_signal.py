"""
Chip (institutional-flow) verdict for tomorrow. ASCII only.

The owner's question (2026-09-21): "the recommendation should look at the
day's chips and say whether to sell or add the next day". This module turns
the day's three-institution net (ingestion/inst_trades.py) into one readout
per row and, where a rule was validated, one ACTION for the next open.

It annotates; it never changes selection or the exit stack. Columns added:

  Inst_Pct       today's three-institution net as a percentage of the stock's
                 20-session average volume (net lots / Vol_MA20 * 100). "-2"
                 means the institutions net sold 2% of a normal day's volume.
                 None when the average volume is unknown.
  Chip_Basis     'current' when the flow is for the same session as the bar
                 (Inst_Date == Data_Date), 'lag' when the institutional feed
                 is behind the price feed, '' when there is no flow data.
  Chip_Action    what the validated rule says to do at the NEXT open for a
                 position that is open today:
                   'sell'  exit at the next open
                   'add'   buy the second half at the next open (staged plan)
                   'hold'  no chip reason to act
                   ''      not a held position, no rule for this mode, or the
                           flow is missing / lagging (a decision on stale chips
                           is not a decision)
  Chip_Note      ASCII sentence with the numbers behind the verdict.

THE VERDICT, AND WHY BOTH LEGS ARE OFF (2026-09-21,
archive/research/sandbox_chip_manage.py; 562 CORE+ first-day trades with 89%
institutional coverage, paired against the adopted exit stack):

  SELL on institutional selling -- refuted, badly. Every threshold and every
  shape loses in BOTH windows. Exiting at the next open when the three
  institutions net sold takes the recent-3y win rate from 67.1% to 47.4%;
  requiring 2% of average volume, or two or three consecutive days, or a down
  day, only softens it (51-61%). The worst variant is the most intuitive one:
  "it is under water AND institutions are selling" scores 46.2% against 67.1%.

  ADD on institutional buying -- refuted too. Every threshold loses 3-11pp of
  win rate, and the added tranche itself wins only ~50% of the time.

  WHY, measured directly on the whole OTC tape (137,925 stock-days over the
  last three years): today's institutional net, as a share of average volume,
  DOES predict tomorrow -- rank correlation +0.041 with the next day's
  close-to-close move, t = +11.6. But against the next day's OPEN-to-close it
  is -0.003, t = -0.9. The entire signal is in the overnight GAP. The scan
  runs after the close and the earliest anyone can act is the next open, by
  which time the move has already happened. The flow is real and unusable.

So CHIP_RULES below are both disabled, and every leg stays that way until a
study passes the adoption gate. The columns remain as a CONFIRMATION readout,
which is also what the older chip work concluded for holder levels and
foreign-flow filters. annotate_chip_action says so in Chip_Note rather than
emitting a "hold" that would dress an unvalidated number up as advice.
"""
import pandas as pd

# Adoption verdict of the 2026-09-21 study (filled in from the run). A leg is
# only turned into an action when it beat the plain rule on win rate AND
# expectancy in both the recent (3y) and the older window.
CHIP_RULES = {
    "sell": {"enabled": False, "inst_pct_max": -2.0, "streak_min": None},
    "add":  {"enabled": False, "inst_pct_min": 2.0, "streak_min": None,
             "max_hold_day": 5},
}
CHIP_MODES = ("mode_prelaunch",)
HELD_STATUSES = ("holding", "delay", "exit_today", "overdue")


def any_rule_enabled(rules=None):
    rules = rules or CHIP_RULES
    return any(bool(r.get("enabled")) for r in rules.values())


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def inst_pct(inst_net, vol_ma20):
    n, v = _f(inst_net), _f(vol_ma20)
    if n is None or not v or v <= 0:
        return None
    return round(n / v * 100.0, 2)


def chip_basis(inst_date, data_date):
    d = str(inst_date or "")[:10]
    if not d:
        return ""
    return "current" if d == str(data_date or "")[:10] else "lag"


def sell_signal(pct, streak, rule=None):
    rule = rule or CHIP_RULES["sell"]
    if not rule.get("enabled") or pct is None:
        return False
    if rule.get("inst_pct_max") is not None and pct <= rule["inst_pct_max"]:
        return True
    smin = rule.get("streak_min")
    return bool(smin and streak is not None and streak <= -smin)


def add_signal(pct, streak, hold_day, rule=None):
    rule = rule or CHIP_RULES["add"]
    if not rule.get("enabled") or pct is None:
        return False
    mx = rule.get("max_hold_day")
    if mx is not None and (hold_day is None or hold_day > mx):
        return False
    if rule.get("inst_pct_min") is not None and pct >= rule["inst_pct_min"]:
        return True
    smin = rule.get("streak_min")
    return bool(smin and streak is not None and streak >= smin)


def describe(inst_net, pct, streak, sessions):
    """One ASCII sentence with the day's numbers."""
    n = _f(inst_net)
    if n is None:
        return "no institutional data"
    side = "bought" if n > 0 else ("sold" if n < 0 else "flat")
    s = "institutions net {} {:+.0f} lots".format(side, n) if n else "institutions flat"
    if pct is not None:
        s += " ({:+.1f}% of avg volume)".format(pct)
    st = _i(streak)
    if st and abs(st) >= 2:
        s += ", {} sessions in a row".format(abs(st))
    if sessions is not None and _i(sessions) is not None and _i(sessions) < 5:
        s += ", {}/5 sessions available".format(_i(sessions))
    return s


def annotate_chip_action(df, scan_mode):
    """Add Inst_Pct / Chip_Basis / Chip_Action / Chip_Note. Never raises."""
    if df is None or df.empty:
        return df
    df = df.copy()
    n = len(df)
    col = lambda c: (df[c] if c in df.columns else pd.Series([None] * n, index=df.index))  # noqa: E731
    inst_net, vol20 = col("Inst_Net"), col("Vol_MA20")
    inst_date, data_date = col("Inst_Date"), col("Data_Date")
    streak, sessions = col("Inst_Streak"), col("Inst_Sessions")
    status, exit_sig = col("Hold_Status"), col("Exit_Signal")
    hold_day = col("Hold_Day")

    pcts, basis, actions, notes = [], [], [], []
    rule_mode = scan_mode in CHIP_MODES
    for i in range(n):
        p = inst_pct(inst_net.iloc[i], vol20.iloc[i])
        b = chip_basis(inst_date.iloc[i], data_date.iloc[i])
        note = describe(inst_net.iloc[i], p, streak.iloc[i], sessions.iloc[i])
        act = ""
        st = str(status.iloc[i] or "")
        held = st in HELD_STATUSES and not str(exit_sig.iloc[i] or "")
        if rule_mode and held and not any_rule_enabled():
            # No leg passed the adoption gate: the readout is confirmation
            # only, and "hold" would dress an unvalidated number up as advice.
            note += "; no validated chip rule -> confirmation only"
        elif rule_mode and held:
            if b != "current":
                note += "; flow for {} not {} -> no verdict".format(
                    str(inst_date.iloc[i] or "?")[:10], str(data_date.iloc[i] or "?")[:10]) \
                    if b == "lag" else "; no verdict"
            elif sell_signal(p, _i(streak.iloc[i])):
                act = "sell"
                note += " -> rule: exit at next open"
            elif add_signal(p, _i(streak.iloc[i]), _i(hold_day.iloc[i])):
                act = "add"
                note += " -> rule: buy the staged second half at next open"
            else:
                act = "hold"
                note += " -> no chip reason to act"
        pcts.append(p)
        basis.append(b)
        actions.append(act)
        notes.append(note)
    df["Inst_Pct"] = pcts
    df["Chip_Basis"] = basis
    df["Chip_Action"] = actions
    df["Chip_Note"] = notes
    return df

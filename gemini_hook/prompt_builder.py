"""
Prompt for the AI report, and the local template used when no AI answers.
ASCII only.

All prompt text is English, and the instruction asks the model to answer in
Traditional Chinese. Every Chinese string -- the per-row reason quoted to the
model (why=), the order-execution note (exec=) and the whole local template --
lives in config/report_text.json, loaded by report_text() with an ASCII
English fallback, so a missing or broken JSON can never stop the template
(the last line of defence) from rendering.

2026-10-08 (plan P1-7). The prompt explained the refusal codes in one clause,
"quality/market/rank = fails a selection filter", and sent only the raw code
per row. Gemini read block=market (a TWSE-listed name; the rule only buys
TPEx/OTC stocks) as "the system refused because market conditions are
unfavourable" -- in both the TSE and the ALL reports of 2026-10-07 -- and
block=quality as a fundamentals warning. Each code now has its own legend line
(BLOCK_LEGEND), every buy=NO row carries the exact Chinese reason to quote,
and rule 5 forbids turning a code into a statement about the market or the
company.
"""
import json
import math
from pathlib import Path

import pandas as pd

REPORT_TEXT_FILE = (Path(__file__).resolve().parent.parent
                    / "config" / "report_text.json")

# One line per Buy_Block code the model can see, in the order the legend is
# printed. Must cover scanner.result_checks.BUY_BLOCKS (minus "") and
# not_evaluated -- tests/test_ai_report.py fails on a code without one. The
# facts come from scanner/scan_mode.mark_buy_ready: its masks run in order and
# the LAST one that applies ships, so e.g. "market" is a TWSE name inside the
# top 20 (a lower-ranked one says "rank"), and "regime" replaces every row's
# code.
BLOCK_ORDER = ("regime", "regime_stale", "market", "rank", "quality",
               "held", "restricted", "unknown", "integrity", "stale",
               "no_rule", "dropped", "not_evaluated")


# Restriction kinds that are shown with order guidance (exec=) and do not
# refuse a buy unless trade_restrictions.BLOCKING_RESTRICTIONS names them.
_SHOWN_RESTRICTIONS = ("disposition", "attention", "altered", "limit_lock")


def blocking_restrictions():
    """scanner.trade_restrictions.BLOCKING_RESTRICTIONS, read at call time
    like mark_buy_ready reads it, so a flip there is reflected here too."""
    try:
        from scanner.trade_restrictions import BLOCKING_RESTRICTIONS
        return tuple(str(k) for k in BLOCKING_RESTRICTIONS)
    except Exception:
        return ("suspended",)


def _restricted_legend(blocking):
    text = ("the stock's exchange restriction is one the rule treats as "
            "blocking (kinds: {}), so the rule does not buy it.".format(
                ", ".join(blocking) or "none"))
    others = [k for k in _SHOWN_RESTRICTIONS if k not in blocking]
    if others:
        text += (" Other exchange measures ({}) do NOT refuse a buy; they "
                 "only change how the order executes, see exec=.".format(
                     ", ".join(others)))
    return text


BLOCK_LEGEND = {
    "regime": "the TAIEX is not above BOTH its 20- and 60-day averages, so the "
              "rule buys NOTHING today. This is the ONLY code about overall "
              "market conditions.",
    "regime_stale": "the index data is not updated to today, so the tailwind "
                    "cannot be judged and nothing is bought today. It does NOT "
                    "mean the index is weak.",
    "market": "listed on the TWSE main board. The rule only ever buys "
              "TPEx/OTC stocks, so every TWSE row is refused for that reason "
              "alone. It is NOT about market conditions, the index or the "
              "stock itself.",
    "rank": "its position in today's FULL shortlist (list#) is beyond 20; "
            "the rule only buys list positions 1-20. It says nothing about "
            "the stock.",
    "quality": "an OTC top-20 name that fails the CORE+ entry-timing gate "
               "(within 5% of the 52-week high, 5-day return <= 5%, average "
               "daily range >= 4.5%, all three). A timing filter, NOT a "
               "judgement on the company, its fundamentals or its quality.",
    "held": "not a NEW signal: the name was already on the previous "
            "session's list, and the rule only buys a name on its first day "
            "on the list. It does NOT mean the rule bought it earlier: that "
            "earlier day may have been refused too (e.g. by regime or "
            "quality). Says nothing about the stock.",
    "restricted": _restricted_legend(blocking_restrictions()),
    "unknown": "the holding state could not be determined, so the rule "
               "refuses instead of guessing.",
    "integrity": "this stock's data failed its own consistency checks; no "
                 "decision is made on it.",
    "stale": "this stock's own data is not from the latest session; no "
             "decision is made on it.",
    "no_rule": "this scan mode has no validated buy rule, so nothing in it "
               "is bought.",
    "dropped": "the name has left today's list; shown only so a holder can "
               "track the exit.",
    "not_evaluated": "the backend did not run its buy decision; treat the row "
                     "as not buyable.",
}

# ASCII fallback for every key of config/report_text.json. report_text()
# overlays the JSON on top of this, key by key, so a JSON that lacks a code
# (or is missing altogether) still yields a complete, readable report.
_FALLBACK_TEXT = {
    "template_header": "[TEMPLATE REPORT - not AI] No AI report this run; "
                       "below is a rule-based local summary with no AI "
                       "interpretation.",
    "empty": "(no names qualified in this scan)",
    "flags": {
        "Cond_A": "box squeeze", "Cond_C": "accumulation",
        "Cond_B": "large holders adding", "MA_Bull_Align": "MA bull",
        "Donchian_Break": "Donchian breakout", "MACD_Cross": "MACD cross",
        "Near_52W_High": "near 52w high", "RS_Strong": "RS strong",
    },
    "flags_none": "no notable signal",
    "flags_sep": ", ",
    "verdict_unevaluated": "Buy decision: not evaluated (not buyable)",
    "verdict_ok": "Buy decision: meets the rule",
    "verdict_no": "Buy decision: not buying ({reason})",
    "row": "#{rank}{list_pos} {sid} {name}  close {close}\n   {verdict}\n"
           "   score={score}  entry ref={buy}  stop={stop}\n"
           "   signals: {flags}\n"
           "   support={sup}  resist={res}  to support={sgp}%  "
           "to resist={rgp}%\n",
    "row_exec": "   trading restriction: {note}\n",
    # Appended to "#{rank}" as {list_pos} when the row's position in the FULL
    # list differs from its rank in this report (the per-market reports).
    "row_pos": " (full list #{pos})",
    "block_reason": dict(BLOCK_LEGEND),
    "block_reason_missing": "the backend gave no reason; not buyable",
    "block_label": {k: k for k in BLOCK_ORDER},
    "restriction_label": {
        "suspended": "suspended", "disposition": "disposition",
        "altered": "altered trading", "limit_lock": "limit-up lock",
        "unknown": "restriction data unavailable", "attention": "attention",
        "none": "none",
    },
    "restriction_note": {
        "suspended": "trading suspended; no order can be placed",
        "disposition": "disposition{until}{match}{prepay}; the buy decision "
                       "is unchanged, but orders fill by periodic call "
                       "auction and stop/lock exits may fill late",
        "altered": "altered trading (e.g. full delivery): purchases are "
                   "prepaid and matching is slower; the buy decision is "
                   "unchanged, exits may fill late",
        "limit_lock": "closed locked limit-up today: the next open may gap "
                      "or not fill; the entry is the actual fill; the buy "
                      "decision is unchanged",
        "unknown": "restriction data unavailable this run; check for "
                   "disposition/attention before ordering",
        "attention": "attention list (an exchange notice, not a "
                     "disposition): the buy decision is unchanged",
        "blocking": "{label}{until}{match}{prepay}: in the rule's blocking "
                    "set, so the rule does not buy it",
        "until": " (until {until})",
        "match": ", matched about every {min} minutes",
        "prepay_all": ", every order must be prepaid",
        "prepay_threshold": ", orders of 10+ lots (30+ in a day) must be "
                            "prepaid",
    },
}
_DICT_KEYS = tuple(k for k, v in _FALLBACK_TEXT.items() if isinstance(v, dict))
_TEXT_CACHE = {}


def report_text(path=None):
    """config/report_text.json overlaid on the ASCII fallback. Never raises.

    Cached per (path, mtime, size), so an edited file is picked up and a test
    can point REPORT_TEXT_FILE elsewhere. Only string values are taken from
    the file; anything else keeps the fallback."""
    p = Path(path) if path else Path(REPORT_TEXT_FILE)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), None, None)
    hit = _TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in _FALLBACK_TEXT.items()}
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in _DICT_KEYS and isinstance(v, dict):
                out[k].update({str(a): b for a, b in v.items()
                               if isinstance(b, str) and b})
            elif k not in _DICT_KEYS and isinstance(v, str) and v:
                out[k] = v
    _TEXT_CACHE.clear()
    _TEXT_CACHE[key] = out
    return out


def _fmt(template, **kw):
    """template.format(**kw), or the template as written when it does not
    format (a stray brace in the JSON must not kill a report)."""
    try:
        return str(template).format(**kw)
    except (KeyError, IndexError, ValueError, AttributeError):
        return str(template)


def _s(val):
    """A cell as stripped text; None / NaN as ''."""
    if val is None:
        return ""
    try:
        if isinstance(val, float) and math.isnan(val):
            return ""
        if val is pd.NA or val is pd.NaT:
            return ""
    except Exception:
        pass
    return str(val).strip()


_FALSE_TEXT = ("false", "0", "0.0", "no", "n", "none", "nan")


def _truthy(val):
    """bool(val), with None / NaN as False (a NaN flag is not a yes), and a
    text cell read back from a CSV ("False", "0") as False too -- bool() of
    any non-empty string is True, which would print a refused row as YES."""
    if _s(val) == "":
        return False
    if isinstance(val, str):
        return val.strip().lower() not in _FALSE_TEXT
    try:
        return bool(val)
    except Exception:
        return False


def _quote(text):
    """Text safe inside key="..." on a prompt line."""
    return " ".join(str(text).replace('"', "'").split())


def _pos_int(val):
    """A positive int (a list position, a matching interval), else None."""
    try:
        n = int(float(val))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n > 0 else None


def _list_pos(row, rank):
    """The row's position in the FULL list (List_Pos, stamped by
    scan_headless.build_market_reports) when it differs from its rank in
    this report, else None."""
    pos = _pos_int(row.get("List_Pos"))
    return pos if pos and pos != rank else None


def block_reason(code, row=None, text=None):
    """The Chinese reason for a refused row (report_text.json block_reason),
    with {restriction} filled from the row's Trade_Restriction. An unknown
    code falls back to the code itself, an empty one to block_reason_missing."""
    txt = text or report_text()
    code = _s(code)
    if not code or code == "-":
        return txt.get("block_reason_missing") or "-"
    tpl = (txt.get("block_reason") or {}).get(code)
    if not tpl:
        return code
    kind = _s(row.get("Trade_Restriction")) if row is not None else ""
    label = (txt.get("restriction_label") or {}).get(kind, kind) or "-"
    return _fmt(tpl, restriction=label)


def restriction_note(row, text=None):
    """Order-execution note for a row's Trade_Restriction ('' when none).

    Display only: disposition / attention / limit_lock do not change the buy
    decision (DECISIONS addendum 1; BACKTEST_LOG section M), so the note
    explains execution -- periodic call auction, prepayment, late exit fills
    -- from the parsed Restriction_Match_Min / Restriction_Prepay, never a
    hardcoded interval."""
    txt = text or report_text()
    kind = _s(row.get("Trade_Restriction"))
    if kind in ("", "none"):
        return ""
    notes = txt.get("restriction_note") or {}
    tpl = notes.get(kind)
    if kind in _SHOWN_RESTRICTIONS and kind in blocking_restrictions():
        # The blocking set was widened (trade_restrictions): this kind now
        # refuses the buy, so its "does not change the decision" note would
        # be false.
        tpl = notes.get("blocking") or tpl
    if not tpl:
        return ""
    label = (txt.get("restriction_label") or {}).get(kind, kind)
    until = _s(row.get("Restriction_Until"))[:10]
    mins = _pos_int(row.get("Restriction_Match_Min"))
    prepay = _s(row.get("Restriction_Prepay"))
    parts = {
        "until": _fmt(notes.get("until", ""), until=until) if until else "",
        "match": _fmt(notes.get("match", ""), min=mins) if mins else "",
        "prepay": notes.get("prepay_" + prepay, "") if prepay else "",
    }
    return _fmt(tpl, label=label, **parts)


def block_legend():
    """BLOCK_LEGEND with the restricted line rebuilt from the blocking set in
    force now."""
    out = dict(BLOCK_LEGEND)
    out["restricted"] = _restricted_legend(blocking_restrictions())
    return out


def _legend(legend):
    return "".join("  - block={}: {}\n".format(code, legend[code])
                   for code in BLOCK_ORDER)


_SYSTEM_HEAD = (
    "You are a senior Taiwan stock market chip analyst. "
    "You will receive rows the backend has ALREADY screened, ranked and judged. "
    "Each row contains technical and chip indicators plus the backend's own "
    "buy decision. "
    "Explanation of the columns:\n"
    "- rank: the backend's shipped order within this report. Row 1 is its "
    "first pick here.\n"
    "- list#: the row's position in today's FULL shortlist (both markets), "
    "which is the position the buy rule ranks. A market report shows only "
    "its own market, so rank and list# can differ; list# is omitted when it "
    "equals rank.\n"
    "- buy: the backend's decision. YES = the trading rule buys this name "
    "tomorrow. NO = it refuses, and block names the reason. Each block code "
    "means exactly this and nothing else:\n"
)
_SYSTEM_TAIL = (
    "- why: on every buy=NO row, the backend's own reason in Traditional "
    "Chinese. Quote it exactly as written.\n"
    "- exec: present only when the exchange has a measure on the stock "
    "(disposition, attention, altered trading, a limit-up lock, a "
    "suspension, or restriction data unavailable). Order-execution guidance "
    "in Traditional Chinese; quote it as written.\n"
    "- buyPx / stopPx / riskPct: the reference entry, the protective stop and "
    "the distance between them. These are anchored on the intended FILL, so "
    "quote them as they are; do not invent your own entry or stop.\n"
    "- Explosion_Score: 0 to 100, higher means closer to a breakout.\n"
    "- gain3m: price change percent over the last ~3 months (momentum already realized).\n"
    "- Range_Tightness: 20-day box width ratio, smaller means tighter consolidation.\n"
    "- Volume_Dryup: the 3-day average volume divided by the 20-day average "
    "volume (a 3-day average, not a single day, so one spike cannot swing it), "
    "smaller means volume is drying up.\n"
    "- Volume_Bias: ratio of up-day volume over total, above 0.6 implies accumulation.\n"
    "- MA20 / MA60: moving average support or resistance levels.\n"
    "- Resist_60H / Support_60L: 60-day high (resistance) and low (support).\n"
    "- VP_Zone1..3: top volume-profile price zones (main holding cost areas).\n"
    "- Gap_Up_Sup / Gap_Dn_Res: gap support and gap resistance levels.\n"
    "- Round_Level: nearest psychological round-number level.\n"
    "- Sup_Gap_Pct / Res_Gap_Pct: distance percent to support and resistance.\n"
    "- Squeeze: YES means price is squeezed between strong support and weak resistance.\n"
    "- date: the trading session this row's data is from.\n\n"
    "Write a concise daily report in TRADITIONAL CHINESE (zh-TW). "
    "For each stock give: a one-line verdict, the key trigger to watch tomorrow "
    "(volume multiple, breakout price, support to hold), and a risk note.\n"
    "RULES YOU MUST FOLLOW:\n"
    "1. Keep the given rank order. Do NOT re-rank, re-score or promote a name "
    "above another. Your job is to EXPLAIN the backend's decision, not to "
    "replace it.\n"
    "2. Never present a buy=NO row as something to buy. Say plainly that the "
    "rule is not buying it; you may still describe what would have to "
    "change.\n"
    "3. You are given only the top rows of a longer shortlist, and the "
    "shortlist itself is only what today's scan produced. Do not claim it is "
    "the whole market or the best available names, and do not comment on "
    "anything that is not in the table.\n"
    "4. Do NOT give financial advice; frame everything as technical "
    "observation only.\n"
    "5. For every buy=NO row, give the reason by quoting its why= text "
    "exactly. Never turn a block code into a statement about market "
    "conditions, the company, its fundamentals or its quality: only "
    "block=regime is about the overall market, and block=regime_stale only "
    "means the index data is late.\n"
    "6. exec= is execution guidance, not a verdict. On a buy=YES row it does "
    "NOT change the buy decision: do not call the name riskier, weaker or "
    "unbuyable because of it, and do not invent a matching interval or "
    "prepayment terms that exec= does not state."
)


def system_instruction():
    """The system message both providers send (gemini_client)."""
    return _SYSTEM_HEAD + _legend(block_legend()) + _SYSTEM_TAIL


# Kept for importers of the constant; the client calls system_instruction().
SYSTEM_INSTRUCTION = system_instruction()


def _row_to_line(row: pd.Series, rank: int, text=None) -> str:
    txt = text or report_text()

    def g(key, default="NA"):
        val = row.get(key, default)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return str(val)

    # F23: the line used to carry scores only, so the model happily wrote up a
    # name the rule refuses to buy as tomorrow's trade, and quoted entries it
    # had invented. buy / block and the fill-anchored levels are the
    # AUTHORITATIVE decision -- they lead the line for that reason.
    if "Buy_Ready" in row:
        buy = "YES" if _truthy(row.get("Buy_Ready")) else "NO"
        block = _s(row.get("Buy_Block")) or "-"
    else:
        # mark_buy_ready did not run for this frame: unevaluated is not refused.
        buy, block = "UNKNOWN", "not_evaluated"

    decision = "buy={} block={}".format(buy, block)
    if buy != "YES":
        decision += ' why="{}"'.format(_quote(block_reason(block, row, txt)))
    note = restriction_note(row, txt)
    if note:
        decision += ' exec="{}"'.format(_quote(note))
    # build_market_reports stamps the full-list position before it splits
    # the frame by market: in the OTC report the 5th OTC row can be list #23,
    # which is exactly what block=rank means (P1-7). Omitted where it equals
    # the rank (every row of the ALL report): it would only add noise.
    pos = _list_pos(row, rank)
    where = "rank={} list#={}".format(rank, pos) if pos else "rank={}".format(rank)

    return (
        "{where} {sid} {name} | {decision} | "
        "buyPx={buypx} stopPx={stoppx} riskPct={risk} | date={date} | "
        "close={close} | score={score} | gain3m={gain3m} | "
        "tightness={tight} | dryup={dry} | bias={bias} | "
        "MA20={ma20} MA60={ma60} | resist={res} support={sup} | "
        "VP=[{vp1},{vp2},{vp3}] | gapSup={gsup} gapRes={gres} | "
        "round={rnd} | supGap%={sgp} resGap%={rgp} | squeeze={sq}"
    ).format(
        where=where, sid=g("Stock_ID"), name=g("Stock_Name"),
        decision=decision,
        buypx=g("Suggested_Buy_Price"), stoppx=g("Strict_Stop_Loss"),
        risk=g("Risk_Pct"), date=g("Data_Date"),
        close=g("Close_Price"),
        score=g("Explosion_Score"), gain3m=g("Gain_3M_Pct"), tight=g("Range_Tightness"),
        dry=g("Volume_Dryup"), bias=g("Volume_Bias"),
        ma20=g("MA20"), ma60=g("MA60"), res=g("Resist_60H"), sup=g("Support_60L"),
        vp1=g("VP_Zone1"), vp2=g("VP_Zone2"), vp3=g("VP_Zone3"),
        gsup=g("Gap_Up_Sup"), gres=g("Gap_Dn_Res"), rnd=g("Round_Level"),
        sgp=g("Sup_Gap_Pct"), rgp=g("Res_Gap_Pct"),
        sq="YES" if row.get("Squeeze") else "NO",
    )


MAX_STOCKS_IN_PROMPT = 10   # keep token cost low on free tier


def build_prompt(df: pd.DataFrame) -> str:
    """The user prompt: the rows and the scope. SYSTEM_INSTRUCTION is NOT
    repeated here -- both providers already send it as the system message
    (gemini_client._call_gemini / _call_groq); the second copy cost about
    5k characters a call for nothing."""
    if df is None or df.empty:
        return ""

    txt = report_text()
    top = df.head(MAX_STOCKS_IN_PROMPT)
    # rank is 1-based and comes from the SHIPPED order, which is what the buy
    # rule itself scored (mark_buy_ready reads position, not any column) -- so
    # the model is told the same ordering the user sees.
    lines = [_row_to_line(row, i, txt)
             for i, (_, row) in enumerate(top.iterrows(), 1)]
    table_text = "\n".join(lines)

    # Counted with the same reading as each line's buy= (NaN and "False" are
    # not a yes), so the scope sentence cannot disagree with the rows.
    n_buy = sum(1 for v in top["Buy_Ready"] if _truthy(v)) \
        if "Buy_Ready" in top.columns else -1
    # The scope used to live only in this header, where it reads as decoration.
    # Repeating it as an instruction is the point of F23: the model must not
    # write as if it had seen the market, or the rest of the shortlist.
    scope = ("You are seeing rows 1-{} of {} shortlisted names, already in the "
             "backend's order.".format(len(top), len(df)))
    if n_buy >= 0:
        scope += (" {} of these {} are buy=YES; the rest are shown so you can "
                  "explain the refusal, quoting each why= text.".format(
                      n_buy, len(top)))

    prompt = (
        "=== Screened Stocks (top {} of {}) ===\n".format(len(top), len(df))
        + table_text
        + "\n\n=== End of Data ===\n"
        + scope + "\n"
        + "Now produce the report in Traditional Chinese, keeping this order "
          "and following every rule of the system instruction."
    )
    return prompt


def _template_rows(df, txt):
    lines = [txt["template_header"] + "\n"]
    flag_names = txt.get("flags") or {}
    for rank, (_, row) in enumerate(df.head(MAX_STOCKS_IN_PROMPT).iterrows(), 1):
        def g(k, d="-"):
            v = _s(row.get(k))
            return v if v else d

        cond_flags = [flag_names.get(k, k) for k in
                      ("Cond_A", "Cond_C", "Cond_B", "MA_Bull_Align",
                       "Donchian_Break", "MACD_Cross", "Near_52W_High",
                       "RS_Strong")
                      if _truthy(row.get(k))]
        flags_str = (txt["flags_sep"].join(cond_flags) if cond_flags
                     else txt["flags_none"])

        # Same rule as the AI prompt (F23): a name the rule refuses must not be
        # printed as if the entry reference below were an instruction to buy,
        # and the refusal is given in words, not as a raw code (P1-7).
        if "Buy_Ready" not in row:
            verdict = txt["verdict_unevaluated"]
        elif _truthy(row.get("Buy_Ready")):
            verdict = txt["verdict_ok"]
        else:
            verdict = _fmt(txt["verdict_no"], reason=block_reason(
                g("Buy_Block", ""), row, txt))

        # The per-market template numbers its own rows; "#6 ... not in the
        # top 20" reads as a contradiction unless the full-list position the
        # rule ranks is shown next to it.
        pos = _list_pos(row, rank)
        list_pos = _fmt(txt.get("row_pos", ""), pos=pos) if pos else ""
        line = _fmt(txt["row"], rank=rank, verdict=verdict, list_pos=list_pos,
                    sid=g("Stock_ID"), name=g("Stock_Name"),
                    close=g("Close_Price"), score=g("Explosion_Score"),
                    buy=g("Suggested_Buy_Price"), stop=g("Strict_Stop_Loss"),
                    flags=flags_str,
                    sup=g("Support_60L"), res=g("Resist_60H"),
                    sgp=g("Sup_Gap_Pct"), rgp=g("Res_Gap_Pct"))
        note = restriction_note(row, txt)
        if note:
            line += _fmt(txt["row_exec"], note=note)
        lines.append(line)
    return "\n".join(lines)


def build_local_report(df: pd.DataFrame) -> str:
    """Plain-text report built locally without any API call -- the template
    the phone and the GUI show when no AI answered. Its first line is
    report_text()['template_header'], which says it is a template. Never
    raises: a broken text file degrades to the ASCII fallback, and anything
    worse to a bare list of names."""
    txt = report_text()
    if df is None or df.empty:
        return txt["empty"]
    try:
        return _template_rows(df, txt)
    except Exception:
        pass
    try:
        return _template_rows(df, report_text(path=Path("<fallback>")))
    except Exception:
        names = []
        try:
            for _, row in df.head(MAX_STOCKS_IN_PROMPT).iterrows():
                names.append("{} {}".format(_s(row.get("Stock_ID")),
                                            _s(row.get("Stock_Name"))))
        except Exception:
            pass
        return "\n".join([_FALLBACK_TEXT["template_header"], ""] + names)

import pandas as pd

# All prompt text is written in English to keep this file pure ASCII.
# The instruction explicitly asks Gemini to answer in Traditional Chinese,
# so the generated report itself will be in Traditional Chinese.

SYSTEM_INSTRUCTION = (
    "You are a senior Taiwan stock market chip analyst. "
    "You will receive rows the backend has ALREADY screened, ranked and judged. "
    "Each row contains technical and chip indicators plus the backend's own "
    "buy decision. "
    "Explanation of the columns:\n"
    "- rank: the backend's shipped order. Row 1 is its first pick.\n"
    "- buy: the backend's decision. YES = the trading rule buys this name "
    "tomorrow. NO = it refuses, and block says why "
    "(regime = market gate shut, stale = the row's data is not from the latest "
    "session, integrity = the data failed its own checks, held = already in a "
    "position from an earlier signal, quality/market/rank = fails a selection "
    "filter, no_rule = this scan mode has no validated buy rule at all, "
    "unknown = holding state could not be determined).\n"
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
    "rule is not buying it and why, using its block reason; you may still "
    "describe what would have to change.\n"
    "3. You are given only the top rows of a longer shortlist, and the "
    "shortlist itself is only what today's scan produced. Do not claim it is "
    "the whole market or the best available names, and do not comment on "
    "anything that is not in the table.\n"
    "4. Do NOT give financial advice; frame everything as technical "
    "observation only."
)


def _row_to_line(row: pd.Series, rank: int) -> str:
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
        buy = "YES" if row.get("Buy_Ready") else "NO"
        block = g("Buy_Block", "") or "-"
    else:
        # mark_buy_ready did not run for this frame: unevaluated is not refused.
        buy, block = "UNKNOWN", "not_evaluated"

    return (
        "rank={rank} {sid} {name} | buy={buy} block={block} | "
        "buyPx={buypx} stopPx={stoppx} riskPct={risk} | date={date} | "
        "close={close} | score={score} | gain3m={gain3m} | "
        "tightness={tight} | dryup={dry} | bias={bias} | "
        "MA20={ma20} MA60={ma60} | resist={res} support={sup} | "
        "VP=[{vp1},{vp2},{vp3}] | gapSup={gsup} gapRes={gres} | "
        "round={rnd} | supGap%={sgp} resGap%={rgp} | squeeze={sq}"
    ).format(
        rank=rank, sid=g("Stock_ID"), name=g("Stock_Name"),
        buy=buy, block=block,
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
    if df is None or df.empty:
        return ""

    top = df.head(MAX_STOCKS_IN_PROMPT)
    # rank is 1-based and comes from the SHIPPED order, which is what the buy
    # rule itself scored (mark_buy_ready reads position, not any column) -- so
    # the model is told the same ordering the user sees.
    lines = [_row_to_line(row, i)
             for i, (_, row) in enumerate(top.iterrows(), 1)]
    table_text = "\n".join(lines)

    n_buy = int(top["Buy_Ready"].fillna(False).astype(bool).sum()) \
        if "Buy_Ready" in top.columns else -1
    # The scope used to live only in this header, where it reads as decoration.
    # Repeating it as an instruction is the point of F23: the model must not
    # write as if it had seen the market, or the rest of the shortlist.
    scope = ("You are seeing rows 1-{} of {} shortlisted names, already in the "
             "backend's order.".format(len(top), len(df)))
    if n_buy >= 0:
        scope += (" {} of these {} are buy=YES; the rest are shown so you can "
                  "explain the refusal.".format(n_buy, len(top)))

    prompt = (
        SYSTEM_INSTRUCTION
        + "\n\n=== Screened Stocks (top {} of {}) ===\n".format(len(top), len(df))
        + table_text
        + "\n\n=== End of Data ===\n"
        + scope + "\n"
        + "Now produce the report in Traditional Chinese, keeping this order."
    )
    return prompt


def build_local_report(df: pd.DataFrame) -> str:
    """Fallback plain-text report generated locally without any API call."""
    if df is None or df.empty:
        return "（本次掃描無符合條件標的）"

    lines = ["【本地報告 — Gemini API 不可用時自動產生】\n"]
    for rank, (_, row) in enumerate(df.head(MAX_STOCKS_IN_PROMPT).iterrows(), 1):
        def g(k, d="-"):
            v = row.get(k)
            return d if (v is None or (isinstance(v, float) and __import__("math").isnan(v))) else str(v)

        buy  = g("Suggested_Buy_Price")
        stop = g("Strict_Stop_Loss")
        score = g("Explosion_Score")
        cond_flags = []
        if row.get("Cond_A"):      cond_flags.append("箱縮")
        if row.get("Cond_C"):      cond_flags.append("吸籌")
        if row.get("Cond_B"):      cond_flags.append("大戶加碼")
        if row.get("MA_Bull_Align"): cond_flags.append("MA多頭")
        if row.get("Donchian_Break"): cond_flags.append("Donchian突破")
        if row.get("MACD_Cross"):   cond_flags.append("MACD金叉")
        if row.get("Near_52W_High"): cond_flags.append("近52週高點")
        if row.get("RS_Strong"):    cond_flags.append("RS強勢")
        flags_str = "、".join(cond_flags) if cond_flags else "無明顯訊號"

        # Same rule as the AI prompt (F23): a name the rule refuses must not be
        # printed as if the entry reference below were an instruction to buy.
        if "Buy_Ready" not in row:
            verdict = "買進判定：未評估"
        elif row.get("Buy_Ready"):
            verdict = "買進判定：符合規則"
        else:
            verdict = "買進判定：不買進（{}）".format(
                g("Buy_Block", "-") or "-")

        lines.append(
            "#{rank} {sid} {name}  收盤 {close}\n"
            "   {verdict}\n"
            "   爆發分={score}  進場參考={buy}  停損={stop}\n"
            "   訊號：{flags}\n"
            "   支撐={sup}  壓力={res}  距支撐={sgp}%  距壓力={rgp}%\n".format(
                rank=rank, verdict=verdict,
                sid=g("Stock_ID"), name=g("Stock_Name"),
                close=g("Close_Price"), score=score,
                buy=buy, stop=stop, flags=flags_str,
                sup=g("Support_60L"), res=g("Resist_60H"),
                sgp=g("Sup_Gap_Pct"), rgp=g("Res_Gap_Pct"),
            )
        )
    return "\n".join(lines)

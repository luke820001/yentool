import pandas as pd

# All prompt text is written in English to keep this file pure ASCII.
# The instruction explicitly asks Gemini to answer in Traditional Chinese,
# so the generated report itself will be in Traditional Chinese.

SYSTEM_INSTRUCTION = (
    "You are a senior Taiwan stock market chip analyst. "
    "You will receive a table of stocks that passed a pre-explosion screening. "
    "Each row contains technical and chip indicators. "
    "Explanation of the columns:\n"
    "- Explosion_Score: 0 to 100, higher means closer to a breakout.\n"
    "- gain3m: price change percent over the last ~3 months (momentum already realized).\n"
    "- Range_Tightness: 20-day box width ratio, smaller means tighter consolidation.\n"
    "- Volume_Dryup: today volume divided by 20-day average volume, "
    "smaller means volume is drying up.\n"
    "- Volume_Bias: ratio of up-day volume over total, above 0.6 implies accumulation.\n"
    "- MA20 / MA60: moving average support or resistance levels.\n"
    "- Resist_60H / Support_60L: 60-day high (resistance) and low (support).\n"
    "- VP_Zone1..3: top volume-profile price zones (main holding cost areas).\n"
    "- Gap_Up_Sup / Gap_Dn_Res: gap support and gap resistance levels.\n"
    "- Round_Level: nearest psychological round-number level.\n"
    "- Sup_Gap_Pct / Res_Gap_Pct: distance percent to support and resistance.\n"
    "- Squeeze: YES means price is squeezed between strong support and weak resistance.\n\n"
    "Write a concise daily report in TRADITIONAL CHINESE (zh-TW). "
    "For each stock give: a one-line verdict, the key trigger to watch tomorrow "
    "(volume multiple, breakout price, support to hold), and a risk note. "
    "Rank the stocks from most to least likely to explode. "
    "Do NOT give financial advice; frame everything as technical observation only."
)


def _row_to_line(row: pd.Series) -> str:
    def g(key, default="NA"):
        val = row.get(key, default)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return str(val)

    return (
        "{sid} {name} | close={close} | score={score} | gain3m={gain3m} | "
        "tightness={tight} | dryup={dry} | bias={bias} | "
        "MA20={ma20} MA60={ma60} | resist={res} support={sup} | "
        "VP=[{vp1},{vp2},{vp3}] | gapSup={gsup} gapRes={gres} | "
        "round={rnd} | supGap%={sgp} resGap%={rgp} | squeeze={sq}"
    ).format(
        sid=g("Stock_ID"), name=g("Stock_Name"), close=g("Close_Price"),
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
    lines = [_row_to_line(row) for _, row in top.iterrows()]
    table_text = "\n".join(lines)

    prompt = (
        SYSTEM_INSTRUCTION
        + "\n\n=== Screened Stocks (top {} of {}) ===\n".format(len(top), len(df))
        + table_text
        + "\n\n=== End of Data ===\n"
        + "Now produce the report in Traditional Chinese."
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

        lines.append(
            "#{rank} {sid} {name}  收盤 {close}\n"
            "   爆發分={score}  進場參考={buy}  停損={stop}\n"
            "   訊號：{flags}\n"
            "   支撐={sup}  壓力={res}  距支撐={sgp}%  距壓力={rgp}%\n".format(
                rank=rank,
                sid=g("Stock_ID"), name=g("Stock_Name"),
                close=g("Close_Price"), score=score,
                buy=buy, stop=stop, flags=flags_str,
                sup=g("Support_60L"), res=g("Resist_60H"),
                sgp=g("Sup_Gap_Pct"), rgp=g("Res_Gap_Pct"),
            )
        )
    return "\n".join(lines)

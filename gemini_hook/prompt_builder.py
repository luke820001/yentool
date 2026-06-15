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
        "{sid} {name} | close={close} | score={score} | "
        "tightness={tight} | dryup={dry} | bias={bias} | "
        "MA20={ma20} MA60={ma60} | resist={res} support={sup} | "
        "VP=[{vp1},{vp2},{vp3}] | gapSup={gsup} gapRes={gres} | "
        "round={rnd} | supGap%={sgp} resGap%={rgp} | squeeze={sq}"
    ).format(
        sid=g("Stock_ID"), name=g("Stock_Name"), close=g("Close_Price"),
        score=g("Explosion_Score"), tight=g("Range_Tightness"),
        dry=g("Volume_Dryup"), bias=g("Volume_Bias"),
        ma20=g("MA20"), ma60=g("MA60"), res=g("Resist_60H"), sup=g("Support_60L"),
        vp1=g("VP_Zone1"), vp2=g("VP_Zone2"), vp3=g("VP_Zone3"),
        gsup=g("Gap_Up_Sup"), gres=g("Gap_Dn_Res"), rnd=g("Round_Level"),
        sgp=g("Sup_Gap_Pct"), rgp=g("Res_Gap_Pct"),
        sq="YES" if row.get("Squeeze") else "NO",
    )


def build_prompt(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""

    lines = [_row_to_line(row) for _, row in df.iterrows()]
    table_text = "\n".join(lines)

    prompt = (
        SYSTEM_INSTRUCTION
        + "\n\n=== Screened Stocks ("
        + str(len(df))
        + " rows) ===\n"
        + table_text
        + "\n\n=== End of Data ===\n"
        + "Now produce the report in Traditional Chinese."
    )
    return prompt

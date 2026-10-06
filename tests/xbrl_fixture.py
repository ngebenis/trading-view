"""Pembuat file XBRL tiruan dengan struktur seperti laporan IDX (untuk test)."""
import io
import zipfile
from datetime import date

NS = ('xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
      'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
      'xmlns:idx-cor="http://www.idx.co.id/xbrl/taxonomy/2020-01-01/cor" '
      'xmlns:idx-dei="http://www.idx.co.id/xbrl/taxonomy/2020-01-01/dei"')

INSTANT_TAGS = {"Assets", "CurrentAssets", "Liabilities", "CurrentLiabilities", "Equity",
                "EquityAttributableToEquityOwnersOfParentEntity", "CashAndCashEquivalents", "DepositsFromCustomers"}


def make_xbrl(end: date, current: dict, prior: dict | None = None, prior_year_end: dict | None = None,
              currency: str = "IDR", entity: str | None = "TEST", segment_noise: bool = True,
              as_zip: bool = False) -> bytes:
    """`current`/`prior` berisi {tag: nilai}; tag neraca memakai konteks instant, sisanya durasi YTD."""
    start = date(end.year, 1, 1)
    p_end = date(end.year - 1, end.month, end.day if end.month != 2 else 28)
    p_start = date(end.year - 1, 1, 1)
    ye = date(end.year - 1, 12, 31)
    ctx = [
        f'<xbrli:context id="CurrentYearInstant"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>{end}</xbrli:instant></xbrli:period></xbrli:context>',
        f'<xbrli:context id="CurrentYearDuration"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>',
        # Durasi 3 bulan terakhir (bukan YTD) — tidak boleh dipilih untuk Q2/Q3.
        f'<xbrli:context id="CurrentQuarterDuration"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{date(end.year, max(end.month - 2, 1), 1)}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>',
        f'<xbrli:context id="PriorYearDuration"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{p_start}</xbrli:startDate><xbrli:endDate>{p_end}</xbrli:endDate></xbrli:period></xbrli:context>',
        f'<xbrli:context id="PriorEndYearInstant"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>{ye}</xbrli:instant></xbrli:period></xbrli:context>',
        f'<xbrli:context id="CurrentYearInstant_Segment"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="idx-cor:SegmentAxis">idx-cor:MiningMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>{end}</xbrli:instant></xbrli:period></xbrli:context>',
        f'<xbrli:context id="CurrentYearDuration_Segment"><xbrli:entity><xbrli:identifier scheme="x">T</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="idx-cor:SegmentAxis">idx-cor:MiningMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>',
    ]
    unit = f'<xbrli:unit id="{currency}"><xbrli:measure>iso4217:{currency}</xbrli:measure></xbrli:unit>' \
           '<xbrli:unit id="Shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>' \
           f'<xbrli:unit id="{currency}PerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:{currency}</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'

    def fact(tag, ctx_id, value):
        u = "Shares" if tag == "NumberOfSharesOutstanding" else (f"{currency}PerShare" if "PerShare" in tag else currency)
        return f'<idx-cor:{tag} contextRef="{ctx_id}" unitRef="{u}" decimals="-3">{value}</idx-cor:{tag}>'

    facts = []
    if segment_noise:  # nilai segmen dan kuartalan ditaruh DULUAN agar urutan tidak menyelamatkan parser
        for tag, value in current.items():
            seg = "CurrentYearInstant_Segment" if tag in INSTANT_TAGS else "CurrentYearDuration_Segment"
            facts.append(fact(tag, seg, value / 10))
            if tag not in INSTANT_TAGS and end.month > 3:  # untuk Q1, kuartal = YTD
                facts.append(fact(tag, "CurrentQuarterDuration", value / 3))
    for tag, value in current.items():
        facts.append(fact(tag, "CurrentYearInstant" if tag in INSTANT_TAGS else "CurrentYearDuration", value))
    for tag, value in (prior or {}).items():
        facts.append(fact(tag, "PriorYearDuration", value))
    for tag, value in (prior_year_end or {}).items():
        facts.append(fact(tag, "PriorEndYearInstant", value))
    facts.append('<idx-cor:Assets contextRef="CurrentYearInstant" unitRef="IDR" xsi:nil="true"/>')
    if entity:
        facts.append(f'<idx-dei:EntityCode contextRef="CurrentYearDuration">{entity}</idx-dei:EntityCode>')
    xml = f'<?xml version="1.0" encoding="utf-8"?><xbrli:xbrl {NS}>{"".join(ctx)}{unit}{"".join(facts)}</xbrli:xbrl>'.encode()
    if not as_zip:
        return xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Taxonomy/readme.txt", "x")
        z.writestr("instance.xbrl", xml)
    return buf.getvalue()


GENERAL = {
    "Assets": 1_000_000_000_000, "CurrentAssets": 400_000_000_000, "Liabilities": 600_000_000_000,
    "CurrentLiabilities": 200_000_000_000, "Equity": 400_000_000_000,
    "EquityAttributableToEquityOwnersOfParentEntity": 380_000_000_000,
    "CashAndCashEquivalents": 50_000_000_000, "SalesAndRevenue": 300_000_000_000, "GrossProfit": 90_000_000_000,
    "ProfitLossBeforeIncomeTax": 40_000_000_000, "ProfitLoss": 32_000_000_000,
    "ProfitLossAttributableToParentEntity": 30_000_000_000,
    "NetCashFlowsReceivedFromUsedInOperatingActivities": 45_000_000_000,
    "PaymentsForAcquisitionOfPropertyPlantAndEquipment": 10_000_000_000,
    "BasicEarningsLossPerShare": 15,  # 30 M / 2 M lembar
}
GENERAL_PRIOR = {"SalesAndRevenue": 250_000_000_000, "ProfitLossAttributableToParentEntity": 24_000_000_000}

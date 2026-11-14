"""3GPP band tables and channel-number <-> frequency conversions (LTE, NR, UMTS, GSM)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Family

# --------------------------------------------------------------------------- LTE (TS 36.101 Table 5.7.3-1)

# band: (duplex, name, F_DL_low, N_Offs_DL, N_DL_high, F_UL_low, N_Offs_UL, N_UL_high)
_LTE: dict[int, tuple] = {
    1: ("FDD", "2100 IMT", 2110, 0, 599, 1920, 18000, 18599),
    2: ("FDD", "1900 PCS", 1930, 600, 1199, 1850, 18600, 19199),
    3: ("FDD", "1800 DCS", 1805, 1200, 1949, 1710, 19200, 19949),
    4: ("FDD", "AWS-1", 2110, 1950, 2399, 1710, 19950, 20399),
    5: ("FDD", "850 CLR", 869, 2400, 2649, 824, 20400, 20649),
    7: ("FDD", "2600 IMT-E", 2620, 2750, 3449, 2500, 20750, 21449),
    8: ("FDD", "900 E-GSM", 925, 3450, 3799, 880, 21450, 21799),
    11: ("FDD", "1500 Lower", 1475.9, 4750, 4949, 1427.9, 22750, 22949),
    12: ("FDD", "700 Lower A/B/C", 729, 5010, 5179, 699, 23010, 23179),
    13: ("FDD", "700 Upper C", 746, 5180, 5279, 777, 23180, 23279),
    14: ("FDD", "700 FirstNet", 758, 5280, 5379, 788, 23280, 23379),
    17: ("FDD", "700 Lower B/C", 734, 5730, 5849, 704, 23730, 23849),
    18: ("FDD", "800 Japan Lower", 860, 5850, 5999, 815, 23850, 23999),
    19: ("FDD", "800 Japan Upper", 875, 6000, 6149, 830, 24000, 24149),
    20: ("FDD", "800 DD", 791, 6150, 6449, 832, 24150, 24449),
    21: ("FDD", "1500 Upper", 1495.9, 6450, 6599, 1447.9, 24450, 24599),
    25: ("FDD", "1900 Ext. PCS", 1930, 8040, 8689, 1850, 26040, 26689),
    26: ("FDD", "850 Ext.", 859, 8690, 9039, 814, 26690, 27039),
    28: ("FDD", "700 APT", 758, 9210, 9659, 703, 27210, 27659),
    29: ("SDL", "700 Lower D/E", 717, 9660, 9769, None, None, None),
    30: ("FDD", "2300 WCS", 2350, 9770, 9869, 2305, 27660, 27759),
    31: ("FDD", "450", 462.5, 9870, 9919, 452.5, 27760, 27809),
    32: ("SDL", "1500 L-Band", 1452, 9920, 10359, None, None, None),
    34: ("TDD", "TD 2000", 2010, 36200, 36349, None, None, None),
    38: ("TDD", "TD 2600", 2570, 37750, 38249, None, None, None),
    39: ("TDD", "TD 1900+", 1880, 38250, 38649, None, None, None),
    40: ("TDD", "TD 2300", 2300, 38650, 39649, None, None, None),
    41: ("TDD", "TD 2500", 2496, 39650, 41589, None, None, None),
    42: ("TDD", "TD 3500", 3400, 41590, 43589, None, None, None),
    43: ("TDD", "TD 3700", 3600, 43590, 45589, None, None, None),
    46: ("TDD", "TD 5200 LAA", 5150, 46790, 54539, None, None, None),
    48: ("TDD", "TD 3600 CBRS", 3550, 55240, 56739, None, None, None),
    65: ("FDD", "2100 Ext.", 2110, 65536, 66435, 1920, 131072, 131971),
    66: ("FDD", "AWS-3", 2110, 66436, 67335, 1710, 131972, 132671),
    67: ("SDL", "700 EU", 738, 67336, 67535, None, None, None),
    68: ("FDD", "700 ME", 753, 67536, 67835, 698, 132672, 132971),
    69: ("SDL", "2600", 2570, 67836, 68335, None, None, None),
    70: ("FDD", "AWS-4", 1995, 68336, 68585, 1695, 132972, 133121),
    71: ("FDD", "600", 617, 68586, 68935, 663, 133122, 133471),
    72: ("FDD", "450 PMR", 461, 68936, 68985, 451, 133472, 133521),
    74: ("FDD", "1500 L-Band", 1475, 69036, 69465, 1427, 133572, 134001),
    75: ("SDL", "1500", 1432, 69466, 70315, None, None, None),
    85: ("FDD", "700 Ext. Lower", 728, 70366, 70545, 698, 134002, 134181),
    87: ("FDD", "410", 420, 70546, 70595, 410, 134182, 134231),
    88: ("FDD", "410 Ext.", 422, 70596, 70645, 412, 134232, 134281),
}


@dataclass(frozen=True)
class LTEBand:
    number: int
    duplex: str
    name: str
    dl_low: float
    dl_offset: int
    dl_high_channel: int
    ul_low: float | None
    ul_offset: int | None
    ul_high_channel: int | None

    @property
    def dl_channels(self) -> range:
        return range(self.dl_offset, self.dl_high_channel + 1)

    @property
    def dl_high(self) -> float:
        return self.dl_low + 0.1 * (self.dl_high_channel - self.dl_offset) + 0.1

    def dl_mhz(self, earfcn: int) -> float:
        return round(self.dl_low + 0.1 * (earfcn - self.dl_offset), 3)

    def earfcn(self, mhz: float) -> int:
        return self.dl_offset + round((mhz - self.dl_low) * 10)


LTE_BANDS: dict[int, LTEBand] = {n: LTEBand(n, *v) for n, v in _LTE.items()}


def lte_band_for_earfcn(earfcn: int) -> LTEBand | None:
    for band in LTE_BANDS.values():
        if band.dl_offset <= earfcn <= band.dl_high_channel:
            return band
    return None


def lte_ul_band_for_earfcn(earfcn: int) -> LTEBand | None:
    for band in LTE_BANDS.values():
        if band.ul_offset is not None and band.ul_offset <= earfcn <= band.ul_high_channel:
            return band
    return None


# --------------------------------------------------------------------------- NR (TS 38.104 / 38.101)

_NR: dict[int, tuple] = {
    1: ("FDD", 2110, 2170, "2100"), 2: ("FDD", 1930, 1990, "1900 PCS"), 3: ("FDD", 1805, 1880, "1800"),
    5: ("FDD", 869, 894, "850"), 7: ("FDD", 2620, 2690, "2600"), 8: ("FDD", 925, 960, "900"),
    12: ("FDD", 729, 746, "700 Lower"), 14: ("FDD", 758, 768, "700 FirstNet"), 20: ("FDD", 791, 821, "800 DD"),
    25: ("FDD", 1930, 1995, "1900 Ext."), 26: ("FDD", 859, 894, "850 Ext."), 28: ("FDD", 758, 803, "700 APT"),
    38: ("TDD", 2570, 2620, "TD 2600"), 40: ("TDD", 2300, 2400, "TD 2300"), 41: ("TDD", 2496, 2690, "TD 2500"),
    48: ("TDD", 3550, 3700, "CBRS"), 66: ("FDD", 2110, 2200, "AWS-3"), 70: ("FDD", 1995, 2020, "AWS-4"),
    71: ("FDD", 617, 652, "600"), 75: ("SDL", 1432, 1517, "1500 SDL"), 77: ("TDD", 3300, 4200, "C-Band"),
    78: ("TDD", 3300, 3800, "3500"), 79: ("TDD", 4400, 5000, "4700"), 90: ("TDD", 2496, 2690, "TD 2500 alt"),
    257: ("TDD", 26500, 29500, "28 GHz"), 258: ("TDD", 24250, 27500, "26 GHz"), 260: ("TDD", 37000, 40000, "39 GHz"),
    261: ("TDD", 27500, 28350, "28 GHz US"),
}


@dataclass(frozen=True)
class NRBand:
    number: int
    duplex: str
    dl_low: float
    dl_high: float
    name: str

    @property
    def width(self) -> float:
        return self.dl_high - self.dl_low


NR_BANDS: dict[int, NRBand] = {n: NRBand(n, *v) for n, v in _NR.items()}


def nr_arfcn_to_mhz(n: int) -> float | None:
    if 0 <= n < 600_000:
        return round(0.005 * n, 4)
    if 600_000 <= n < 2_016_667:
        return round(3000 + 0.015 * (n - 600_000), 4)
    if 2_016_667 <= n <= 3_279_165:
        return round(24_250.08 + 0.06 * (n - 2_016_667), 4)
    return None


def mhz_to_nr_arfcn(f: float) -> int | None:
    if 0 <= f < 3000:
        return round(f / 0.005)
    if 3000 <= f < 24_250.08:
        return 600_000 + round((f - 3000) / 0.015)
    if 24_250.08 <= f <= 100_000:
        return 2_016_667 + round((f - 24_250.08) / 0.06)
    return None


def nr_candidate_bands(arfcn: int) -> list[NRBand]:
    f = nr_arfcn_to_mhz(arfcn)
    if f is None:
        return []
    return sorted((b for b in NR_BANDS.values() if b.dl_low <= f <= b.dl_high), key=lambda b: (b.width, b.number))


def is_north_america(mcc: int | None) -> bool:
    return mcc is not None and (310 <= mcc <= 316 or mcc == 302)


def is_americas(mcc: int | None) -> bool:
    return mcc is not None and (300 <= mcc <= 399 or 700 <= mcc <= 799)


def nr_likely_band(arfcn: int, mcc: int | None = None) -> NRBand | None:
    cands = nr_candidate_bands(arfcn)
    if not cands:
        return None
    preferred = [77, 66, 25, 41, 71, 5, 2, 260, 261] if is_north_america(mcc) else [78, 1, 3, 7, 8, 28, 20, 41, 258, 257]
    for number in preferred:
        for c in cands:
            if c.number == number:
                return c
    return cands[0]


# --------------------------------------------------------------------------- UMTS / GSM

# band: (dl offset MHz, uarfcn range, duplex spacing MHz, name)
_UMTS = [
    (1, 0, range(10562, 10839), 190, "2100"), (2, 0, range(9662, 9939), 80, "1900 PCS"),
    (3, 1575, range(1162, 1514), 95, "1800"), (4, 1805, range(1537, 1739), 400, "AWS"),
    (5, 0, range(4357, 4459), 45, "850"), (8, 340, range(2937, 3089), 45, "900"),
]


def roman(n: int) -> str:
    out, table = "", [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    for v, s in table:
        while n >= v:
            out, n = out + s, n - v
    return out


# --------------------------------------------------------------------------- unified decode


@dataclass
class ChannelInfo:
    family: Family
    channel: int
    band: int | None = None
    band_name: str | None = None
    duplex: str | None = None
    dl_mhz: float | None = None
    ul_mhz: float | None = None
    ul_channel: int | None = None
    alternatives: list[int] = field(default_factory=list)

    @property
    def band_label(self) -> str | None:
        if self.band is None:
            return None
        if self.family is Family.NR:
            return f"n{self.band}"
        if self.family is Family.LTE:
            return f"B{self.band}"
        if self.family is Family.UMTS:
            return f"Band {roman(self.band)}"
        return self.band_name or str(self.band)


def channel_info(family: Family, channel: int, mcc: int | None = None, reported_band: int | None = None) -> ChannelInfo | None:
    if family is Family.LTE:
        band = lte_band_for_earfcn(channel)
        if band:
            ul = None
            if band.duplex == "FDD" and band.ul_offset is not None:
                ul_ch = band.ul_offset + (channel - band.dl_offset)
                ul = ul_ch if ul_ch <= band.ul_high_channel else None
            ul_mhz = band.dl_mhz(channel) if band.duplex == "TDD" else (
                round(band.ul_low + 0.1 * (channel - band.dl_offset), 3) if ul is not None else None)
            return ChannelInfo(family, channel, band.number, band.name, band.duplex, band.dl_mhz(channel), ul_mhz, ul)
        ulb = lte_ul_band_for_earfcn(channel)
        if ulb:  # an uplink EARFCN was supplied
            dl = ulb.dl_offset + (channel - ulb.ul_offset)
            return ChannelInfo(family, dl, ulb.number, ulb.name, ulb.duplex, ulb.dl_mhz(dl),
                               round(ulb.ul_low + 0.1 * (channel - ulb.ul_offset), 3), channel)
        return None
    if family is Family.NR:
        f = nr_arfcn_to_mhz(channel)
        if f is None:
            return None
        cands = nr_candidate_bands(channel)
        chosen = next((c for c in cands if c.number == reported_band), None) if reported_band else None
        chosen = chosen or (NR_BANDS.get(reported_band) if reported_band else None) or nr_likely_band(channel, mcc)
        return ChannelInfo(family, channel, chosen.number if chosen else None, chosen.name if chosen else None,
                           chosen.duplex if chosen else None, f, f if chosen and chosen.duplex == "TDD" else None,
                           None, [c.number for c in cands if not chosen or c.number != chosen.number])
    if family is Family.UMTS:
        for number, off, rng, dup, name in _UMTS:
            if channel in rng:
                dl = round(off + 0.2 * channel, 1)
                return ChannelInfo(family, channel, number, name, "FDD", dl, round(dl - dup, 1))
        return None
    if family is Family.GSM:
        return gsm_channel_info(channel, mcc)
    return None


def gsm_channel_info(n: int, mcc: int | None = None) -> ChannelInfo | None:
    if 0 <= n <= 124:
        ul, name, band = 890 + 0.2 * n, "E-GSM 900" if n == 0 else "P-GSM 900", 900
        dl = ul + 45
    elif 128 <= n <= 251:
        ul, name, band = 824.2 + 0.2 * (n - 128), "GSM 850", 850
        dl = ul + 45
    elif 512 <= n <= 810 and is_americas(mcc):
        ul, name, band = 1850.2 + 0.2 * (n - 512), "PCS 1900", 1900
        dl = ul + 80
    elif 512 <= n <= 885:
        ul, name, band = 1710.2 + 0.2 * (n - 512), "DCS 1800", 1800
        dl = ul + 95
    elif 955 <= n <= 1023:
        ul, name, band = 890 + 0.2 * (n - 1024), "E-GSM 900" if n >= 975 else "R-GSM 900", 900
        dl = ul + 45
    else:
        return None
    return ChannelInfo(Family.GSM, n, band, name, "FDD", round(dl, 1), round(ul, 1), n)


def gsm_arfcn_for_dl(mhz: float, mcc: int | None = None) -> int | None:
    """Inverse of gsm_channel_info for the downlink frequency."""
    for n in list(range(0, 125)) + list(range(128, 252)) + list(range(512, 886)) + list(range(955, 1024)):
        info = gsm_channel_info(n, mcc)
        if info and info.dl_mhz is not None and abs(info.dl_mhz - mhz) < 0.05:
            return n
    return None


def format_mhz(mhz: float) -> str:
    if mhz >= 6000:
        return f"{mhz / 1000:.3f}".rstrip("0").rstrip(".") + " GHz"
    return f"{mhz:.3f}".rstrip("0").rstrip(".") + " MHz"

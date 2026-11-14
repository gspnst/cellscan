"""Global cell-identity decoding (ECI, NCI, UTRAN CID) per TS 36.413 / 38.413 / 25.401."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Family, format_plmn

LTE_ECI_MAX = (1 << 28) - 1
NR_NCI_MAX = (1 << 36) - 1
DEFAULT_GNB_BITS = 24


@dataclass
class DecodedIdentity:
    family: Family
    raw: int
    site_id: int | None
    local_id: int | None
    site_label: str
    local_label: str
    site_bits: int | None = None
    global_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def hex(self) -> str:
        return f"0x{self.raw:X}"

    @property
    def short(self) -> str:
        if self.site_id is not None and self.local_id is not None:
            prefix = {Family.LTE: "eNB", Family.NR: "gNB", Family.UMTS: "RNC"}.get(self.family, "site")
            return f"{prefix} {self.site_id}·{self.local_id}"
        return f"CID {self.raw}"


def decode(family: Family, cell_id: int, mcc: int | None = None, mnc: int | None = None,
           gnb_bits: int = DEFAULT_GNB_BITS) -> DecodedIdentity:
    plmn = format_plmn(mcc, mnc) if mcc is not None and mnc is not None else None
    if family is Family.LTE:
        d = DecodedIdentity(family, cell_id, cell_id >> 8, cell_id & 0xFF, "eNB ID", "Cell", 20)
        if plmn:
            d.global_id = f"{plmn}-{d.site_id}-{d.local_id}"
        if not 0 <= cell_id <= LTE_ECI_MAX:
            d.warnings.append("ECI exceeds 28 bits")
        return d
    if family is Family.NR:
        bits = min(max(gnb_bits, 22), 32)
        cell_bits = 36 - bits
        d = DecodedIdentity(family, cell_id, cell_id >> cell_bits, cell_id & ((1 << cell_bits) - 1), "gNB ID", "Cell", bits)
        if plmn:
            d.global_id = f"{plmn}-{d.site_id}-{d.local_id}"
        if not 0 <= cell_id <= NR_NCI_MAX:
            d.warnings.append("NCI exceeds 36 bits")
        return d
    if family is Family.UMTS and cell_id > 0xFFFF:
        d = DecodedIdentity(family, cell_id, cell_id >> 16, cell_id & 0xFFFF, "RNC ID", "C-ID", 12)
        if plmn:
            d.global_id = f"{plmn}-{d.site_id}-{d.local_id}"
        return d
    d = DecodedIdentity(family, cell_id, None, cell_id, "Site", "CI")
    if plmn:
        d.global_id = f"{plmn}-{cell_id}"
    if family is Family.GSM and cell_id > 0xFFFF:
        d.warnings.append("GSM CI should fit in 16 bits")
    return d


def compose_eci(enb: int, local: int) -> int:
    return (enb << 8) | (local & 0xFF)


def pci_parts(pci: int, family: Family) -> tuple[int, int] | None:
    """PCI -> (N_ID_1 / SSS group, N_ID_2 / PSS id)."""
    limit = 1007 if family is Family.NR else 503
    if not 0 <= pci <= limit:
        return None
    return pci // 3, pci % 3

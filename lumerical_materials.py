from __future__ import annotations

import re
from typing import Iterable


DEFAULT_SIN_MATERIAL = "Si3N4 (Silicon Nitride) - Luke"
DEFAULT_SI_MATERIAL = "Si (Silicon) - Palik"
DEFAULT_SIO2_MATERIAL = "SiO2 (Glass) - Palik"

MATERIAL_REFERENCE = "docs/lumerical/default_material_database.yaml"

DEFAULT_MATERIAL_DATABASE: dict[str, list[str]] = {
    "palik": [
        "Ag (Silver)",
        "Al (Aluminum)",
        "Al2O3 (Aluminum Oxide)",
        "Au (Gold)",
        "Cr (Chromium)",
        "Cu (Copper)",
        "Fe (Iron)",
        "GaAs (Gallium Arsenide)",
        "Ge (Germanium)",
        "H2O (Water)",
        "In (Indium)",
        "InAs (Indium Arsenide)",
        "InP (Indium Phosphide)",
        "Ni (Nickel)",
        "Pd (Palladium)",
        "Pt (Platinum)",
        "Rh (Rhodium)",
        "Si (Silicon)",
        "SiO2 (Glass)",
        "Sn (Tin)",
        "Ti (Titanium)",
        "TiN (Titanium Nitride)",
        "W (Tungsten)",
    ],
    "crc": [
        "Ag (Silver)",
        "Al (Aluminum)",
        "Au (Gold)",
        "Cr (Chromium)",
        "Cu (Copper)",
        "Fe (Iron)",
        "Ge (Germanium)",
        "Ni (Nickel)",
        "Ta (Tantalum)",
        "Ti (Titanium)",
        "V (Vanadium)",
        "W (Tungsten)",
    ],
    "johnson_christy": [
        "Ag (Silver)",
        "Au (Gold)",
    ],
    "liquid_crystal_li": [
        "5CB - Li",
        "5PCH - Li",
        "6241-000 - Li",
        "E44 - Li",
        "E7 - Li",
        "MLC-6608 - Li",
        "MLC-9200-000 - Li",
        "MLC-9200-100 - Li",
        "TL-216 - Li",
    ],
    "other": [
        "Etch",
        "PEC (Perfect Electrical Conductor)",
        "C (graphene) - Falkovsky (mid-IR)",
        "Graphene",
        "Si3N4 (Silicon Nitride) - Phillip",
        "Si3N4 (Silicon Nitride) - Kischkat",
        "Si3N4 (Silicon Nitride) - Luke",
        "TiO2 (Titanium Dioxide) - Kischkat",
        "TiO2 (Titanium Dioxide) - Sarkar",
        "TiO2 (Titanium Dioxide) - Siefke",
        "TiO2 (Titanium Dioxide) - Devore",
    ],
}

MATERIAL_ALIASES: dict[str, str] = {
    "si": DEFAULT_SI_MATERIAL,
    "silicon": DEFAULT_SI_MATERIAL,
    "soi": DEFAULT_SI_MATERIAL,
    "sio2": DEFAULT_SIO2_MATERIAL,
    "silica": DEFAULT_SIO2_MATERIAL,
    "glass": DEFAULT_SIO2_MATERIAL,
    "sin": DEFAULT_SIN_MATERIAL,
    "si n": DEFAULT_SIN_MATERIAL,
    "si3n4": DEFAULT_SIN_MATERIAL,
    "silicon nitride": DEFAULT_SIN_MATERIAL,
    "nitride": DEFAULT_SIN_MATERIAL,
    "sin luke": "Si3N4 (Silicon Nitride) - Luke",
    "si3n4 luke": "Si3N4 (Silicon Nitride) - Luke",
    "silicon nitride luke": "Si3N4 (Silicon Nitride) - Luke",
    "sin phillip": "Si3N4 (Silicon Nitride) - Phillip",
    "si3n4 phillip": "Si3N4 (Silicon Nitride) - Phillip",
    "sin kischkat": "Si3N4 (Silicon Nitride) - Kischkat",
    "si3n4 kischkat": "Si3N4 (Silicon Nitride) - Kischkat",
    "tio2": "TiO2 (Titanium Dioxide) - Kischkat",
    "titanium dioxide": "TiO2 (Titanium Dioxide) - Kischkat",
    "graphene": "Graphene",
    "pec": "PEC (Perfect Electrical Conductor)",
}


def normalize_material_text(text: str) -> str:
    normalized = text.lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def material_names() -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for values in DEFAULT_MATERIAL_DATABASE.values():
        for name in values:
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _contains_alias(normalized_text: str, alias: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


def resolve_material_from_text(text: str, default_material: str | None = None) -> str | None:
    normalized = normalize_material_text(text)
    if not normalized:
        return default_material

    for alias, material in sorted(MATERIAL_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if _contains_alias(normalized, alias):
            return material

    for material in _known_exact_lumerical_names():
        if normalize_material_text(material) == normalized:
            return material

    return default_material


def _known_exact_lumerical_names() -> Iterable[str]:
    yield DEFAULT_SI_MATERIAL
    yield DEFAULT_SIO2_MATERIAL
    yield from DEFAULT_MATERIAL_DATABASE["other"]

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = Path(os.environ.get("PHOTONICS_OUTPUT_DIR", "data/photonics"))
DEFAULT_TIMEOUT_S = int(os.environ.get("PHOTONICS_LIVE_TIMEOUT_S", "180"))
DEFAULT_HIDE = os.environ.get("LUMERICAL_MODE_HIDE", "true").strip().lower() not in {"0", "false", "no"}
DEFAULT_MODE_WIDTH_NM = float(os.environ.get("PHOTONICS_DEFAULT_WIDTH_NM", "500"))
DEFAULT_DEVICE_LAYER_NM = float(os.environ.get("PHOTONICS_DEFAULT_HEIGHT_NM", "220"))
DEFAULT_WAVELENGTH_NM = float(os.environ.get("PHOTONICS_DEFAULT_WAVELENGTH_NM", "1550"))
DEFAULT_SWEEP_START_NM = float(os.environ.get("PHOTONICS_DEFAULT_SWEEP_START_NM", "400"))
DEFAULT_SWEEP_STOP_NM = float(os.environ.get("PHOTONICS_DEFAULT_SWEEP_STOP_NM", "700"))
DEFAULT_SWEEP_STEP_NM = float(os.environ.get("PHOTONICS_DEFAULT_SWEEP_STEP_NM", "25"))
DEFAULT_RIB_SLAB_NM = float(os.environ.get("PHOTONICS_DEFAULT_RIB_SLAB_NM", "90"))


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_root() -> Path:
    root = DEFAULT_OUTPUT_DIR
    if not root.is_absolute():
        root = BASE_DIR / root
    return ensure_directory(root.resolve())


def version_key(path: Path) -> tuple[int, str]:
    for part in path.parts:
        if part.lower().startswith("v") and part[1:].isdigit():
            return (int(part[1:]), str(path))
    return (0, str(path))


def candidate_lumapi_dirs(explicit: str | None = None) -> list[Path]:
    candidates: list[Path] = []

    raw_values = [
        explicit or "",
        os.environ.get("LUMERICAL_PYTHON_API_DIR", ""),
    ]
    for raw in raw_values:
        raw = raw.strip()
        if raw:
            candidates.append(Path(raw).expanduser())

    install_roots = [
        Path(r"C:\Program Files\Ansys Inc"),
        Path(r"C:\Program Files\ANSYS Inc"),
    ]
    for root in install_roots:
        if not root.exists():
            continue
        for api_dir in sorted(root.glob(r"v*\Lumerical\api\python"), key=version_key, reverse=True):
            candidates.append(api_dir)

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def resolve_lumapi_dir(explicit: str | None = None) -> Path:
    for candidate in candidate_lumapi_dirs(explicit):
        if (candidate / "lumapi.py").exists():
            return candidate.resolve()
    tried = ", ".join(str(path) for path in candidate_lumapi_dirs(explicit)) or "<none>"
    raise RuntimeError(f"Could not find lumapi.py. Tried: {tried}")


def load_lumapi(explicit: str | None = None):
    api_dir = resolve_lumapi_dir(explicit)
    api_dir_str = str(api_dir)
    if api_dir_str not in sys.path:
        sys.path.insert(0, api_dir_str)
    return importlib.import_module("lumapi"), api_dir


@dataclass
class ModeSpec:
    width_nm: float
    height_nm: float
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM
    slab_nm: float = 0.0
    sidewall_angle_deg: float = 90.0
    core_material: str = "Si (Silicon) - Palik"
    clad_material: str = "SiO2 (Glass) - Palik"
    trial_modes: int = 8
    mesh_accuracy: int = 3
    side_margin_um: float = 1.5
    vertical_margin_um: float = 1.5
    hide: bool = DEFAULT_HIDE
    lumapi_dir: str | None = None

    def validate(self) -> None:
        if self.width_nm <= 0:
            raise ValueError("width_nm must be positive.")
        if self.height_nm <= 0:
            raise ValueError("height_nm must be positive.")
        if self.wavelength_nm <= 0:
            raise ValueError("wavelength_nm must be positive.")
        if self.slab_nm < 0:
            raise ValueError("slab_nm cannot be negative.")
        if self.slab_nm >= self.height_nm:
            raise ValueError("slab_nm must be smaller than height_nm.")
        if not (45.0 <= self.sidewall_angle_deg <= 90.0):
            raise ValueError("sidewall_angle_deg must be between 45 and 90 degrees.")
        if self.trial_modes < 1:
            raise ValueError("trial_modes must be at least 1.")
        if self.mesh_accuracy < 1:
            raise ValueError("mesh_accuracy must be at least 1.")

    @property
    def width_m(self) -> float:
        return self.width_nm * 1e-9

    @property
    def height_m(self) -> float:
        return self.height_nm * 1e-9

    @property
    def wavelength_m(self) -> float:
        return self.wavelength_nm * 1e-9

    @property
    def slab_m(self) -> float:
        return self.slab_nm * 1e-9

    @property
    def rib_height_m(self) -> float:
        return self.height_m - self.slab_m

    @property
    def etch_depth_nm(self) -> float:
        return self.height_nm - self.slab_nm

    @property
    def side_margin_m(self) -> float:
        return self.side_margin_um * 1e-6

    @property
    def vertical_margin_m(self) -> float:
        return self.vertical_margin_um * 1e-6

    @property
    def solver_span_x_m(self) -> float:
        return max(self.width_m + 2 * self.side_margin_m, self.width_m * 6)

    @property
    def solver_span_y_m(self) -> float:
        return max(self.height_m + 2 * self.vertical_margin_m, self.height_m * 8)

    @property
    def slab_span_x_m(self) -> float:
        return max(self.width_m * 6, 3e-6)

    @property
    def slab_center_y_m(self) -> float:
        return (-self.height_m / 2.0) + (self.slab_m / 2.0)

    @property
    def rib_center_y_m(self) -> float:
        return self.slab_m / 2.0

    @property
    def rib_top_y_m(self) -> float:
        return self.height_m / 2.0

    @property
    def rib_bottom_y_m(self) -> float:
        return self.slab_m - (self.height_m / 2.0)

    @property
    def sidewall_offset_m(self) -> float:
        if self.sidewall_angle_deg >= 89.999:
            return 0.0
        return self.rib_height_m / math.tan(math.radians(self.sidewall_angle_deg))

    @property
    def needs_polygon_core(self) -> bool:
        return self.sidewall_offset_m > 1e-15

    def mode_label(self) -> str:
        return "rib" if self.slab_nm > 0 else "strip"


@dataclass
class SweepSpec:
    width_start_nm: float
    width_stop_nm: float
    width_step_nm: float
    height_nm: float
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM
    slab_nm: float = 0.0
    sidewall_angle_deg: float = 90.0
    core_material: str = "Si (Silicon) - Palik"
    clad_material: str = "SiO2 (Glass) - Palik"
    trial_modes: int = 8
    mesh_accuracy: int = 3
    side_margin_um: float = 1.5
    vertical_margin_um: float = 1.5
    hide: bool = DEFAULT_HIDE
    lumapi_dir: str | None = None

    def validate(self) -> None:
        if self.width_step_nm <= 0:
            raise ValueError("width_step_nm must be positive.")
        if self.width_stop_nm < self.width_start_nm:
            raise ValueError("width_stop_nm must be greater than or equal to width_start_nm.")
        _ = self.width_values_nm()
        self.to_mode_spec(self.width_start_nm).validate()

    def width_values_nm(self) -> list[float]:
        values: list[float] = []
        current = self.width_start_nm
        while current <= self.width_stop_nm + 1e-9:
            values.append(round(current, 6))
            current += self.width_step_nm
            if len(values) > 200:
                raise ValueError("Sweep is too large. Keep the number of points at 200 or fewer.")
        return values

    def to_mode_spec(self, width_nm: float) -> ModeSpec:
        return ModeSpec(
            width_nm=width_nm,
            height_nm=self.height_nm,
            wavelength_nm=self.wavelength_nm,
            slab_nm=self.slab_nm,
            sidewall_angle_deg=self.sidewall_angle_deg,
            core_material=self.core_material,
            clad_material=self.clad_material,
            trial_modes=self.trial_modes,
            mesh_accuracy=self.mesh_accuracy,
            side_margin_um=self.side_margin_um,
            vertical_margin_um=self.vertical_margin_um,
            hide=self.hide,
            lumapi_dir=self.lumapi_dir,
        )


@dataclass
class ParsedNaturalLanguageRequest:
    kind: str
    mode_spec: ModeSpec | None
    sweep_spec: SweepSpec | None
    assumptions: list[str]
    notes: list[str]
    normalized_request: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mode_spec": asdict(self.mode_spec) if self.mode_spec is not None else None,
            "sweep_spec": asdict(self.sweep_spec) if self.sweep_spec is not None else None,
            "assumptions": self.assumptions,
            "notes": self.notes,
            "normalized_request": self.normalized_request,
        }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def first_scalar(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return None
    if isinstance(value, complex):
        return float(value.real)
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        return first_scalar(value.tolist())
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        return first_scalar(value[0])
    return None


def to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        converted = value.tolist()
        if isinstance(converted, list):
            return converted
        return [converted]
    return [value]


def normalize_request_text(text: str) -> str:
    normalized = text.lower()
    normalized = normalized.replace("μm", "um").replace("µm", "um")
    normalized = normalized.replace("㎛", "um").replace("㎚", "nm")
    normalized = normalized.replace("°", "deg")
    normalized = normalized.replace(",", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def unit_to_nm(value: float, unit: str | None) -> float:
    if unit is None:
        return value
    normalized = unit.lower()
    if normalized in {"nm"}:
        return value
    if normalized in {"um", "micron", "microns"}:
        return value * 1000.0
    raise ValueError(f"Unsupported length unit: {unit}")


def extract_first_length_nm(text: str, patterns: list[str]) -> float | None:
    joined = "|".join(patterns)
    regexes = [
        re.compile(rf"(?:{joined})\s*(?:is|=|:|는|은|가|이|약|around|about)?\s*([0-9]+(?:\.[0-9]+)?)\s*(nm|um|micron|microns)?"),
        re.compile(rf"([0-9]+(?:\.[0-9]+)?)\s*(nm|um|micron|microns)?\s*(?:{joined})"),
    ]
    for regex in regexes:
        match = regex.search(text)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            return unit_to_nm(value, unit)
    return None


def extract_first_angle_deg(text: str, patterns: list[str]) -> float | None:
    joined = "|".join(patterns)
    regexes = [
        re.compile(rf"(?:{joined})\s*(?:is|=|:|는|은|가|이|약|around|about)?\s*([0-9]+(?:\.[0-9]+)?)\s*(deg|degree|degrees|도)?"),
        re.compile(rf"([0-9]+(?:\.[0-9]+)?)\s*(deg|degree|degrees|도)?\s*(?:{joined})"),
    ]
    for regex in regexes:
        match = regex.search(text)
        if match:
            return float(match.group(1))
    return None


def infer_sweep_range_nm(text: str) -> tuple[float, float] | None:
    range_patterns = [
        re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?\s*(?:to|~|-)\s*([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?"),
        re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?\s*(?:부터|에서)\s*([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?\s*(?:까지)?"),
    ]
    for regex in range_patterns:
        match = regex.search(text)
        if match:
            start = unit_to_nm(float(match.group(1)), match.group(2))
            stop = unit_to_nm(float(match.group(3)), match.group(4))
            if stop < start:
                start, stop = stop, start
            return start, stop
    return None


def format_assumptions(assumptions: list[str]) -> str:
    if not assumptions:
        return ""
    return " 기본 가정: " + "; ".join(assumptions) + "."


def looks_like_sweep_request(text: str) -> bool:
    return any(keyword in text for keyword in ["sweep", "scan", "range", "스윕", "쓸어", "바꿔", "변화", "vs ", "versus"])


def parse_natural_language_request(request: str) -> ParsedNaturalLanguageRequest:
    text = normalize_request_text(request)
    assumptions: list[str] = []
    notes: list[str] = []

    width_nm = extract_first_length_nm(
        text,
        [
            r"waveguide\s*width",
            r"wg\s*width",
            r"core\s*width",
            r"strip\s*width",
            r"rib\s*width",
            r"도파로\s*폭",
            r"웨이브가이드\s*폭",
            r"폭",
            r"선폭",
            r"가로폭",
            r"width",
        ],
    )
    width_was_default = width_nm is None
    if width_nm is None:
        width_nm = DEFAULT_MODE_WIDTH_NM

    height_nm = extract_first_length_nm(
        text,
        [
            r"device\s*layer\s*thickness",
            r"device\s*layer\s*height",
            r"top\s*silicon\s*thickness",
            r"top\s*silicon\s*height",
            r"silicon\s*thickness",
            r"si\s*thickness",
            r"waveguide\s*thickness",
            r"waveguide\s*height",
            r"wg\s*thickness",
            r"wg\s*height",
            r"core\s*thickness",
            r"core\s*height",
            r"상부\s*실리콘\s*두께",
            r"실리콘\s*두께",
            r"웨이브가이드\s*두께",
            r"도파로\s*두께",
            r"코어\s*두께",
            r"웨이브가이드\s*높이",
            r"도파로\s*높이",
            r"코어\s*높이",
        ],
    )
    wavelength_nm = extract_first_length_nm(
        text,
        [
            r"wavelength",
            r"lambda",
            r"파장",
        ],
    )
    if wavelength_nm is None:
        wavelength_nm = DEFAULT_WAVELENGTH_NM
        assumptions.append(f"wavelength {wavelength_nm:g} nm")

    slab_nm = extract_first_length_nm(
        text,
        [
            r"slab\s*thickness",
            r"slab",
            r"slab\s*height",
            r"슬랩\s*두께",
            r"슬랩",
        ],
    )
    etch_depth_nm = extract_first_length_nm(
        text,
        [
            r"etch\s*depth",
            r"etch",
            r"etching\s*depth",
            r"식각\s*깊이",
            r"에치\s*깊이",
            r"에칭\s*깊이",
        ],
    )
    sidewall_angle_deg = extract_first_angle_deg(
        text,
        [
            r"sidewall\s*angle",
            r"sidewall",
            r"wall\s*angle",
            r"사이드월\s*각도",
            r"사이드월\s*앵글",
            r"측벽\s*각도",
            r"각도",
        ],
    )
    if sidewall_angle_deg is None:
        sidewall_angle_deg = 90.0
        assumptions.append("sidewall angle 90 deg")

    full_etch = any(keyword in text for keyword in ["full etch", "fully etched", "full-etch", "전식각", "풀에치", "strip waveguide", "스트립"])
    rib_requested = any(keyword in text for keyword in ["rib waveguide", "rib", "ridge", "partial etch", "partial-etch", "부분식각", "리브", "리지"])

    if slab_nm is not None and etch_depth_nm is not None and height_nm is None:
        height_nm = slab_nm + etch_depth_nm
        notes.append("height was reconstructed from slab thickness + etch depth")

    if height_nm is None and slab_nm is None and etch_depth_nm is None:
        height_nm = DEFAULT_DEVICE_LAYER_NM
        if rib_requested:
            slab_nm = DEFAULT_RIB_SLAB_NM
            assumptions.append(f"SOI device layer {height_nm:g} nm")
            assumptions.append(f"rib slab {slab_nm:g} nm")
        else:
            slab_nm = 0.0
            assumptions.append(f"SOI device layer {height_nm:g} nm")
            assumptions.append("full etch")
    elif height_nm is None and etch_depth_nm is not None:
        if etch_depth_nm <= DEFAULT_DEVICE_LAYER_NM:
            height_nm = DEFAULT_DEVICE_LAYER_NM
            slab_nm = max(DEFAULT_DEVICE_LAYER_NM - etch_depth_nm, 0.0) if slab_nm is None else slab_nm
            assumptions.append(f"SOI device layer {height_nm:g} nm")
        else:
            height_nm = etch_depth_nm
            slab_nm = 0.0 if slab_nm is None else slab_nm
            notes.append("device layer thickness was inferred from etch depth")
    elif height_nm is None and slab_nm is not None:
        height_nm = max(DEFAULT_DEVICE_LAYER_NM, slab_nm + 1.0)
        assumptions.append(f"SOI device layer {height_nm:g} nm")

    if height_nm is None:
        height_nm = DEFAULT_DEVICE_LAYER_NM
        assumptions.append(f"SOI device layer {height_nm:g} nm")

    if slab_nm is None:
        if etch_depth_nm is not None:
            slab_nm = max(height_nm - etch_depth_nm, 0.0)
        elif full_etch:
            slab_nm = 0.0
        elif rib_requested:
            slab_nm = DEFAULT_RIB_SLAB_NM if DEFAULT_RIB_SLAB_NM < height_nm else max(height_nm - 70.0, 0.0)
            assumptions.append(f"rib slab {slab_nm:g} nm")
        else:
            slab_nm = 0.0

    if etch_depth_nm is None:
        etch_depth_nm = max(height_nm - slab_nm, 0.0)
        if slab_nm > 0:
            notes.append("etch depth was derived from height - slab")

    if full_etch:
        slab_nm = 0.0
        etch_depth_nm = height_nm

    if slab_nm < 0:
        raise ValueError("Parsed slab thickness became negative. Please specify height/slab/etch more clearly.")
    if etch_depth_nm < 0:
        raise ValueError("Parsed etch depth became negative. Please specify height/slab/etch more clearly.")
    if abs((height_nm - slab_nm) - etch_depth_nm) > 1.5:
        raise ValueError("height, slab, and etch depth are inconsistent.")

    kind = "sweep" if looks_like_sweep_request(text) else "mode"

    if kind == "sweep":
        sweep_range = infer_sweep_range_nm(text)
        if sweep_range is None:
            width_start_nm = DEFAULT_SWEEP_START_NM
            width_stop_nm = DEFAULT_SWEEP_STOP_NM
            assumptions.append(f"sweep range {width_start_nm:g}-{width_stop_nm:g} nm")
        else:
            width_start_nm, width_stop_nm = sweep_range
        width_step_nm = extract_first_length_nm(
            text,
            [
                r"step",
                r"step\s*size",
                r"interval",
                r"간격",
                r"스텝",
            ],
        )
        if width_step_nm is None:
            width_step_nm = DEFAULT_SWEEP_STEP_NM
            assumptions.append(f"sweep step {width_step_nm:g} nm")

        sweep_spec = SweepSpec(
            width_start_nm=width_start_nm,
            width_stop_nm=width_stop_nm,
            width_step_nm=width_step_nm,
            height_nm=height_nm,
            wavelength_nm=wavelength_nm,
            slab_nm=slab_nm,
            sidewall_angle_deg=sidewall_angle_deg,
        )
        return ParsedNaturalLanguageRequest(
            kind="sweep",
            mode_spec=None,
            sweep_spec=sweep_spec,
            assumptions=assumptions,
            notes=notes,
            normalized_request=text,
        )

    mode_spec = ModeSpec(
        width_nm=width_nm,
        height_nm=height_nm,
        wavelength_nm=wavelength_nm,
        slab_nm=slab_nm,
        sidewall_angle_deg=sidewall_angle_deg,
    )
    if width_was_default:
        assumptions.insert(0, f"width {width_nm:g} nm")
    return ParsedNaturalLanguageRequest(
        kind="mode",
        mode_spec=mode_spec,
        sweep_spec=None,
        assumptions=assumptions,
        notes=notes,
        normalized_request=text,
    )


def safe_result_names(sim, object_name: str) -> list[str]:
    try:
        return [str(item) for item in to_list(sim.getresult(object_name))]
    except Exception:
        return []


def safe_data_names(sim, object_name: str) -> list[str]:
    try:
        return [str(item) for item in to_list(sim.getdata(object_name))]
    except Exception:
        return []


def safe_metric(sim, object_name: str, data_name: str) -> float | None:
    try:
        return first_scalar(sim.getdata(object_name, data_name))
    except Exception:
        return None


def first_available_metric(sim, object_name: str, *data_names: str) -> float | None:
    for data_name in data_names:
        value = safe_metric(sim, object_name, data_name)
        if value is not None:
            return value
    return None


def matrix_literal(points: list[tuple[float, float]]) -> str:
    rows = [f"{x:.12g}, {y:.12g}" for x, y in points]
    return "[" + "; ".join(rows) + "]"


def core_geometry_script_lines(spec: ModeSpec) -> list[str]:
    if not spec.needs_polygon_core:
        return [
            'addrect; set("name", "core");',
            'set("x", 0);',
            f'set("x span", {spec.width_m:.12g});',
            f'set("y", {spec.rib_center_y_m:.12g});',
            f'set("y span", {spec.rib_height_m:.12g});',
            'set("z", 0);',
            'set("z span", 1e-6);',
            f'set("material", "{spec.core_material}");',
        ]

    top_half_width = spec.width_m / 2.0
    bottom_half_width = top_half_width + spec.sidewall_offset_m
    vertices = [
        (-bottom_half_width, spec.rib_bottom_y_m),
        (-top_half_width, spec.rib_top_y_m),
        (top_half_width, spec.rib_top_y_m),
        (bottom_half_width, spec.rib_bottom_y_m),
    ]
    return [
        f'vtx = {matrix_literal(vertices)};',
        'addpoly; set("name", "core");',
        'set("vertices", vtx);',
        'set("z", 0);',
        'set("z span", 1e-6);',
        f'set("material", "{spec.core_material}");',
    ]


def build_mode_script(spec: ModeSpec, project_path: Path, include_solve: bool = True) -> str:
    spec.validate()
    lines = [
        "newproject;",
        "switchtolayout;",
        "deleteall;",
        'addrect; set("name", "clad");',
        'set("x", 0);',
        f'set("x span", {spec.solver_span_x_m:.12g});',
        'set("y", 0);',
        f'set("y span", {spec.solver_span_y_m:.12g});',
        'set("z", 0);',
        'set("z span", 1e-6);',
        f'set("material", "{spec.clad_material}");',
    ]

    if spec.slab_m > 0:
        lines.extend(
            [
                'addrect; set("name", "slab");',
                'set("x", 0);',
                f'set("x span", {spec.slab_span_x_m:.12g});',
                f'set("y", {spec.slab_center_y_m:.12g});',
                f'set("y span", {spec.slab_m:.12g});',
                'set("z", 0);',
                'set("z span", 1e-6);',
                f'set("material", "{spec.core_material}");',
            ]
        )

    lines.extend(
        [
            *core_geometry_script_lines(spec),
            'addfde;',
            'set("solver type", "2D Z normal");',
            'set("x", 0);',
            f'set("x span", {spec.solver_span_x_m:.12g});',
            'set("y", 0);',
            f'set("y span", {spec.solver_span_y_m:.12g});',
            f'set("wavelength", {spec.wavelength_m:.12g});',
            f'set("number of trial modes", {spec.trial_modes});',
        ]
    )
    if include_solve:
        lines.extend(
            [
                "findmodes;",
                f'save("{project_path.as_posix()}");',
            ]
        )
    return "\n".join(lines) + "\n"


def write_mode_script(spec: ModeSpec, output_dir: Path, stem: str = "mode_job") -> Path:
    script_path = output_dir / f"{stem}.lsf"
    project_path = output_dir / f"{stem}.lms"
    script_path.write_text(build_mode_script(spec, project_path), encoding="utf-8")
    return script_path


def configure_mode_session(sim, spec: ModeSpec) -> None:
    sim.newproject()
    sim.switchtolayout()
    sim.deleteall()

    sim.addrect()
    sim.set("name", "clad")
    sim.set("x", 0)
    sim.set("x span", spec.solver_span_x_m)
    sim.set("y", 0)
    sim.set("y span", spec.solver_span_y_m)
    sim.set("z", 0)
    sim.set("z span", 1e-6)
    sim.set("material", spec.clad_material)

    if spec.slab_m > 0:
        sim.addrect()
        sim.set("name", "slab")
        sim.set("x", 0)
        sim.set("x span", spec.slab_span_x_m)
        sim.set("y", spec.slab_center_y_m)
        sim.set("y span", spec.slab_m)
        sim.set("z", 0)
        sim.set("z span", 1e-6)
        sim.set("material", spec.core_material)

    sim.addrect()
    sim.set("name", "core")
    sim.set("x", 0)
    sim.set("x span", spec.width_m)
    sim.set("y", spec.rib_center_y_m)
    sim.set("y span", spec.rib_height_m)
    sim.set("z", 0)
    sim.set("z span", 1e-6)
    sim.set("material", spec.core_material)

    sim.addfde()
    sim.set("solver type", "2D Z normal")
    sim.set("x", 0)
    sim.set("x span", spec.solver_span_x_m)
    sim.set("y", 0)
    sim.set("y span", spec.solver_span_y_m)
    sim.set("wavelength", spec.wavelength_m)
    sim.set("number of trial modes", spec.trial_modes)


def maybe_write_mode_plot(sim, output_path: Path) -> Path | None:
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    dataset = None
    for result_name in ("E", "mode profile", "fields"):
        try:
            dataset = sim.getresult("mode1", result_name)
            if dataset:
                break
        except Exception:
            continue
    if not isinstance(dataset, dict):
        return None

    x = dataset.get("x")
    y = dataset.get("y")
    field = dataset.get("E")
    if field is None:
        field = dataset.get("Ex")
    if field is None:
        field = dataset.get("Ey")
    if field is None:
        field = dataset.get("Ez")
    if field is None:
        return None

    x_arr = np.squeeze(np.asarray(x)) if x is not None else None
    y_arr = np.squeeze(np.asarray(y)) if y is not None else None
    field_arr = np.asarray(field)

    if field_arr.size == 0:
        return None

    if field_arr.ndim >= 3 and field_arr.shape[-1] in (3, 4):
        field_arr = np.linalg.norm(field_arr[..., :3], axis=-1)
    if np.iscomplexobj(field_arr):
        field_arr = np.abs(field_arr) ** 2
    field_arr = np.squeeze(field_arr)
    while field_arr.ndim > 2:
        field_arr = field_arr[..., 0]
    if field_arr.ndim != 2:
        return None

    extent = None
    if x_arr is not None and y_arr is not None and x_arr.ndim == 1 and y_arr.ndim == 1:
        if field_arr.shape == (len(y_arr), len(x_arr)):
            extent = [float(x_arr[0] * 1e6), float(x_arr[-1] * 1e6), float(y_arr[0] * 1e6), float(y_arr[-1] * 1e6)]
        elif field_arr.shape == (len(x_arr), len(y_arr)):
            field_arr = field_arr.T
            extent = [float(x_arr[0] * 1e6), float(x_arr[-1] * 1e6), float(y_arr[0] * 1e6), float(y_arr[-1] * 1e6)]

    field_arr = field_arr.astype(float)
    if field_arr.max() > field_arr.min():
        normalized = (field_arr - field_arr.min()) / (field_arr.max() - field_arr.min())
    else:
        normalized = np.zeros_like(field_arr)

    r = np.clip(255 * np.sqrt(normalized), 0, 255).astype(np.uint8)
    g = np.clip(255 * normalized**0.7, 0, 255).astype(np.uint8)
    b = np.clip(255 * (1.0 - normalized**0.45), 0, 255).astype(np.uint8)
    rgb = np.dstack([r, g // 2, b])

    heatmap = Image.fromarray(rgb, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (760, 620), "white")
    canvas.paste(heatmap, (70, 70))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    draw.text((70, 18), "Fundamental mode intensity", fill="black", font=font)
    if extent is not None:
        x0, x1, y0, y1 = extent
        draw.text((70, 560), f"x: {x0:.2f} to {x1:.2f} um", fill="black", font=font)
        draw.text((70, 580), f"y: {y0:.2f} to {y1:.2f} um", fill="black", font=font)
    else:
        draw.text((70, 560), "Axis: array index", fill="black", font=font)

    canvas.save(output_path)
    return output_path


def live_mode_analysis(spec: ModeSpec, output_dir: Path) -> dict[str, Any]:
    spec.validate()
    lumapi, api_dir = load_lumapi(spec.lumapi_dir)
    output_dir = ensure_directory(output_dir)

    project_path = output_dir / "mode_job.lms"
    result_payload: dict[str, Any] = {
        "api_dir": str(api_dir),
        "project_path": str(project_path.resolve()),
        "spec": asdict(spec),
    }

    sim = lumapi.MODE(hide=spec.hide)
    try:
        sim.eval(build_mode_script(spec, project_path, include_solve=False))
        modes_found = int(sim.findmodes())
        sim.save(str(project_path))

        result_payload["modes_found"] = modes_found
        result_payload["mode1_data_names"] = safe_data_names(sim, "mode1")
        result_payload["mode1_result_names"] = safe_result_names(sim, "mode1")

        metrics = {
            "neff": safe_metric(sim, "mode1", "neff") if modes_found > 0 else None,
            "loss": safe_metric(sim, "mode1", "loss") if modes_found > 0 else None,
            "te_polarization_fraction": safe_metric(sim, "mode1", "TE polarization fraction") if modes_found > 0 else None,
            "effective_area": first_available_metric(sim, "mode1", "effective area", "mode effective area") if modes_found > 0 else None,
        }
        result_payload["metrics"] = metrics

        plot_path = maybe_write_mode_plot(sim, output_dir / "mode1_intensity.png") if modes_found > 0 else None
        if plot_path is not None:
            result_payload["mode_plot"] = str(plot_path.resolve())

        result_payload["ok"] = True
        return result_payload
    finally:
        try:
            sim.close()
        except Exception:
            pass


def summarize_mode_result(result: dict[str, Any], spec: ModeSpec, fallback_reason: str | None = None) -> str:
    metrics = result.get("metrics", {}) or {}
    neff = metrics.get("neff")
    te_fraction = metrics.get("te_polarization_fraction")
    modes_found = result.get("modes_found")

    if fallback_reason:
        return (
            f"{spec.mode_label()} waveguide 작업은 준비해 두었습니다. "
            f"live MODE 실행은 완료되지 않아 spec과 LSF 스크립트를 남겼고, 이유는 {fallback_reason} 입니다."
        )

    parts = [
        f"{spec.mode_label()} waveguide {spec.width_nm:g} x {spec.height_nm:g} nm",
        f"{spec.wavelength_nm:g} nm",
    ]
    message = "MODE 해석을 실행했습니다. " + ", ".join(parts) + " 기준입니다."
    if modes_found is not None:
        message += f" 찾은 모드는 {modes_found}개입니다."
    if neff is not None:
        message += f" fundamental neff는 {neff:.6f} 입니다."
    if te_fraction is not None:
        message += f" TE polarization fraction은 {te_fraction:.4f} 입니다."
    return message


def run_live_subprocess(subcommand: str, spec_path: Path, output_dir: Path, timeout_s: int) -> tuple[bool, str | None]:
    command = [
        sys.executable,
        str(BASE_DIR / "photonics_agent.py"),
        subcommand,
        "--spec",
        str(spec_path),
        "--output-dir",
        str(output_dir),
    ]
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["taskkill", "/pid", str(process.pid), "/t", "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return False, f"timeout after {timeout_s}s"

    if process.returncode != 0:
        message = (stderr or stdout or "").strip()
        return False, message[-500:] if message else f"subprocess exited with code {process.returncode}"
    return True, None


def write_mode_summary(output_dir: Path, spec: ModeSpec, result: dict[str, Any], message: str) -> Path:
    summary_path = output_dir / "summary.json"
    payload = {
        "message": message,
        "spec": asdict(spec),
        "result": result,
    }
    write_json(summary_path, payload)
    return summary_path


def execute_mode_command(spec: ModeSpec, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False) -> dict[str, Any]:
    spec.validate()
    run_dir = ensure_directory(output_dir or (output_root() / f"mode-{timestamp()}"))
    spec_path = run_dir / "mode_spec.json"
    write_json(spec_path, asdict(spec))
    script_path = write_mode_script(spec, run_dir)

    result: dict[str, Any] = {
        "ok": True,
        "status": "script_only",
        "result_dir": str(run_dir.resolve()),
        "attachments": [str(spec_path.resolve()), str(script_path.resolve())],
    }

    if script_only:
        result["message"] = summarize_mode_result(result, spec, fallback_reason="script-only mode")
        summary_path = write_mode_summary(run_dir, spec, result, result["message"])
        result["attachments"].append(str(summary_path.resolve()))
        return result

    live_ok, live_error = run_live_subprocess("_live_mode", spec_path, run_dir, timeout_s=timeout_s)
    if live_ok:
        live_result = json.loads((run_dir / "live_result.json").read_text(encoding="utf-8"))
        message = summarize_mode_result(live_result, spec)
        attachments = [str(spec_path.resolve()), str(script_path.resolve()), str((run_dir / "mode_job.lms").resolve())]
        if live_result.get("mode_plot"):
            attachments.insert(0, live_result["mode_plot"])
        summary_path = write_mode_summary(run_dir, spec, live_result, message)
        attachments.append(str(summary_path.resolve()))
        result.update(
            {
                "status": "solved",
                "message": message,
                "live_result": live_result,
                "attachments": attachments,
            }
        )
        return result

    message = summarize_mode_result(result, spec, fallback_reason=live_error or "unknown error")
    summary_path = write_mode_summary(run_dir, spec, result, message)
    result["status"] = "fallback"
    result["message"] = message
    result["live_error"] = live_error
    result["attachments"].append(str(summary_path.resolve()))
    return result


def execute_sweep_command(spec: SweepSpec, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False) -> dict[str, Any]:
    spec.validate()
    run_dir = ensure_directory(output_dir or (output_root() / f"sweep-{timestamp()}"))
    spec_path = run_dir / "sweep_spec.json"
    write_json(spec_path, asdict(spec))

    seed_mode_spec = spec.to_mode_spec(spec.width_start_nm)
    seed_script_path = write_mode_script(seed_mode_spec, run_dir, stem="seed_mode")
    result: dict[str, Any] = {
        "ok": True,
        "status": "script_only",
        "result_dir": str(run_dir.resolve()),
        "attachments": [str(spec_path.resolve()), str(seed_script_path.resolve())],
    }

    if script_only:
        message = (
            f"width sweep 준비를 마쳤습니다. {spec.width_start_nm:g}~{spec.width_stop_nm:g} nm, "
            f"step {spec.width_step_nm:g} nm 조건으로 spec과 seed 스크립트를 남겼습니다."
        )
        summary_path = write_mode_summary(run_dir, seed_mode_spec, result, message)
        result["message"] = message
        result["attachments"].append(str(summary_path.resolve()))
        return result

    live_ok, live_error = run_live_subprocess("_live_sweep", spec_path, run_dir, timeout_s=timeout_s)
    if live_ok:
        live_result = json.loads((run_dir / "live_result.json").read_text(encoding="utf-8"))
        message = (
            f"width sweep를 실행했습니다. {spec.width_start_nm:g}~{spec.width_stop_nm:g} nm 범위에서 "
            f"{len(spec.width_values_nm())}개 점을 계산했습니다."
        )
        attachments = [str(spec_path.resolve()), str(seed_script_path.resolve())]
        for file_name in ("sweep_results.csv", "sweep_summary.json", "neff_vs_width.png"):
            path = run_dir / file_name
            if path.exists():
                attachments.insert(0, str(path.resolve()))
        result.update(
            {
                "status": "solved",
                "message": message,
                "live_result": live_result,
                "attachments": attachments,
            }
        )
        return result

    message = (
        f"width sweep는 준비해 두었습니다. live MODE 실행은 완료되지 않아 spec과 seed 스크립트를 남겼고, "
        f"이유는 {live_error or 'unknown error'} 입니다."
    )
    summary_path = write_mode_summary(run_dir, seed_mode_spec, result, message)
    result["status"] = "fallback"
    result["message"] = message
    result["live_error"] = live_error
    result["attachments"].append(str(summary_path.resolve()))
    return result


def execute_nl_command(request: str, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False) -> dict[str, Any]:
    parsed = parse_natural_language_request(request)
    run_dir = ensure_directory(output_dir or (output_root() / f"nl-{timestamp()}"))
    parsed_path = run_dir / "parsed_request.json"
    write_json(parsed_path, parsed.to_dict())

    if parsed.kind == "mode":
        assert parsed.mode_spec is not None
        payload = execute_mode_command(parsed.mode_spec, output_dir=run_dir, timeout_s=timeout_s, script_only=script_only)
        payload["parsed"] = parsed.to_dict()
        payload["attachments"].append(str(parsed_path.resolve()))
        payload["message"] += format_assumptions(parsed.assumptions)
        if parsed.notes:
            payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
        return payload

    assert parsed.sweep_spec is not None
    payload = execute_sweep_command(parsed.sweep_spec, output_dir=run_dir, timeout_s=timeout_s, script_only=script_only)
    payload["parsed"] = parsed.to_dict()
    payload["attachments"].append(str(parsed_path.resolve()))
    payload["message"] += format_assumptions(parsed.assumptions)
    if parsed.notes:
        payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
    return payload


def write_sweep_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["width_nm", "modes_found", "neff", "loss", "te_polarization_fraction", "effective_area", "status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def maybe_write_sweep_plot(rows: list[dict[str, Any]], output_path: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    x_values = [row["width_nm"] for row in rows if row.get("neff") is not None]
    y_values = [row["neff"] for row in rows if row.get("neff") is not None]
    if not x_values or not y_values:
        return None

    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    if math.isclose(x_min, x_max):
        x_max = x_min + 1.0
    if math.isclose(y_min, y_max):
        y_max = y_min + 0.1

    canvas = Image.new("RGB", (760, 520), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    left, top, right, bottom = 90, 50, 700, 420
    draw.rectangle((left, top, right, bottom), outline="black", width=2)
    draw.text((90, 18), "neff vs width", fill="black", font=font)
    draw.text((300, 470), "Width (nm)", fill="black", font=font)
    draw.text((20, 230), "neff", fill="black", font=font)

    def map_x(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (right - left)

    def map_y(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    points = [(map_x(x), map_y(y)) for x, y in zip(x_values, y_values)]
    if len(points) >= 2:
        draw.line(points, fill=(30, 90, 200), width=3)
    for point, x_value, y_value in zip(points, x_values, y_values):
        draw.ellipse((point[0] - 4, point[1] - 4, point[0] + 4, point[1] + 4), fill=(220, 40, 40), outline=(220, 40, 40))
        draw.text((point[0] - 16, point[1] - 20), f"{x_value:g}", fill="black", font=font)
    draw.text((90, 430), f"x: {x_min:g} to {x_max:g} nm", fill="black", font=font)
    draw.text((90, 448), f"y: {y_min:.4f} to {y_max:.4f}", fill="black", font=font)
    canvas.save(output_path)
    return output_path


def live_sweep_analysis(spec: SweepSpec, output_dir: Path) -> dict[str, Any]:
    spec.validate()
    output_dir = ensure_directory(output_dir)
    rows: list[dict[str, Any]] = []
    point_dirs: list[str] = []

    for width_nm in spec.width_values_nm():
        point_dir = ensure_directory(output_dir / f"w_{width_nm:g}nm")
        point_dirs.append(str(point_dir.resolve()))
        point_spec = spec.to_mode_spec(width_nm)
        point_result = live_mode_analysis(point_spec, point_dir)
        metrics = point_result.get("metrics", {}) or {}
        rows.append(
            {
                "width_nm": width_nm,
                "modes_found": point_result.get("modes_found"),
                "neff": metrics.get("neff"),
                "loss": metrics.get("loss"),
                "te_polarization_fraction": metrics.get("te_polarization_fraction"),
                "effective_area": metrics.get("effective_area"),
                "status": "ok",
            }
        )

    csv_path = output_dir / "sweep_results.csv"
    summary_path = output_dir / "sweep_summary.json"
    write_sweep_csv(csv_path, rows)
    plot_path = maybe_write_sweep_plot(rows, output_dir / "neff_vs_width.png")
    payload = {
        "ok": True,
        "rows": rows,
        "point_dirs": point_dirs,
        "csv_path": str(csv_path.resolve()),
        "plot_path": str(plot_path.resolve()) if plot_path is not None else None,
    }
    write_json(summary_path, payload)
    return payload


def environment_summary(explicit: str | None = None) -> dict[str, Any]:
    candidates = [str(path) for path in candidate_lumapi_dirs(explicit)]
    payload: dict[str, Any] = {
        "ok": True,
        "python": sys.executable,
        "output_root": str(output_root()),
        "candidate_lumapi_dirs": candidates,
        "commands": [
            "/mode width=500 height=220 wavelength=1550",
            "/sweep start=400 stop=700 step=25 height=220 wavelength=1550",
            "기본적인 etch depth 220nm SOI waveguide mode profile 그려줘",
        ],
    }

    try:
        lumapi, api_dir = load_lumapi(explicit)
        payload["lumapi_importable"] = True
        payload["lumapi_file"] = getattr(lumapi, "__file__", None)
        payload["selected_lumapi_dir"] = str(api_dir)
        payload["message"] = (
            "lumapi import는 가능합니다. 이제 /mode 또는 /sweep 명령으로 MODE 작업을 바로 시작할 수 있습니다."
        )
    except Exception as exc:
        payload["lumapi_importable"] = False
        payload["message"] = f"lumapi import에 실패했습니다: {exc}"
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lumerical-oriented photonic design helper")
    parser.add_argument("--json", action="store_true", help="Print JSON output only")

    subparsers = parser.add_subparsers(dest="command", required=True)

    env_parser = subparsers.add_parser("env", help="Check lumapi detection")
    env_parser.add_argument("--lumapi-dir", default=None)
    env_parser.add_argument("--json", action="store_true")

    mode_parser = subparsers.add_parser("mode", help="Run or prepare one MODE waveguide solve")
    add_mode_arguments(mode_parser)
    mode_parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    mode_parser.add_argument("--script-only", action="store_true")
    mode_parser.add_argument("--output-dir", default=None)
    mode_parser.add_argument("--json", action="store_true")

    sweep_parser = subparsers.add_parser("sweep", help="Run or prepare a width sweep")
    add_sweep_arguments(sweep_parser)
    sweep_parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    sweep_parser.add_argument("--script-only", action="store_true")
    sweep_parser.add_argument("--output-dir", default=None)
    sweep_parser.add_argument("--json", action="store_true")

    nl_parser = subparsers.add_parser("nl", help="Parse and run a natural language photonics request")
    nl_parser.add_argument("--request", required=True)
    nl_parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    nl_parser.add_argument("--script-only", action="store_true")
    nl_parser.add_argument("--output-dir", default=None)
    nl_parser.add_argument("--json", action="store_true")

    live_mode_parser = subparsers.add_parser("_live_mode")
    live_mode_parser.add_argument("--spec", required=True)
    live_mode_parser.add_argument("--output-dir", required=True)

    live_sweep_parser = subparsers.add_parser("_live_sweep")
    live_sweep_parser.add_argument("--spec", required=True)
    live_sweep_parser.add_argument("--output-dir", required=True)

    return parser.parse_args()


def add_mode_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--width-nm", type=float, required=True)
    parser.add_argument("--height-nm", type=float, required=True)
    parser.add_argument("--wavelength-nm", type=float, default=DEFAULT_WAVELENGTH_NM)
    parser.add_argument("--slab-nm", type=float, default=0.0)
    parser.add_argument("--sidewall-angle-deg", type=float, default=90.0)
    parser.add_argument("--core-material", default="Si (Silicon) - Palik")
    parser.add_argument("--clad-material", default="SiO2 (Glass) - Palik")
    parser.add_argument("--trial-modes", type=int, default=8)
    parser.add_argument("--mesh-accuracy", type=int, default=3)
    parser.add_argument("--side-margin-um", type=float, default=1.5)
    parser.add_argument("--vertical-margin-um", type=float, default=1.5)
    parser.add_argument("--show-gui", action="store_true")
    parser.add_argument("--lumapi-dir", default=None)


def add_sweep_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--width-start-nm", type=float, required=True)
    parser.add_argument("--width-stop-nm", type=float, required=True)
    parser.add_argument("--width-step-nm", type=float, required=True)
    parser.add_argument("--height-nm", type=float, required=True)
    parser.add_argument("--wavelength-nm", type=float, default=DEFAULT_WAVELENGTH_NM)
    parser.add_argument("--slab-nm", type=float, default=0.0)
    parser.add_argument("--sidewall-angle-deg", type=float, default=90.0)
    parser.add_argument("--core-material", default="Si (Silicon) - Palik")
    parser.add_argument("--clad-material", default="SiO2 (Glass) - Palik")
    parser.add_argument("--trial-modes", type=int, default=8)
    parser.add_argument("--mesh-accuracy", type=int, default=3)
    parser.add_argument("--side-margin-um", type=float, default=1.5)
    parser.add_argument("--vertical-margin-um", type=float, default=1.5)
    parser.add_argument("--show-gui", action="store_true")
    parser.add_argument("--lumapi-dir", default=None)


def mode_spec_from_args(args: argparse.Namespace) -> ModeSpec:
    return ModeSpec(
        width_nm=args.width_nm,
        height_nm=args.height_nm,
        wavelength_nm=args.wavelength_nm,
        slab_nm=args.slab_nm,
        sidewall_angle_deg=args.sidewall_angle_deg,
        core_material=args.core_material,
        clad_material=args.clad_material,
        trial_modes=args.trial_modes,
        mesh_accuracy=args.mesh_accuracy,
        side_margin_um=args.side_margin_um,
        vertical_margin_um=args.vertical_margin_um,
        hide=not args.show_gui,
        lumapi_dir=args.lumapi_dir,
    )


def sweep_spec_from_args(args: argparse.Namespace) -> SweepSpec:
    return SweepSpec(
        width_start_nm=args.width_start_nm,
        width_stop_nm=args.width_stop_nm,
        width_step_nm=args.width_step_nm,
        height_nm=args.height_nm,
        wavelength_nm=args.wavelength_nm,
        slab_nm=args.slab_nm,
        sidewall_angle_deg=args.sidewall_angle_deg,
        core_material=args.core_material,
        clad_material=args.clad_material,
        trial_modes=args.trial_modes,
        mesh_accuracy=args.mesh_accuracy,
        side_margin_um=args.side_margin_um,
        vertical_margin_um=args.vertical_margin_um,
        hide=not args.show_gui,
        lumapi_dir=args.lumapi_dir,
    )


def emit(payload: dict[str, Any], json_only: bool = False) -> None:
    if json_only:
        print(json.dumps(payload, ensure_ascii=False))
        return

    message = payload.get("message")
    if message:
        print(message)
    attachments = payload.get("attachments") or []
    if attachments:
        print("Attachments:")
        for attachment in attachments:
            print(attachment)


def main() -> int:
    args = parse_args()

    try:
        if args.command == "env":
            payload = environment_summary(args.lumapi_dir)
            emit(payload, json_only=args.json)
            return 0

        if args.command == "mode":
            output_dir = Path(args.output_dir).resolve() if args.output_dir else None
            payload = execute_mode_command(
                mode_spec_from_args(args),
                output_dir=output_dir,
                timeout_s=args.timeout_s,
                script_only=args.script_only,
            )
            emit(payload, json_only=args.json)
            return 0

        if args.command == "sweep":
            output_dir = Path(args.output_dir).resolve() if args.output_dir else None
            payload = execute_sweep_command(
                sweep_spec_from_args(args),
                output_dir=output_dir,
                timeout_s=args.timeout_s,
                script_only=args.script_only,
            )
            emit(payload, json_only=args.json)
            return 0

        if args.command == "nl":
            output_dir = Path(args.output_dir).resolve() if args.output_dir else None
            payload = execute_nl_command(
                args.request,
                output_dir=output_dir,
                timeout_s=args.timeout_s,
                script_only=args.script_only,
            )
            emit(payload, json_only=args.json)
            return 0

        if args.command == "_live_mode":
            spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            result = live_mode_analysis(ModeSpec(**spec_data), Path(args.output_dir))
            write_json(Path(args.output_dir) / "live_result.json", result)
            return 0

        if args.command == "_live_sweep":
            spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            result = live_sweep_analysis(SweepSpec(**spec_data), Path(args.output_dir))
            write_json(Path(args.output_dir) / "live_result.json", result)
            return 0

        raise RuntimeError(f"Unknown command: {args.command}")
    except Exception as exc:
        if args.command.startswith("_live_"):
            error_path = Path(args.output_dir) / "live_error.txt"
            error_path.write_text(str(exc), encoding="utf-8")
        payload = {"ok": False, "message": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

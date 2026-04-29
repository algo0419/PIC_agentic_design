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
DEFAULT_DC_GAP_NM = float(os.environ.get("PHOTONICS_DEFAULT_DC_GAP_NM", "200"))
DEFAULT_DC_COUPLING_LENGTH_UM = float(os.environ.get("PHOTONICS_DEFAULT_DC_COUPLING_LENGTH_UM", "20"))
DEFAULT_DC_ACCESS_LENGTH_UM = float(os.environ.get("PHOTONICS_DEFAULT_DC_ACCESS_LENGTH_UM", "10"))
DEFAULT_DC_GAP_SWEEP_START_NM = float(os.environ.get("PHOTONICS_DEFAULT_DC_GAP_SWEEP_START_NM", "100"))
DEFAULT_DC_GAP_SWEEP_STOP_NM = float(os.environ.get("PHOTONICS_DEFAULT_DC_GAP_SWEEP_STOP_NM", "300"))
DEFAULT_DC_GAP_SWEEP_STEP_NM = float(os.environ.get("PHOTONICS_DEFAULT_DC_GAP_SWEEP_STEP_NM", "50"))
DEFAULT_DC_LENGTH_SWEEP_START_UM = float(os.environ.get("PHOTONICS_DEFAULT_DC_LENGTH_SWEEP_START_UM", "5"))
DEFAULT_DC_LENGTH_SWEEP_STOP_UM = float(os.environ.get("PHOTONICS_DEFAULT_DC_LENGTH_SWEEP_STOP_UM", "60"))
DEFAULT_DC_LENGTH_SWEEP_STEP_UM = float(os.environ.get("PHOTONICS_DEFAULT_DC_LENGTH_SWEEP_STEP_UM", "5"))
LUMERICAL_PROJECT_SUFFIXES = {".lms", ".fsp", ".ldev", ".icp"}
TELEGRAM_ATTACHMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gds", *LUMERICAL_PROJECT_SUFFIXES}


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


def memory_path() -> Path:
    return output_root() / "task_memory.jsonl"


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
class DirectionalCouplerSpec:
    width_nm: float = DEFAULT_MODE_WIDTH_NM
    height_nm: float = DEFAULT_DEVICE_LAYER_NM
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM
    gap_nm: float = DEFAULT_DC_GAP_NM
    coupling_length_um: float = DEFAULT_DC_COUPLING_LENGTH_UM
    input_length_um: float = DEFAULT_DC_ACCESS_LENGTH_UM
    output_length_um: float = DEFAULT_DC_ACCESS_LENGTH_UM
    target_split_ratio: float = 0.5
    slab_nm: float = 0.0
    sidewall_angle_deg: float = 90.0
    core_material: str = "Si (Silicon) - Palik"
    clad_material: str = "SiO2 (Glass) - Palik"
    trial_modes: int = 8
    mesh_accuracy: int = 3
    side_margin_um: float = 2.0
    vertical_margin_um: float = 1.5
    hide: bool = DEFAULT_HIDE
    lumapi_dir: str | None = None
    coupling_length_source: str = "default"
    simulation_method: str = "eme"

    def validate(self) -> None:
        if self.width_nm <= 0:
            raise ValueError("width_nm must be positive.")
        if self.height_nm <= 0:
            raise ValueError("height_nm must be positive.")
        if self.wavelength_nm <= 0:
            raise ValueError("wavelength_nm must be positive.")
        if self.gap_nm <= 0:
            raise ValueError("gap_nm must be positive.")
        if self.coupling_length_um <= 0:
            raise ValueError("coupling_length_um must be positive.")
        if self.input_length_um < 0 or self.output_length_um < 0:
            raise ValueError("access lengths cannot be negative.")
        if self.slab_nm < 0:
            raise ValueError("slab_nm cannot be negative.")
        if self.slab_nm >= self.height_nm:
            raise ValueError("slab_nm must be smaller than height_nm.")
        if not (45.0 <= self.sidewall_angle_deg <= 90.0):
            raise ValueError("sidewall_angle_deg must be between 45 and 90 degrees.")
        if not (0.0 < self.target_split_ratio < 1.0):
            raise ValueError("target_split_ratio must be between 0 and 1.")
        if self.trial_modes < 2:
            raise ValueError("trial_modes must be at least 2 for directional coupler supermodes.")

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
    def gap_m(self) -> float:
        return self.gap_nm * 1e-9

    @property
    def slab_m(self) -> float:
        return self.slab_nm * 1e-9

    @property
    def rib_height_m(self) -> float:
        return self.height_m - self.slab_m

    @property
    def center_offset_m(self) -> float:
        return (self.width_m + self.gap_m) / 2.0

    @property
    def solver_span_x_m(self) -> float:
        pair_width = 2 * self.width_m + self.gap_m
        return max(pair_width + 2 * self.side_margin_um * 1e-6, pair_width * 4)

    @property
    def solver_span_y_m(self) -> float:
        return max(self.height_m + 2 * self.vertical_margin_um * 1e-6, self.height_m * 8)

    @property
    def slab_span_x_m(self) -> float:
        return max(2 * self.width_m + self.gap_m + 4e-6, 5e-6)

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

    @property
    def total_length_um(self) -> float:
        return self.input_length_um + self.coupling_length_um + self.output_length_um


@dataclass
class DirectionalCouplerSweepSpec:
    base: DirectionalCouplerSpec
    parameter: str
    start: float
    stop: float
    step: float

    def validate(self) -> None:
        self.base.validate()
        if self.parameter not in {"gap_nm", "coupling_length_um"}:
            raise ValueError("parameter must be gap_nm or coupling_length_um.")
        if self.step <= 0:
            raise ValueError("step must be positive.")
        if self.stop < self.start:
            raise ValueError("stop must be greater than or equal to start.")
        _ = self.values()

    def values(self) -> list[float]:
        values: list[float] = []
        current = self.start
        while current <= self.stop + 1e-9:
            values.append(round(current, 6))
            current += self.step
            if len(values) > 200:
                raise ValueError("Sweep is too large. Keep the number of points at 200 or fewer.")
        return values

    def to_dc_spec(self, value: float) -> DirectionalCouplerSpec:
        data = asdict(self.base)
        data[self.parameter] = value
        if self.parameter == "coupling_length_um":
            data["coupling_length_source"] = "sweep"
        return DirectionalCouplerSpec(**data)


@dataclass
class FdtdTestSpec:
    width_nm: float = DEFAULT_MODE_WIDTH_NM
    height_nm: float = DEFAULT_DEVICE_LAYER_NM
    length_um: float = 2.0
    wavelength_nm: float = DEFAULT_WAVELENGTH_NM
    core_material: str = "Si (Silicon) - Palik"
    clad_material: str = "SiO2 (Glass) - Palik"
    mesh_accuracy: int = 1
    simulation_time_fs: float = 50.0
    hide: bool = DEFAULT_HIDE
    lumapi_dir: str | None = None

    def validate(self) -> None:
        if self.width_nm <= 0:
            raise ValueError("width_nm must be positive.")
        if self.height_nm <= 0:
            raise ValueError("height_nm must be positive.")
        if self.length_um <= 0:
            raise ValueError("length_um must be positive.")
        if self.wavelength_nm <= 0:
            raise ValueError("wavelength_nm must be positive.")
        if self.mesh_accuracy < 1:
            raise ValueError("mesh_accuracy must be at least 1.")
        if self.simulation_time_fs <= 0:
            raise ValueError("simulation_time_fs must be positive.")

    @property
    def width_m(self) -> float:
        return self.width_nm * 1e-9

    @property
    def height_m(self) -> float:
        return self.height_nm * 1e-9

    @property
    def length_m(self) -> float:
        return self.length_um * 1e-6

    @property
    def wavelength_m(self) -> float:
        return self.wavelength_nm * 1e-9

    @property
    def x_span_m(self) -> float:
        return self.length_m + 1.0e-6

    @property
    def y_span_m(self) -> float:
        return max(self.width_m + 1.5e-6, 2.0e-6)


@dataclass
class ParsedNaturalLanguageRequest:
    kind: str
    mode_spec: ModeSpec | None
    sweep_spec: SweepSpec | None
    assumptions: list[str]
    notes: list[str]
    normalized_request: str
    directional_coupler_spec: DirectionalCouplerSpec | None = None
    directional_coupler_sweep_spec: DirectionalCouplerSweepSpec | None = None
    fdtd_test_spec: FdtdTestSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mode_spec": asdict(self.mode_spec) if self.mode_spec is not None else None,
            "sweep_spec": asdict(self.sweep_spec) if self.sweep_spec is not None else None,
            "directional_coupler_spec": asdict(self.directional_coupler_spec) if self.directional_coupler_spec is not None else None,
            "directional_coupler_sweep_spec": asdict(self.directional_coupler_sweep_spec) if self.directional_coupler_sweep_spec is not None else None,
            "fdtd_test_spec": asdict(self.fdtd_test_spec) if self.fdtd_test_spec is not None else None,
            "assumptions": self.assumptions,
            "notes": self.notes,
            "normalized_request": self.normalized_request,
        }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return json.dumps(str(value))
        return f"{value:g}" if isinstance(value, float) else str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_lines(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if not item:
                    empty = "[]" if isinstance(item, list) else "{}"
                    lines.append(f"{prefix}{key}: {empty}")
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.extend(yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                if not item:
                    empty = "[]" if isinstance(item, list) else "{}"
                    lines.append(f"{prefix}- {empty}")
                else:
                    lines.append(f"{prefix}-")
                    lines.extend(yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
        return lines
    return [f"{prefix}{yaml_scalar(value)}"]


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text("\n".join(yaml_lines(payload)) + "\n", encoding="utf-8")


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


def extract_first_length_um(text: str, patterns: list[str]) -> float | None:
    joined = "|".join(patterns)
    regexes = [
        re.compile(rf"(?:{joined})\s*(?:is|=|:|는|은|가|이|약|around|about)?\s*([0-9]+(?:\.[0-9]+)?)\s*(nm|um|micron|microns)?"),
        re.compile(rf"([0-9]+(?:\.[0-9]+)?)\s*(nm|um|micron|microns)?\s*(?:{joined})"),
    ]
    for regex in regexes:
        match = regex.search(text)
        if match:
            value = float(match.group(1))
            unit = match.group(2) or "um"
            return unit_to_nm(value, unit) / 1000.0
    return None


def infer_sweep_range_um(text: str) -> tuple[float, float] | None:
    range_patterns = [
        re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?\s*(?:to|~|-)\s*([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?"),
        re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?\s*(?:부터|에서)\s*([0-9]+(?:\.[0-9]+)?)\s*(nm|um)?\s*(?:까지)?"),
    ]
    for regex in range_patterns:
        match = regex.search(text)
        if match:
            start = unit_to_nm(float(match.group(1)), match.group(2) or "um") / 1000.0
            stop = unit_to_nm(float(match.group(3)), match.group(4) or "um") / 1000.0
            if stop < start:
                start, stop = stop, start
            return start, stop
    return None


def extract_split_ratio(text: str) -> float | None:
    match = re.search(r"([0-9]{1,3})\s*(?::|/|대)\s*([0-9]{1,3})", text)
    if not match:
        return None
    first = float(match.group(1))
    second = float(match.group(2))
    total = first + second
    if total <= 0:
        return None
    ratio = second / total
    if not (0.0 < ratio < 1.0):
        return None
    return ratio


def format_assumptions(assumptions: list[str]) -> str:
    if not assumptions:
        return ""
    return " 기본 가정: " + "; ".join(assumptions) + "."


def looks_like_sweep_request(text: str) -> bool:
    return any(keyword in text for keyword in ["sweep", "scan", "range", "스윕", "쓸어", "바꿔", "변화", "vs ", "versus"])


def looks_like_directional_coupler_request(text: str) -> bool:
    keywords = [
        "directional coupler",
        "directional-coupler",
        "dc ",
        " dc",
        "50:50",
        "50대 50",
        "coupling length",
        "커플러",
        "커플링",
        "방향성 결합기",
        "방향성커플러",
        "결합기",
    ]
    return any(keyword in text for keyword in keywords)


def looks_like_fdtd_test_request(text: str) -> bool:
    return "fdtd" in text or ".fsp" in text or "fsp" in text


def detect_directional_coupler_sweep_parameter(text: str) -> str | None:
    if not looks_like_sweep_request(text):
        return None
    length_terms = ["coupling length", "interaction length", "coupler length", "결합 길이", "커플링 길이", "길이 sweep", "길이 스윕"]
    gap_terms = ["gap", "spacing", "separation", "갭", "간격", "사이"]
    if any(term in text for term in length_terms):
        return "coupling_length_um"
    if any(term in text for term in gap_terms):
        return "gap_nm"
    return "gap_nm"


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

    if looks_like_fdtd_test_request(text):
        if width_was_default:
            assumptions.insert(0, f"waveguide width {width_nm:g} nm")
        length_um = extract_first_length_um(
            text,
            [
                r"fdtd\s*length",
                r"simulation\s*length",
                r"waveguide\s*length",
                r"wg\s*length",
                r"length",
                r"길이",
            ],
        )
        if length_um is None:
            length_um = 2.0
            assumptions.append("FDTD test waveguide length 2 um")
        assumptions.append("FDTD project only; no time-domain run")
        return ParsedNaturalLanguageRequest(
            kind="fdtd_test",
            mode_spec=None,
            sweep_spec=None,
            assumptions=assumptions,
            notes=notes,
            normalized_request=text,
            fdtd_test_spec=FdtdTestSpec(
                width_nm=width_nm,
                height_nm=height_nm,
                length_um=length_um,
                wavelength_nm=wavelength_nm,
            ),
        )

    if looks_like_directional_coupler_request(text):
        if width_was_default:
            assumptions.insert(0, f"waveguide width {width_nm:g} nm")

        gap_nm = extract_first_length_nm(
            text,
            [
                r"coupler\s*gap",
                r"waveguide\s*gap",
                r"wg\s*gap",
                r"gap",
                r"spacing",
                r"separation",
                r"갭",
                r"간격",
            ],
        )
        if gap_nm is None:
            gap_nm = DEFAULT_DC_GAP_NM
            assumptions.append(f"directional coupler gap {gap_nm:g} nm")

        coupling_length_um = extract_first_length_um(
            text,
            [
                r"coupling\s*length",
                r"interaction\s*length",
                r"coupler\s*length",
                r"dc\s*length",
                r"l_c",
                r"lc",
                r"결합\s*길이",
                r"커플링\s*길이",
                r"길이",
            ],
        )
        coupling_length_source = "user"
        if coupling_length_um is None:
            coupling_length_um = DEFAULT_DC_COUPLING_LENGTH_UM
            coupling_length_source = "default"
            assumptions.append(f"initial coupling length {coupling_length_um:g} um")

        target_split_ratio = extract_split_ratio(text)
        if target_split_ratio is None:
            target_split_ratio = 0.5
            assumptions.append("target split 50:50")

        base_spec = DirectionalCouplerSpec(
            width_nm=width_nm,
            height_nm=height_nm,
            wavelength_nm=wavelength_nm,
            gap_nm=gap_nm,
            coupling_length_um=coupling_length_um,
            target_split_ratio=target_split_ratio,
            slab_nm=slab_nm,
            sidewall_angle_deg=sidewall_angle_deg,
            coupling_length_source=coupling_length_source,
        )
        sweep_parameter = detect_directional_coupler_sweep_parameter(text)
        if sweep_parameter == "gap_nm":
            sweep_range = infer_sweep_range_nm(text)
            if sweep_range is None:
                sweep_start = DEFAULT_DC_GAP_SWEEP_START_NM
                sweep_stop = DEFAULT_DC_GAP_SWEEP_STOP_NM
                assumptions.append(f"gap sweep range {sweep_start:g}-{sweep_stop:g} nm")
            else:
                sweep_start, sweep_stop = sweep_range
            sweep_step = extract_first_length_nm(
                text,
                [
                    r"step",
                    r"step\s*size",
                    r"interval",
                    r"간격",
                    r"스텝",
                ],
            )
            if sweep_step is None:
                sweep_step = DEFAULT_DC_GAP_SWEEP_STEP_NM
                assumptions.append(f"gap sweep step {sweep_step:g} nm")
            return ParsedNaturalLanguageRequest(
                kind="directional_coupler_sweep",
                mode_spec=None,
                sweep_spec=None,
                assumptions=assumptions,
                notes=notes,
                normalized_request=text,
                directional_coupler_sweep_spec=DirectionalCouplerSweepSpec(
                    base=base_spec,
                    parameter="gap_nm",
                    start=sweep_start,
                    stop=sweep_stop,
                    step=sweep_step,
                ),
            )

        if sweep_parameter == "coupling_length_um":
            sweep_range_um = infer_sweep_range_um(text)
            if sweep_range_um is None:
                sweep_start_um = DEFAULT_DC_LENGTH_SWEEP_START_UM
                sweep_stop_um = DEFAULT_DC_LENGTH_SWEEP_STOP_UM
                assumptions.append(f"coupling length sweep range {sweep_start_um:g}-{sweep_stop_um:g} um")
            else:
                sweep_start_um, sweep_stop_um = sweep_range_um
            sweep_step_um = extract_first_length_um(
                text,
                [
                    r"step",
                    r"step\s*size",
                    r"interval",
                    r"간격",
                    r"스텝",
                ],
            )
            if sweep_step_um is None:
                sweep_step_um = DEFAULT_DC_LENGTH_SWEEP_STEP_UM
                assumptions.append(f"coupling length sweep step {sweep_step_um:g} um")
            return ParsedNaturalLanguageRequest(
                kind="directional_coupler_sweep",
                mode_spec=None,
                sweep_spec=None,
                assumptions=assumptions,
                notes=notes,
                normalized_request=text,
                directional_coupler_sweep_spec=DirectionalCouplerSweepSpec(
                    base=base_spec,
                    parameter="coupling_length_um",
                    start=sweep_start_um,
                    stop=sweep_stop_um,
                    step=sweep_step_um,
                ),
            )

        return ParsedNaturalLanguageRequest(
            kind="directional_coupler",
            mode_spec=None,
            sweep_spec=None,
            assumptions=assumptions,
            notes=notes,
            normalized_request=text,
            directional_coupler_spec=base_spec,
        )

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


def waveguide_params_for_dsl(spec: ModeSpec | DirectionalCouplerSpec) -> dict[str, Any]:
    return {
        "width_nm": spec.width_nm,
        "height_nm": spec.height_nm,
        "slab_nm": spec.slab_nm,
        "etch_depth_nm": spec.height_nm - spec.slab_nm,
        "sidewall_angle_deg": spec.sidewall_angle_deg,
        "core_material": spec.core_material,
        "clad_material": spec.clad_material,
    }


def mode_spec_to_dsl(spec: ModeSpec, raw_request: str | None = None, assumptions: list[str] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "request": {
            "raw": raw_request,
            "assumptions": assumptions or [],
            "notes": notes or [],
        },
        "intent": {
            "component": "waveguide",
            "task": "mode_solve",
            "solver": "MODE_FDE",
        },
        "graph": {
            "nodes": [
                {
                    "id": "wg1",
                    "type": "soi_waveguide",
                    "params": waveguide_params_for_dsl(spec),
                }
            ],
            "edges": [],
        },
        "simulation": {
            "wavelength_nm": spec.wavelength_nm,
            "trial_modes": spec.trial_modes,
            "mesh_accuracy": spec.mesh_accuracy,
        },
    }


def sweep_spec_to_dsl(spec: SweepSpec, raw_request: str | None = None, assumptions: list[str] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    seed = spec.to_mode_spec(spec.width_start_nm)
    dsl = mode_spec_to_dsl(seed, raw_request=raw_request, assumptions=assumptions, notes=notes)
    dsl["intent"] = {
        "component": "waveguide",
        "task": "width_sweep",
        "solver": "MODE_FDE",
    }
    dsl["sweep"] = {
        "parameter": "width_nm",
        "start": spec.width_start_nm,
        "stop": spec.width_stop_nm,
        "step": spec.width_step_nm,
        "values": spec.width_values_nm(),
    }
    return dsl


def directional_coupler_spec_to_dsl(
    spec: DirectionalCouplerSpec,
    raw_request: str | None = None,
    assumptions: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "request": {
            "raw": raw_request,
            "assumptions": assumptions or [],
            "notes": notes or [],
        },
        "intent": {
            "component": "directional_coupler",
            "task": "simulate_and_layout",
            "solver": "MODE_FDE_supermode_with_EME_script",
            "target_split_ratio": spec.target_split_ratio,
        },
        "graph": {
            "nodes": [
                {
                    "id": "dc1",
                    "type": "directional_coupler",
                    "params": {
                        "waveguide": waveguide_params_for_dsl(spec),
                        "gap_nm": spec.gap_nm,
                        "coupling_length_um": spec.coupling_length_um,
                        "input_length_um": spec.input_length_um,
                        "output_length_um": spec.output_length_um,
                    },
                },
                {"id": "in_top", "type": "optical_port", "params": {"side": "west", "rail": "top"}},
                {"id": "in_bottom", "type": "optical_port", "params": {"side": "west", "rail": "bottom"}},
                {"id": "out_top", "type": "optical_port", "params": {"side": "east", "rail": "top"}},
                {"id": "out_bottom", "type": "optical_port", "params": {"side": "east", "rail": "bottom"}},
            ],
            "edges": [
                {"from": "in_top", "to": "dc1", "kind": "optical_link"},
                {"from": "in_bottom", "to": "dc1", "kind": "optical_link"},
                {"from": "dc1", "to": "out_top", "kind": "optical_link"},
                {"from": "dc1", "to": "out_bottom", "kind": "optical_link"},
            ],
        },
        "simulation": {
            "wavelength_nm": spec.wavelength_nm,
            "method": spec.simulation_method,
            "sandbox_metric": "even_odd_supermode_delta_neff",
            "trial_modes": spec.trial_modes,
        },
    }


def directional_coupler_sweep_spec_to_dsl(
    spec: DirectionalCouplerSweepSpec,
    raw_request: str | None = None,
    assumptions: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    dsl = directional_coupler_spec_to_dsl(spec.base, raw_request=raw_request, assumptions=assumptions, notes=notes)
    dsl["intent"]["task"] = "parameter_sweep"
    dsl["sweep"] = {
        "parameter": spec.parameter,
        "start": spec.start,
        "stop": spec.stop,
        "step": spec.step,
        "values": spec.values(),
    }
    return dsl


def fdtd_test_spec_to_dsl(
    spec: FdtdTestSpec,
    raw_request: str | None = None,
    assumptions: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "request": {
            "raw": raw_request,
            "assumptions": assumptions or [],
            "notes": notes or [],
        },
        "intent": {
            "component": "waveguide",
            "task": "fdtd_project_smoke_test",
            "solver": "FDTD",
        },
        "graph": {
            "nodes": [
                {
                    "id": "wg1",
                    "type": "straight_waveguide",
                    "params": {
                        "width_nm": spec.width_nm,
                        "height_nm": spec.height_nm,
                        "length_um": spec.length_um,
                        "core_material": spec.core_material,
                        "clad_material": spec.clad_material,
                    },
                },
                {"id": "src", "type": "mode_source", "params": {"wavelength_nm": spec.wavelength_nm}},
                {"id": "mon", "type": "power_monitor", "params": {"plane": "x-normal"}},
            ],
            "edges": [
                {"from": "src", "to": "wg1", "kind": "excitation"},
                {"from": "wg1", "to": "mon", "kind": "field_monitor"},
            ],
        },
        "simulation": {
            "wavelength_nm": spec.wavelength_nm,
            "mesh_accuracy": spec.mesh_accuracy,
            "simulation_time_fs": spec.simulation_time_fs,
            "run": False,
        },
    }


def write_design_yaml(output_dir: Path, dsl: dict[str, Any]) -> Path:
    path = output_dir / "design.yaml"
    write_yaml(path, dsl)
    return path


def write_agent_pipeline(
    output_dir: Path,
    intent: dict[str, Any],
    spec_path: Path | None,
    design_path: Path | None,
    artifacts: list[str],
    runner: dict[str, Any],
    evaluation: dict[str, Any],
    issues: list[str] | None = None,
) -> Path:
    payload = {
        "version": 1,
        "stages": [
            {"agent": "Intent Agent", "status": "done", "output": intent},
            {"agent": "Spec Agent", "status": "done", "output": {"spec": str(spec_path.resolve()) if spec_path else None, "dsl": str(design_path.resolve()) if design_path else None}},
            {"agent": "Code Writer Agent", "status": "done", "output": {"artifacts": artifacts}},
            {"agent": "Code Reviewer Agent", "status": "done" if not issues else "needs_attention", "output": {"issues": issues or []}},
            {"agent": "Sandbox Runner", "status": runner.get("status", "done"), "output": runner},
            {"agent": "Result Evaluator Agent", "status": "done", "output": evaluation},
            {"agent": "Debug / Refine Agent", "status": "done", "output": {"next_action": "ready_to_report" if not issues else "review_issues"}},
            {"agent": "Telegram Report + Files", "status": "queued", "output": {"attachments": artifacts}},
        ],
    }
    path = output_dir / "agent_pipeline.yaml"
    write_yaml(path, payload)
    return path


def dsl_summary_text(dsl: dict[str, Any]) -> str:
    intent = dsl.get("intent", {}) or {}
    component = intent.get("component", "unknown")
    task = intent.get("task", "unknown")
    graph = dsl.get("graph", {}) or {}
    nodes = graph.get("nodes", []) or []
    simulation = dsl.get("simulation", {}) or {}
    assumptions = ((dsl.get("request", {}) or {}).get("assumptions", []) or [])

    if task == "fdtd_project_smoke_test":
        wg_node = next((node for node in nodes if node.get("type") == "straight_waveguide"), {})
        params = wg_node.get("params", {}) or {}
        lines = [
            "DSL 요약: FDTD / project smoke test",
            f"- 구조: width {params.get('width_nm')} nm, height {params.get('height_nm')} nm, length {params.get('length_um')} um",
            f"- 시뮬레이션: wavelength {simulation.get('wavelength_nm')} nm, mesh accuracy {simulation.get('mesh_accuracy')}, run=false",
            f"- 그래프: nodes {len(nodes)}개, links {len(graph.get('edges', []) or [])}개",
        ]
        if assumptions:
            lines.append("- 기본 가정: " + "; ".join(str(item) for item in assumptions))
        return "\n".join(lines)

    if component == "waveguide":
        params = ((nodes[0] if nodes else {}).get("params", {}) or {})
        lines = [
            f"DSL 요약: {component} / {task}",
            f"- 구조: width {params.get('width_nm')} nm, height {params.get('height_nm')} nm, slab {params.get('slab_nm')} nm, sidewall {params.get('sidewall_angle_deg')} deg",
            f"- 시뮬레이션: {simulation.get('solver', intent.get('solver', 'MODE_FDE'))}, wavelength {simulation.get('wavelength_nm')} nm",
        ]
        sweep = dsl.get("sweep")
        if sweep:
            lines.append(f"- Sweep: {sweep.get('parameter')} {sweep.get('start')} -> {sweep.get('stop')} step {sweep.get('step')}")
        if assumptions:
            lines.append("- 기본 가정: " + "; ".join(str(item) for item in assumptions))
        return "\n".join(lines)

    if component == "directional_coupler":
        dc_node = next((node for node in nodes if node.get("type") == "directional_coupler"), {})
        params = dc_node.get("params", {}) or {}
        wg = params.get("waveguide", {}) or {}
        lines = [
            f"DSL 요약: directional coupler / {task}",
            f"- 구조: width {wg.get('width_nm')} nm, height {wg.get('height_nm')} nm, gap {params.get('gap_nm')} nm, coupling length {params.get('coupling_length_um')} um",
            f"- 목표: split {float(intent.get('target_split_ratio', 0.5)):.3f}, solver {intent.get('solver')}",
            f"- 그래프: nodes {len(nodes)}개, optical links {len(graph.get('edges', []) or [])}개",
        ]
        sweep = dsl.get("sweep")
        if sweep:
            label = "gap" if sweep.get("parameter") == "gap_nm" else "coupling length"
            lines.append(f"- Sweep: {label} {sweep.get('start')} -> {sweep.get('stop')} step {sweep.get('step')}")
        if assumptions:
            lines.append("- 기본 가정: " + "; ".join(str(item) for item in assumptions))
        return "\n".join(lines)

    return f"DSL 요약: {component} / {task}"


def telegram_attachments_from(paths: list[str]) -> list[str]:
    filtered: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path.suffix.lower() not in TELEGRAM_ATTACHMENT_SUFFIXES:
            continue
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        filtered.append(raw_path)
    return filtered


def apply_telegram_policy(payload: dict[str, Any]) -> dict[str, Any]:
    payload["telegram_attachments"] = telegram_attachments_from([str(path) for path in payload.get("attachments", [])])
    return payload


def request_tokens(text: str) -> set[str]:
    normalized = normalize_request_text(text)
    return {token for token in re.findall(r"[a-z0-9가-힣_]+", normalized) if len(token) >= 2}


def read_task_memory() -> list[dict[str, Any]]:
    path = memory_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def find_similar_task(kind: str, request: str, limit: int = 1) -> list[dict[str, Any]]:
    current_tokens = request_tokens(request)
    if not current_tokens:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for record in read_task_memory():
        past_tokens = set(record.get("tokens", []))
        if not past_tokens:
            continue
        overlap = len(current_tokens & past_tokens)
        union = len(current_tokens | past_tokens)
        score = overlap / union if union else 0.0
        if record.get("kind") == kind:
            score += 0.25
        if score >= 0.35:
            scored.append((score, record))
    scored.sort(key=lambda item: (item[0], item[1].get("timestamp", "")), reverse=True)
    return [record for _, record in scored[:limit]]


def remember_task(kind: str, request: str, dsl: dict[str, Any], payload: dict[str, Any]) -> None:
    path = memory_path()
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "request": request,
        "tokens": sorted(request_tokens(request)),
        "summary": dsl_summary_text(dsl).replace("\n", " / "),
        "result_dir": payload.get("result_dir"),
        "status": payload.get("status"),
        "message": payload.get("message"),
        "telegram_attachments": payload.get("telegram_attachments", []),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def add_report_context(payload: dict[str, Any], dsl: dict[str, Any] | None = None, similar_records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _ = dsl
    _ = similar_records
    return apply_telegram_policy(payload)


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


def dc_core_geometry_script_lines(spec: DirectionalCouplerSpec, x_center_m: float, name: str, z_span_m: float = 1e-6) -> list[str]:
    if not spec.needs_polygon_core:
        return [
            f'addrect; set("name", "{name}");',
            f'set("x", {x_center_m:.12g});',
            f'set("x span", {spec.width_m:.12g});',
            f'set("y", {spec.rib_center_y_m:.12g});',
            f'set("y span", {spec.rib_height_m:.12g});',
            'set("z", 0);',
            f'set("z span", {z_span_m:.12g});',
            f'set("material", "{spec.core_material}");',
        ]

    top_half_width = spec.width_m / 2.0
    bottom_half_width = top_half_width + spec.sidewall_offset_m
    vertices = [
        (x_center_m - bottom_half_width, spec.rib_bottom_y_m),
        (x_center_m - top_half_width, spec.rib_top_y_m),
        (x_center_m + top_half_width, spec.rib_top_y_m),
        (x_center_m + bottom_half_width, spec.rib_bottom_y_m),
    ]
    return [
        f'vtx_{name} = {matrix_literal(vertices)};',
        f'addpoly; set("name", "{name}");',
        f'set("vertices", vtx_{name});',
        'set("z", 0);',
        f'set("z span", {z_span_m:.12g});',
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


def build_dc_supermode_script(spec: DirectionalCouplerSpec, project_path: Path, include_solve: bool = True) -> str:
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
            *dc_core_geometry_script_lines(spec, -spec.center_offset_m, "wg_left"),
            *dc_core_geometry_script_lines(spec, spec.center_offset_m, "wg_right"),
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


def build_dc_eme_script(spec: DirectionalCouplerSpec, project_path: Path) -> str:
    spec.validate()
    length_m = spec.coupling_length_um * 1e-6
    z_span_m = max(length_m, 1e-6)
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
        f'set("z span", {z_span_m:.12g});',
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
                f'set("z span", {z_span_m:.12g});',
                f'set("material", "{spec.core_material}");',
            ]
        )
    lines.extend(
        [
            *dc_core_geometry_script_lines(spec, -spec.center_offset_m, "wg_left", z_span_m=z_span_m),
            *dc_core_geometry_script_lines(spec, spec.center_offset_m, "wg_right", z_span_m=z_span_m),
            'addeme;',
            "# Minimal EME smoke-test project: geometry plus EME solver object, no propagation run.",
            "# Configure cell groups and ports manually for a production EME solve if needed.",
            f'save("{project_path.as_posix()}");',
        ]
    )
    return "\n".join(lines) + "\n"


def write_dc_scripts(spec: DirectionalCouplerSpec, output_dir: Path) -> tuple[Path, Path]:
    supermode_script = output_dir / "dc_supermode_fde.lsf"
    eme_script = output_dir / "dc_eme.lsf"
    supermode_script.write_text(build_dc_supermode_script(spec, output_dir / "dc_supermode_fde.lms"), encoding="utf-8")
    eme_script.write_text(build_dc_eme_script(spec, output_dir / "dc_eme.lms"), encoding="utf-8")
    return supermode_script, eme_script


def build_fdtd_test_script(spec: FdtdTestSpec, project_path: Path) -> str:
    spec.validate()
    source_x = -0.35 * spec.length_m
    monitor_x = 0.35 * spec.length_m
    return "\n".join(
        [
            "newproject;",
            "switchtolayout;",
            "deleteall;",
            'addrect; set("name", "clad");',
            'set("x", 0);',
            f'set("x span", {spec.x_span_m:.12g});',
            'set("y", 0);',
            f'set("y span", {spec.y_span_m:.12g});',
            'set("z", 0);',
            f'set("z span", {max(spec.height_m + 1.0e-6, 1.5e-6):.12g});',
            f'set("material", "{spec.clad_material}");',
            'addrect; set("name", "core");',
            'set("x", 0);',
            f'set("x span", {spec.length_m:.12g});',
            'set("y", 0);',
            f'set("y span", {spec.width_m:.12g});',
            'set("z", 0);',
            f'set("z span", {spec.height_m:.12g});',
            f'set("material", "{spec.core_material}");',
            "addfdtd;",
            'set("dimension", "2D");',
            'set("x", 0);',
            f'set("x span", {spec.x_span_m:.12g});',
            'set("y", 0);',
            f'set("y span", {spec.y_span_m:.12g});',
            f'set("mesh accuracy", {spec.mesh_accuracy});',
            f'set("simulation time", {spec.simulation_time_fs * 1e-15:.12g});',
            "addmode;",
            'set("name", "mode_source");',
            'set("injection axis", "x-axis");',
            'set("direction", "Forward");',
            f'set("x", {source_x:.12g});',
            'set("y", 0);',
            f'set("y span", {min(spec.y_span_m * 0.8, 1.5e-6):.12g});',
            f'set("wavelength start", {spec.wavelength_m:.12g});',
            f'set("wavelength stop", {spec.wavelength_m:.12g});',
            "addpower;",
            'set("name", "through_monitor");',
            'set("monitor type", "2D X-normal");',
            f'set("x", {monitor_x:.12g});',
            'set("y", 0);',
            f'set("y span", {min(spec.y_span_m * 0.8, 1.5e-6):.12g});',
            f'save("{project_path.as_posix()}");',
            "",
        ]
    )


def write_fdtd_test_script(spec: FdtdTestSpec, output_dir: Path) -> Path:
    script_path = output_dir / "fdtd_test.lsf"
    script_path.write_text(build_fdtd_test_script(spec, output_dir / "fdtd_test.fsp"), encoding="utf-8")
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


def mode_structure_overlay(spec: ModeSpec) -> dict[str, Any]:
    outlines: list[dict[str, float | str]] = []
    if spec.slab_nm > 0:
        outlines.append(
            {
                "label": "slab",
                "x0_um": -spec.slab_span_x_m * 1e6 / 2.0,
                "x1_um": spec.slab_span_x_m * 1e6 / 2.0,
                "y0_um": spec.slab_center_y_m * 1e6 - spec.slab_m * 1e6 / 2.0,
                "y1_um": spec.slab_center_y_m * 1e6 + spec.slab_m * 1e6 / 2.0,
            }
        )
    outlines.append(
        {
            "label": "core",
            "x0_um": -spec.width_nm / 2000.0,
            "x1_um": spec.width_nm / 2000.0,
            "y0_um": spec.rib_bottom_y_m * 1e6,
            "y1_um": spec.rib_top_y_m * 1e6,
        }
    )
    return {"title": f"{spec.mode_label()} waveguide structure", "outlines": outlines}


def dc_structure_overlay(spec: DirectionalCouplerSpec) -> dict[str, Any]:
    outlines: list[dict[str, float | str]] = []
    if spec.slab_nm > 0:
        outlines.append(
            {
                "label": "slab",
                "x0_um": -spec.slab_span_x_m * 1e6 / 2.0,
                "x1_um": spec.slab_span_x_m * 1e6 / 2.0,
                "y0_um": spec.slab_center_y_m * 1e6 - spec.slab_m * 1e6 / 2.0,
                "y1_um": spec.slab_center_y_m * 1e6 + spec.slab_m * 1e6 / 2.0,
            }
        )
    half_width_um = spec.width_nm / 2000.0
    center_offset_um = spec.center_offset_m * 1e6
    for label, center_x in (("left core", -center_offset_um), ("right core", center_offset_um)):
        outlines.append(
            {
                "label": label,
                "x0_um": center_x - half_width_um,
                "x1_um": center_x + half_width_um,
                "y0_um": spec.rib_bottom_y_m * 1e6,
                "y1_um": spec.rib_top_y_m * 1e6,
            }
        )
    return {"title": "directional coupler structure", "outlines": outlines}


def draw_structure_overlay(draw: Any, extent: list[float] | None, structure: dict[str, Any] | None, heatmap_box: tuple[int, int, int, int]) -> None:
    if extent is None or not structure:
        return
    x0, x1, y0, y1 = extent
    left, top, right, bottom = heatmap_box
    if math.isclose(x0, x1) or math.isclose(y0, y1):
        return

    def map_x(value_um: float) -> float:
        return left + (value_um - x0) / (x1 - x0) * (right - left)

    def map_y(value_um: float) -> float:
        return bottom - (value_um - y0) / (y1 - y0) * (bottom - top)

    colors = [(255, 255, 255), (255, 220, 50), (50, 255, 180)]
    for index, outline in enumerate(structure.get("outlines", []) or []):
        color = colors[index % len(colors)]
        box = (
            map_x(float(outline["x0_um"])),
            map_y(float(outline["y1_um"])),
            map_x(float(outline["x1_um"])),
            map_y(float(outline["y0_um"])),
        )
        draw.rectangle(box, outline=color, width=3)


def maybe_write_mode_plot(sim, output_path: Path, structure: dict[str, Any] | None = None) -> Path | None:
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
    heatmap_box = (70, 70, 710, 550)
    canvas.paste(heatmap, (heatmap_box[0], heatmap_box[1]))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    draw.text((70, 18), "Fundamental mode intensity", fill="black", font=font)
    if structure:
        draw.text((70, 38), str(structure.get("title", "structure overlay")), fill="black", font=font)
    draw_structure_overlay(draw, extent, structure, heatmap_box)
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

        plot_path = maybe_write_mode_plot(sim, output_dir / "mode1_intensity.png", structure=mode_structure_overlay(spec)) if modes_found > 0 else None
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
        encoding="utf-8",
        errors="replace",
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
        return apply_telegram_policy(result)

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
        return apply_telegram_policy(result)

    message = summarize_mode_result(result, spec, fallback_reason=live_error or "unknown error")
    summary_path = write_mode_summary(run_dir, spec, result, message)
    result["status"] = "fallback"
    result["message"] = message
    result["live_error"] = live_error
    result["attachments"].append(str(summary_path.resolve()))
    return apply_telegram_policy(result)


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
        return apply_telegram_policy(result)

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
        return apply_telegram_policy(result)

    message = (
        f"width sweep는 준비해 두었습니다. live MODE 실행은 완료되지 않아 spec과 seed 스크립트를 남겼고, "
        f"이유는 {live_error or 'unknown error'} 입니다."
    )
    summary_path = write_mode_summary(run_dir, seed_mode_spec, result, message)
    result["status"] = "fallback"
    result["message"] = message
    result["live_error"] = live_error
    result["attachments"].append(str(summary_path.resolve()))
    return apply_telegram_policy(result)


def execute_nl_command(request: str, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False) -> dict[str, Any]:
    parsed = parse_natural_language_request(request)
    similar_records = find_similar_task(parsed.kind, request)
    run_dir = ensure_directory(output_dir or (output_root() / f"nl-{timestamp()}"))
    parsed_path = run_dir / "parsed_request.json"
    write_json(parsed_path, parsed.to_dict())

    if parsed.kind == "mode":
        assert parsed.mode_spec is not None
        dsl = mode_spec_to_dsl(parsed.mode_spec, raw_request=request, assumptions=parsed.assumptions, notes=parsed.notes)
        design_path = write_design_yaml(run_dir, dsl)
        payload = execute_mode_command(parsed.mode_spec, output_dir=run_dir, timeout_s=timeout_s, script_only=script_only)
        payload["parsed"] = parsed.to_dict()
        payload["attachments"].append(str(design_path.resolve()))
        payload["attachments"].append(str(parsed_path.resolve()))
        if parsed.notes:
            payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": parsed.kind, "route": "nl"},
            run_dir / "mode_spec.json",
            design_path,
            payload["attachments"],
            {"status": payload.get("status", "done")},
            {"message": payload.get("message")},
            issues=review_artifacts(payload["attachments"]),
        )
        payload["attachments"].append(str(pipeline_path.resolve()))
        payload = add_report_context(payload, dsl, similar_records)
        remember_task(parsed.kind, request, dsl, payload)
        return payload

    if parsed.kind == "sweep":
        assert parsed.sweep_spec is not None
        dsl = sweep_spec_to_dsl(parsed.sweep_spec, raw_request=request, assumptions=parsed.assumptions, notes=parsed.notes)
        design_path = write_design_yaml(run_dir, dsl)
        payload = execute_sweep_command(parsed.sweep_spec, output_dir=run_dir, timeout_s=timeout_s, script_only=script_only)
        payload["parsed"] = parsed.to_dict()
        payload["attachments"].append(str(design_path.resolve()))
        payload["attachments"].append(str(parsed_path.resolve()))
        if parsed.notes:
            payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": parsed.kind, "route": "nl"},
            run_dir / "sweep_spec.json",
            design_path,
            payload["attachments"],
            {"status": payload.get("status", "done")},
            {"message": payload.get("message")},
            issues=review_artifacts(payload["attachments"]),
        )
        payload["attachments"].append(str(pipeline_path.resolve()))
        payload = add_report_context(payload, dsl, similar_records)
        remember_task(parsed.kind, request, dsl, payload)
        return payload

    if parsed.kind == "directional_coupler":
        assert parsed.directional_coupler_spec is not None
        dsl = directional_coupler_spec_to_dsl(parsed.directional_coupler_spec, raw_request=request, assumptions=parsed.assumptions, notes=parsed.notes)
        payload = execute_dc_command(
            parsed.directional_coupler_spec,
            output_dir=run_dir,
            timeout_s=timeout_s,
            script_only=script_only,
            raw_request=request,
            assumptions=parsed.assumptions,
            notes=parsed.notes,
        )
        payload["parsed"] = parsed.to_dict()
        payload["attachments"].append(str(parsed_path.resolve()))
        if parsed.notes:
            payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
        payload = add_report_context(payload, None, similar_records)
        remember_task(parsed.kind, request, dsl, payload)
        return payload

    if parsed.kind == "directional_coupler_sweep":
        assert parsed.directional_coupler_sweep_spec is not None
        dsl = directional_coupler_sweep_spec_to_dsl(parsed.directional_coupler_sweep_spec, raw_request=request, assumptions=parsed.assumptions, notes=parsed.notes)
        payload = execute_dc_sweep_command(
            parsed.directional_coupler_sweep_spec,
            output_dir=run_dir,
            timeout_s=timeout_s,
            script_only=script_only,
            raw_request=request,
            assumptions=parsed.assumptions,
            notes=parsed.notes,
        )
        payload["parsed"] = parsed.to_dict()
        payload["attachments"].append(str(parsed_path.resolve()))
        if parsed.notes:
            payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
        payload = add_report_context(payload, None, similar_records)
        remember_task(parsed.kind, request, dsl, payload)
        return payload

    assert parsed.fdtd_test_spec is not None
    dsl = fdtd_test_spec_to_dsl(parsed.fdtd_test_spec, raw_request=request, assumptions=parsed.assumptions, notes=parsed.notes)
    payload = execute_fdtd_test_command(
        parsed.fdtd_test_spec,
        output_dir=run_dir,
        timeout_s=timeout_s,
        script_only=script_only,
        raw_request=request,
        assumptions=parsed.assumptions,
        notes=parsed.notes,
    )
    payload["parsed"] = parsed.to_dict()
    payload["attachments"].append(str(parsed_path.resolve()))
    if parsed.notes:
        payload["message"] += " 해석 노트: " + "; ".join(parsed.notes) + "."
    payload = add_report_context(payload, None, similar_records)
    remember_task(parsed.kind, request, dsl, payload)
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


def gds_record(record_type: int, data_type: int, data: bytes = b"") -> bytes:
    length = 4 + len(data)
    if length % 2:
        data += b"\0"
        length += 1
    return length.to_bytes(2, "big") + bytes([record_type, data_type]) + data


def gds_int2(values: list[int]) -> bytes:
    return b"".join(int(value).to_bytes(2, "big", signed=True) for value in values)


def gds_int4(values: list[int]) -> bytes:
    return b"".join(int(value).to_bytes(4, "big", signed=True) for value in values)


def gds_string(value: str) -> bytes:
    raw = value.encode("ascii", errors="ignore") or b"X"
    return raw if len(raw) % 2 == 0 else raw + b"\0"


def gds_real8(value: float) -> bytes:
    if value == 0:
        return b"\0" * 8
    sign = 0x80 if value < 0 else 0
    normalized = abs(value)
    exponent = 64
    while normalized >= 1.0:
        normalized /= 16.0
        exponent += 1
    while normalized < 1.0 / 16.0:
        normalized *= 16.0
        exponent -= 1
    mantissa = int(normalized * (1 << 56) + 0.5)
    if mantissa >= (1 << 56):
        mantissa >>= 4
        exponent += 1
    return bytes([sign | exponent]) + mantissa.to_bytes(7, "big")


def gds_boundary(layer: int, datatype: int, points_nm: list[tuple[float, float]]) -> bytes:
    closed = points_nm if points_nm[0] == points_nm[-1] else [*points_nm, points_nm[0]]
    xy_values: list[int] = []
    for x_nm, y_nm in closed:
        xy_values.extend([round(x_nm), round(y_nm)])
    return b"".join(
        [
            gds_record(0x08, 0x00),
            gds_record(0x0D, 0x02, gds_int2([layer])),
            gds_record(0x0E, 0x02, gds_int2([datatype])),
            gds_record(0x10, 0x03, gds_int4(xy_values)),
            gds_record(0x11, 0x00),
        ]
    )


def write_basic_dc_gds(spec: DirectionalCouplerSpec, path: Path) -> Path:
    spec.validate()
    now = datetime.now()
    date_values = [now.year, now.month, now.day, now.hour, now.minute, now.second] * 2
    half_length_nm = spec.total_length_um * 1000.0 / 2.0
    half_width_nm = spec.width_nm / 2.0
    center_offset_nm = (spec.width_nm + spec.gap_nm) / 2.0
    top = [
        (-half_length_nm, center_offset_nm - half_width_nm),
        (half_length_nm, center_offset_nm - half_width_nm),
        (half_length_nm, center_offset_nm + half_width_nm),
        (-half_length_nm, center_offset_nm + half_width_nm),
    ]
    bottom = [
        (-half_length_nm, -center_offset_nm - half_width_nm),
        (half_length_nm, -center_offset_nm - half_width_nm),
        (half_length_nm, -center_offset_nm + half_width_nm),
        (-half_length_nm, -center_offset_nm + half_width_nm),
    ]
    payload = b"".join(
        [
            gds_record(0x00, 0x02, gds_int2([600])),
            gds_record(0x01, 0x02, gds_int2(date_values)),
            gds_record(0x02, 0x06, gds_string("PHOTONICS_AGENT")),
            gds_record(0x03, 0x05, gds_real8(0.001) + gds_real8(1e-9)),
            gds_record(0x05, 0x02, gds_int2(date_values)),
            gds_record(0x06, 0x06, gds_string("DIRECTIONAL_COUPLER")),
            gds_boundary(1, 0, top),
            gds_boundary(1, 0, bottom),
            gds_record(0x07, 0x00),
            gds_record(0x04, 0x00),
        ]
    )
    path.write_bytes(payload)
    return path


def write_dc_preview(spec: DirectionalCouplerSpec, output_path: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    spec.validate()
    canvas = Image.new("RGB", (900, 360), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    left, right = 80, 820
    center_y = 180
    scale_y = 0.20
    half_width_px = max(4, spec.width_nm * scale_y / 2)
    center_offset_px = (spec.width_nm + spec.gap_nm) * scale_y / 2
    rail_color = (20, 95, 170)
    for sign in (-1, 1):
        cy = center_y + sign * center_offset_px
        draw.rectangle((left, cy - half_width_px, right, cy + half_width_px), fill=rail_color, outline=(10, 50, 100))
    draw.line((left, center_y, right, center_y), fill=(180, 180, 180), width=1)
    draw.text((80, 28), "Directional coupler GDS preview", fill="black", font=font)
    draw.text((80, 310), f"width={spec.width_nm:g} nm, gap={spec.gap_nm:g} nm, coupling length={spec.coupling_length_um:g} um", fill="black", font=font)
    draw.text((80, 330), f"total straight length={spec.total_length_um:g} um, target split={spec.target_split_ratio:.3f}", fill="black", font=font)
    canvas.save(output_path)
    return output_path


def write_fdtd_test_preview(spec: FdtdTestSpec, output_path: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    spec.validate()
    canvas = Image.new("RGB", (820, 360), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    left, right = 90, 730
    center_y = 175
    height_px = max(18, spec.width_nm * 0.12)
    draw.rectangle((left, center_y - height_px / 2, right, center_y + height_px / 2), fill=(25, 95, 170), outline=(10, 50, 100))
    source_x = left + 0.15 * (right - left)
    monitor_x = left + 0.85 * (right - left)
    draw.line((source_x, 80, source_x, 270), fill=(230, 90, 40), width=3)
    draw.line((monitor_x, 80, monitor_x, 270), fill=(40, 150, 80), width=3)
    draw.text((90, 28), "Minimal FDTD project preview", fill="black", font=font)
    draw.text((source_x - 25, 60), "source", fill="black", font=font)
    draw.text((monitor_x - 28, 60), "monitor", fill="black", font=font)
    draw.text((90, 310), f"width={spec.width_nm:g} nm, height={spec.height_nm:g} nm, length={spec.length_um:g} um", fill="black", font=font)
    draw.text((90, 330), f"wavelength={spec.wavelength_nm:g} nm, project-only smoke test, no run", fill="black", font=font)
    canvas.save(output_path)
    return output_path


def dc_split_power(delta_neff: float | None, length_um: float, wavelength_nm: float) -> float | None:
    if delta_neff is None or delta_neff <= 0:
        return None
    phase = math.pi * delta_neff * (length_um * 1000.0) / wavelength_nm
    return math.sin(phase) ** 2


def dc_target_length_um(delta_neff: float | None, target_split_ratio: float, wavelength_nm: float) -> float | None:
    if delta_neff is None or delta_neff <= 0:
        return None
    phase = math.asin(math.sqrt(target_split_ratio))
    return phase * wavelength_nm / (math.pi * delta_neff) / 1000.0


def dc_supermode_parity_check(sim, spec: DirectionalCouplerSpec) -> dict[str, Any]:
    try:
        import numpy as np
    except Exception as exc:
        return {"verified": False, "reason": f"numpy unavailable: {exc}"}

    checks: dict[str, Any] = {}
    for mode_name in ("mode1", "mode2"):
        try:
            dataset = sim.getresult(mode_name, "E")
            x = np.squeeze(np.asarray(dataset["x"]))
            y = np.squeeze(np.asarray(dataset["y"]))
            field = np.asarray(dataset["E"])
            field = np.squeeze(field)
            while field.ndim > 3:
                field = field[..., 0, :]
            if field.ndim != 3 or field.shape[-1] != 3:
                checks[mode_name] = {"ok": False, "reason": f"unexpected field shape {field.shape}"}
                continue

            ix_left = int(np.argmin(np.abs(x + spec.center_offset_m)))
            ix_right = int(np.argmin(np.abs(x - spec.center_offset_m)))
            iy = int(np.argmin(np.abs(y - spec.rib_center_y_m)))
            left_components = field[ix_left, iy, :]
            right_components = field[ix_right, iy, :]
            component_index = int(np.argmax(np.abs(left_components) + np.abs(right_components)))
            left = left_components[component_index]
            right = right_components[component_index]
            denom = abs(left) * abs(right)
            if denom <= 0:
                checks[mode_name] = {"ok": False, "reason": "zero field sample"}
                continue
            correlation = float(np.real(right * np.conj(left)) / denom)
            phase_deg = float(math.degrees(math.atan2((right / left).imag, (right / left).real))) if abs(left) > 0 else None
            parity = "even" if correlation > 0.5 else "odd" if correlation < -0.5 else "mixed"
            checks[mode_name] = {
                "ok": True,
                "parity": parity,
                "correlation": correlation,
                "relative_phase_deg": phase_deg,
                "component_index": component_index,
                "sample_left_x_um": float(x[ix_left] * 1e6),
                "sample_right_x_um": float(x[ix_right] * 1e6),
                "sample_y_um": float(y[iy] * 1e6),
            }
        except Exception as exc:
            checks[mode_name] = {"ok": False, "reason": str(exc)}

    mode1 = checks.get("mode1", {})
    mode2 = checks.get("mode2", {})
    verified = (
        mode1.get("parity") == "even"
        and mode2.get("parity") == "odd"
        and abs(float(mode1.get("correlation", 0.0))) > 0.8
        and abs(float(mode2.get("correlation", 0.0))) > 0.8
    )
    return {"verified": verified, "modes": checks}


def live_dc_supermode_analysis(spec: DirectionalCouplerSpec, output_dir: Path) -> dict[str, Any]:
    spec.validate()
    lumapi, api_dir = load_lumapi(spec.lumapi_dir)
    output_dir = ensure_directory(output_dir)
    project_path = output_dir / "dc_supermode_fde.lms"
    result_payload: dict[str, Any] = {
        "api_dir": str(api_dir),
        "project_path": str(project_path.resolve()),
        "spec": asdict(spec),
    }

    sim = lumapi.MODE(hide=spec.hide)
    try:
        sim.eval(build_dc_supermode_script(spec, project_path, include_solve=False))
        modes_found = int(sim.findmodes())
        sim.save(str(project_path))

        neff_1 = safe_metric(sim, "mode1", "neff") if modes_found > 0 else None
        neff_2 = safe_metric(sim, "mode2", "neff") if modes_found > 1 else None
        delta_neff = abs(neff_1 - neff_2) if neff_1 is not None and neff_2 is not None else None
        l50_um = dc_target_length_um(delta_neff, 0.5, spec.wavelength_nm)
        target_length_um = dc_target_length_um(delta_neff, spec.target_split_ratio, spec.wavelength_nm)
        layout_cross_power = dc_split_power(delta_neff, spec.coupling_length_um, spec.wavelength_nm)
        supermode_check = dc_supermode_parity_check(sim, spec) if modes_found > 1 else {"verified": False, "reason": "fewer than two modes found"}

        result_payload.update(
            {
                "ok": True,
                "modes_found": modes_found,
                "supermode_check": supermode_check,
                "metrics": {
                    "neff_mode_1": neff_1,
                    "neff_mode_2": neff_2,
                    "delta_neff": delta_neff,
                    "l50_um": l50_um,
                    "target_length_um": target_length_um,
                    "layout_cross_power": layout_cross_power,
                    "layout_through_power": 1.0 - layout_cross_power if layout_cross_power is not None else None,
                },
            }
        )

        plot_path = maybe_write_mode_plot(sim, output_dir / "dc_mode1_intensity.png", structure=dc_structure_overlay(spec)) if modes_found > 0 else None
        if plot_path is not None:
            result_payload["mode_plot"] = str(plot_path.resolve())
        eme_project_path = output_dir / "dc_eme.lms"
        sim.eval(build_dc_eme_script(spec, eme_project_path))
        if eme_project_path.exists():
            result_payload["eme_project_path"] = str(eme_project_path.resolve())
        return result_payload
    finally:
        try:
            sim.close()
        except Exception:
            pass


def write_dynamic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def maybe_write_dc_sweep_plot(rows: list[dict[str, Any]], x_key: str, y_key: str, title: str, output_path: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    x_values = [float(row[x_key]) for row in rows if row.get(x_key) is not None and row.get(y_key) is not None]
    y_values = [float(row[y_key]) for row in rows if row.get(x_key) is not None and row.get(y_key) is not None]
    if not x_values or not y_values:
        return None

    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    if math.isclose(x_min, x_max):
        x_max = x_min + 1.0
    if math.isclose(y_min, y_max):
        y_max = y_min + 0.1

    canvas = Image.new("RGB", (820, 540), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    left, top, right, bottom = 95, 55, 760, 430
    draw.rectangle((left, top, right, bottom), outline="black", width=2)
    draw.text((95, 20), title, fill="black", font=font)
    draw.text((330, 485), x_key, fill="black", font=font)
    draw.text((20, 235), y_key, fill="black", font=font)

    def map_x(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (right - left)

    def map_y(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    points = [(map_x(x), map_y(y)) for x, y in zip(x_values, y_values)]
    if len(points) >= 2:
        draw.line(points, fill=(30, 90, 200), width=3)
    for point in points:
        draw.ellipse((point[0] - 4, point[1] - 4, point[0] + 4, point[1] + 4), fill=(220, 40, 40))
    draw.text((95, 440), f"x: {x_min:g} to {x_max:g}", fill="black", font=font)
    draw.text((95, 460), f"y: {y_min:.4g} to {y_max:.4g}", fill="black", font=font)
    canvas.save(output_path)
    return output_path


def live_dc_sweep_analysis(spec: DirectionalCouplerSweepSpec, output_dir: Path) -> dict[str, Any]:
    spec.validate()
    output_dir = ensure_directory(output_dir)
    rows: list[dict[str, Any]] = []

    if spec.parameter == "gap_nm":
        representative_lms: str | None = None
        representative_eme_lms: str | None = None
        for value in spec.values():
            point_spec = spec.to_dc_spec(value)
            point_dir = ensure_directory(output_dir / f"gap_{value:g}nm")
            point_result = live_dc_supermode_analysis(point_spec, point_dir)
            if representative_lms is None and point_result.get("project_path"):
                representative_lms = point_result["project_path"]
            if representative_eme_lms is None and point_result.get("eme_project_path"):
                representative_eme_lms = point_result["eme_project_path"]
            metrics = point_result.get("metrics", {}) or {}
            rows.append(
                {
                    "gap_nm": value,
                    "modes_found": point_result.get("modes_found"),
                    "neff_mode_1": metrics.get("neff_mode_1"),
                    "neff_mode_2": metrics.get("neff_mode_2"),
                    "delta_neff": metrics.get("delta_neff"),
                    "l50_um": metrics.get("l50_um"),
                    "target_length_um": metrics.get("target_length_um"),
                    "cross_power_at_layout_length": metrics.get("layout_cross_power"),
                    "status": "ok",
                }
            )
        csv_path = output_dir / "dc_gap_sweep.csv"
        write_dynamic_csv(csv_path, rows)
        plot_path = maybe_write_dc_sweep_plot(rows, "gap_nm", "l50_um", "Directional coupler gap sweep", output_dir / "dc_gap_sweep_l50.png")
        payload = {
            "ok": True,
            "parameter": spec.parameter,
            "rows": rows,
            "csv_path": str(csv_path.resolve()),
            "plot_path": str(plot_path.resolve()) if plot_path is not None else None,
            "representative_lms": representative_lms,
            "representative_eme_lms": representative_eme_lms,
        }
        write_json(output_dir / "dc_sweep_summary.json", payload)
        return payload

    base_dir = ensure_directory(output_dir / "base_supermode")
    base_result = live_dc_supermode_analysis(spec.base, base_dir)
    metrics = base_result.get("metrics", {}) or {}
    delta_neff = metrics.get("delta_neff")
    for value in spec.values():
        cross_power = dc_split_power(delta_neff, value, spec.base.wavelength_nm)
        rows.append(
            {
                "coupling_length_um": value,
                "cross_power": cross_power,
                "through_power": 1.0 - cross_power if cross_power is not None else None,
                "target_error": abs(cross_power - spec.base.target_split_ratio) if cross_power is not None else None,
                "delta_neff": delta_neff,
                "status": "ok" if cross_power is not None else "no_delta_neff",
            }
        )
    csv_path = output_dir / "dc_length_sweep.csv"
    write_dynamic_csv(csv_path, rows)
    plot_path = maybe_write_dc_sweep_plot(rows, "coupling_length_um", "cross_power", "Directional coupler length sweep", output_dir / "dc_length_sweep_power.png")
    valid_rows = [row for row in rows if row.get("target_error") is not None]
    best_row = min(valid_rows, key=lambda row: row["target_error"]) if valid_rows else None
    best_gds = None
    best_preview = None
    if best_row is not None:
        best_spec = spec.to_dc_spec(float(best_row["coupling_length_um"]))
        best_gds = write_basic_dc_gds(best_spec, output_dir / "directional_coupler_best_length.gds")
        best_preview = write_dc_preview(best_spec, output_dir / "directional_coupler_best_length.png")
    payload = {
        "ok": True,
        "parameter": spec.parameter,
        "base_result": base_result,
        "rows": rows,
        "best_row": best_row,
        "csv_path": str(csv_path.resolve()),
        "plot_path": str(plot_path.resolve()) if plot_path is not None else None,
        "best_gds": str(best_gds.resolve()) if best_gds is not None else None,
        "best_preview": str(best_preview.resolve()) if best_preview is not None else None,
        "representative_lms": base_result.get("project_path"),
        "representative_eme_lms": base_result.get("eme_project_path"),
    }
    write_json(output_dir / "dc_sweep_summary.json", payload)
    return payload


def live_fdtd_test_analysis(spec: FdtdTestSpec, output_dir: Path) -> dict[str, Any]:
    spec.validate()
    lumapi, api_dir = load_lumapi(spec.lumapi_dir)
    output_dir = ensure_directory(output_dir)
    project_path = output_dir / "fdtd_test.fsp"
    result_payload: dict[str, Any] = {
        "api_dir": str(api_dir),
        "project_path": str(project_path.resolve()),
        "spec": asdict(spec),
    }

    sim = lumapi.FDTD(hide=spec.hide)
    try:
        sim.eval(build_fdtd_test_script(spec, project_path))
        result_payload["ok"] = True
        result_payload["message"] = "FDTD smoke-test project was created without running time-domain simulation."
        return result_payload
    finally:
        try:
            sim.close()
        except Exception:
            pass


def summarize_dc_result(result: dict[str, Any], spec: DirectionalCouplerSpec, fallback_reason: str | None = None) -> str:
    if fallback_reason:
        return (
            f"directional coupler 작업 파일을 준비했습니다. gap {spec.gap_nm:g} nm, "
            f"초기 coupling length {spec.coupling_length_um:g} um 기준이며, live supermode 실행은 완료되지 않았습니다. "
            f"이유는 {fallback_reason} 입니다."
        )
    metrics = result.get("metrics", {}) or {}
    message = (
        f"directional coupler 기본 설계를 생성했습니다. width {spec.width_nm:g} nm, gap {spec.gap_nm:g} nm, "
        f"height {spec.height_nm:g} nm, wavelength {spec.wavelength_nm:g} nm 기준입니다."
    )
    if metrics.get("delta_neff") is not None:
        message += f" supermode delta neff는 {metrics['delta_neff']:.6g} 입니다."
    supermode_check = result.get("supermode_check", {}) or {}
    if supermode_check.get("verified"):
        mode_checks = supermode_check.get("modes", {}) or {}
        mode1_corr = ((mode_checks.get("mode1", {}) or {}).get("correlation"))
        mode2_corr = ((mode_checks.get("mode2", {}) or {}).get("correlation"))
        if mode1_corr is not None and mode2_corr is not None:
            message += f" mode1/mode2는 even/odd supermode로 확인됐고 parity correlation은 {mode1_corr:.3f}/{mode2_corr:.3f} 입니다."
        else:
            message += " mode1/mode2는 even/odd supermode로 확인됐습니다."
    if metrics.get("target_length_um") is not None:
        message += f" 목표 split 기준 coupling length 추정값은 {metrics['target_length_um']:.3f} um 입니다."
    if metrics.get("layout_cross_power") is not None:
        message += f" 현재 GDS 길이에서는 cross power가 약 {metrics['layout_cross_power']:.3f} 입니다."
    return message


def summarize_dc_sweep_result(result: dict[str, Any], spec: DirectionalCouplerSweepSpec, fallback_reason: str | None = None) -> str:
    if fallback_reason:
        label = "gap" if spec.parameter == "gap_nm" else "coupling length"
        return f"directional coupler {label} sweep 파일을 준비했습니다. live 실행은 완료되지 않았고, 이유는 {fallback_reason} 입니다."
    if spec.parameter == "gap_nm":
        return f"directional coupler gap sweep를 실행했습니다. {spec.start:g}~{spec.stop:g} nm 범위에서 {len(spec.values())}개 점을 계산했습니다."
    best = result.get("best_row") or {}
    message = f"directional coupler coupling length sweep를 실행했습니다. {spec.start:g}~{spec.stop:g} um 범위에서 {len(spec.values())}개 점을 계산했습니다."
    if best.get("coupling_length_um") is not None and best.get("cross_power") is not None:
        message += f" 목표 split에 가장 가까운 길이는 {best['coupling_length_um']:g} um이고 cross power는 {best['cross_power']:.3f} 입니다."
    return message


def summarize_fdtd_test_result(result: dict[str, Any], spec: FdtdTestSpec, fallback_reason: str | None = None) -> str:
    if fallback_reason:
        return (
            f"간단한 FDTD 테스트 프로젝트 파일을 준비했습니다. width {spec.width_nm:g} nm, "
            f"height {spec.height_nm:g} nm, length {spec.length_um:g} um 기준이며 live FDTD 저장은 완료되지 않았습니다. "
            f"이유는 {fallback_reason} 입니다."
        )
    return (
        f"간단한 FDTD 테스트 프로젝트를 생성했습니다. width {spec.width_nm:g} nm, height {spec.height_nm:g} nm, "
        f"length {spec.length_um:g} um, wavelength {spec.wavelength_nm:g} nm 기준입니다. "
        "시간영역 run은 하지 않고 .fsp 저장까지만 수행했습니다."
    )


def review_artifacts(paths: list[str]) -> list[str]:
    issues: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            issues.append(f"missing artifact: {path}")
        elif path.is_file() and path.stat().st_size == 0:
            issues.append(f"empty artifact: {path}")
    return issues


def execute_dc_command(spec: DirectionalCouplerSpec, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False, raw_request: str | None = None, assumptions: list[str] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    spec.validate()
    run_dir = ensure_directory(output_dir or (output_root() / f"dc-{timestamp()}"))
    spec_path = run_dir / "directional_coupler_spec.json"
    write_json(spec_path, asdict(spec))
    design_path = write_design_yaml(run_dir, directional_coupler_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))
    supermode_script, eme_script = write_dc_scripts(spec, run_dir)
    gds_path = write_basic_dc_gds(spec, run_dir / "directional_coupler.gds")
    preview_path = write_dc_preview(spec, run_dir / "directional_coupler_preview.png")

    attachments = [
        str(path.resolve())
        for path in [preview_path, gds_path, eme_script, supermode_script, spec_path, design_path]
        if path is not None
    ]
    issues = review_artifacts(attachments)
    result: dict[str, Any] = {
        "ok": True,
        "status": "script_only",
        "result_dir": str(run_dir.resolve()),
        "attachments": attachments,
    }

    if script_only:
        message = summarize_dc_result(result, spec, fallback_reason="script-only mode")
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": "directional_coupler", "route": "script_only"},
            spec_path,
            design_path,
            attachments,
            {"status": "skipped"},
            {"message": message},
            issues=issues,
        )
        result["message"] = message
        result["attachments"].append(str(pipeline_path.resolve()))
        return add_report_context(result, directional_coupler_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))

    live_ok, live_error = run_live_subprocess("_live_dc", spec_path, run_dir, timeout_s=timeout_s)
    if live_ok:
        live_result = json.loads((run_dir / "live_result.json").read_text(encoding="utf-8"))
        metrics = live_result.get("metrics", {}) or {}
        target_length_um = metrics.get("target_length_um")
        if target_length_um is not None and 0 < target_length_um < 10000:
            target_data = asdict(spec)
            target_data["coupling_length_um"] = target_length_um
            target_data["coupling_length_source"] = "estimated_from_supermodes"
            target_spec = DirectionalCouplerSpec(**target_data)
            target_gds = write_basic_dc_gds(target_spec, run_dir / "directional_coupler_target_split.gds")
            target_preview = write_dc_preview(target_spec, run_dir / "directional_coupler_target_split.png")
            for path in [target_preview, target_gds]:
                if path is not None:
                    attachments.insert(0, str(path.resolve()))
        if live_result.get("mode_plot"):
            attachments.insert(0, live_result["mode_plot"])
        if live_result.get("eme_project_path"):
            attachments.append(live_result["eme_project_path"])
        lms_path = run_dir / "dc_supermode_fde.lms"
        if lms_path.exists():
            attachments.append(str(lms_path.resolve()))
        message = summarize_dc_result(live_result, spec)
        summary_path = run_dir / "summary.json"
        write_json(summary_path, {"message": message, "spec": asdict(spec), "result": live_result})
        attachments.append(str(summary_path.resolve()))
        issues = review_artifacts(attachments)
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": "directional_coupler", "route": "live_supermode"},
            spec_path,
            design_path,
            attachments,
            {"status": "solved", "live_result": str((run_dir / "live_result.json").resolve())},
            {"message": message, "metrics": metrics},
            issues=issues,
        )
        attachments.append(str(pipeline_path.resolve()))
        result.update({"status": "solved", "message": message, "live_result": live_result, "attachments": attachments})
        return add_report_context(result, directional_coupler_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))

    message = summarize_dc_result(result, spec, fallback_reason=live_error or "unknown error")
    summary_path = run_dir / "summary.json"
    write_json(summary_path, {"message": message, "spec": asdict(spec), "result": result, "live_error": live_error})
    attachments.append(str(summary_path.resolve()))
    pipeline_path = write_agent_pipeline(
        run_dir,
        {"kind": "directional_coupler", "route": "fallback"},
        spec_path,
        design_path,
        attachments,
        {"status": "fallback", "live_error": live_error},
        {"message": message},
        issues=review_artifacts(attachments),
    )
    attachments.append(str(pipeline_path.resolve()))
    result.update({"status": "fallback", "message": message, "live_error": live_error, "attachments": attachments})
    return add_report_context(result, directional_coupler_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))


def execute_dc_sweep_command(spec: DirectionalCouplerSweepSpec, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False, raw_request: str | None = None, assumptions: list[str] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    spec.validate()
    run_dir = ensure_directory(output_dir or (output_root() / f"dc-sweep-{timestamp()}"))
    spec_path = run_dir / "directional_coupler_sweep_spec.json"
    write_json(spec_path, asdict(spec))
    design_path = write_design_yaml(run_dir, directional_coupler_sweep_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))
    supermode_script, eme_script = write_dc_scripts(spec.base, run_dir)
    gds_path = write_basic_dc_gds(spec.base, run_dir / "directional_coupler_seed.gds")
    preview_path = write_dc_preview(spec.base, run_dir / "directional_coupler_seed.png")
    attachments = [
        str(path.resolve())
        for path in [preview_path, gds_path, eme_script, supermode_script, spec_path, design_path]
        if path is not None
    ]
    result: dict[str, Any] = {
        "ok": True,
        "status": "script_only",
        "result_dir": str(run_dir.resolve()),
        "attachments": attachments,
    }

    if script_only:
        message = summarize_dc_sweep_result(result, spec, fallback_reason="script-only mode")
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": "directional_coupler_sweep", "parameter": spec.parameter, "route": "script_only"},
            spec_path,
            design_path,
            attachments,
            {"status": "skipped"},
            {"message": message},
            issues=review_artifacts(attachments),
        )
        result["message"] = message
        result["attachments"].append(str(pipeline_path.resolve()))
        return add_report_context(result, directional_coupler_sweep_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))

    live_ok, live_error = run_live_subprocess("_live_dc_sweep", spec_path, run_dir, timeout_s=timeout_s)
    if live_ok:
        live_result = json.loads((run_dir / "live_result.json").read_text(encoding="utf-8"))
        for key in ["csv_path", "plot_path", "best_gds", "best_preview", "representative_lms", "representative_eme_lms"]:
            value = live_result.get(key)
            if value:
                attachments.insert(0, value)
        message = summarize_dc_sweep_result(live_result, spec)
        summary_path = run_dir / "summary.json"
        write_json(summary_path, {"message": message, "spec": asdict(spec), "result": live_result})
        attachments.append(str(summary_path.resolve()))
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": "directional_coupler_sweep", "parameter": spec.parameter, "route": "live_supermode"},
            spec_path,
            design_path,
            attachments,
            {"status": "solved", "live_result": str((run_dir / "live_result.json").resolve())},
            {"message": message},
            issues=review_artifacts(attachments),
        )
        attachments.append(str(pipeline_path.resolve()))
        result.update({"status": "solved", "message": message, "live_result": live_result, "attachments": attachments})
        return add_report_context(result, directional_coupler_sweep_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))

    message = summarize_dc_sweep_result(result, spec, fallback_reason=live_error or "unknown error")
    summary_path = run_dir / "summary.json"
    write_json(summary_path, {"message": message, "spec": asdict(spec), "result": result, "live_error": live_error})
    attachments.append(str(summary_path.resolve()))
    pipeline_path = write_agent_pipeline(
        run_dir,
        {"kind": "directional_coupler_sweep", "parameter": spec.parameter, "route": "fallback"},
        spec_path,
        design_path,
        attachments,
        {"status": "fallback", "live_error": live_error},
        {"message": message},
        issues=review_artifacts(attachments),
    )
    attachments.append(str(pipeline_path.resolve()))
    result.update({"status": "fallback", "message": message, "live_error": live_error, "attachments": attachments})
    return add_report_context(result, directional_coupler_sweep_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes))


def execute_fdtd_test_command(spec: FdtdTestSpec, output_dir: Path | None = None, timeout_s: int = DEFAULT_TIMEOUT_S, script_only: bool = False, raw_request: str | None = None, assumptions: list[str] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    spec.validate()
    run_dir = ensure_directory(output_dir or (output_root() / f"fdtd-test-{timestamp()}"))
    spec_path = run_dir / "fdtd_test_spec.json"
    write_json(spec_path, asdict(spec))
    dsl = fdtd_test_spec_to_dsl(spec, raw_request=raw_request, assumptions=assumptions, notes=notes)
    design_path = write_design_yaml(run_dir, dsl)
    script_path = write_fdtd_test_script(spec, run_dir)
    preview_path = write_fdtd_test_preview(spec, run_dir / "fdtd_test_preview.png")
    attachments = [
        str(path.resolve())
        for path in [preview_path, script_path, spec_path, design_path]
        if path is not None
    ]
    result: dict[str, Any] = {
        "ok": True,
        "status": "script_only",
        "result_dir": str(run_dir.resolve()),
        "attachments": attachments,
    }

    if script_only:
        message = summarize_fdtd_test_result(result, spec, fallback_reason="script-only mode")
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": "fdtd_test", "route": "script_only"},
            spec_path,
            design_path,
            attachments,
            {"status": "skipped"},
            {"message": message},
            issues=review_artifacts(attachments),
        )
        result["message"] = message
        result["attachments"].append(str(pipeline_path.resolve()))
        return add_report_context(result, dsl)

    live_ok, live_error = run_live_subprocess("_live_fdtd_test", spec_path, run_dir, timeout_s=timeout_s)
    if live_ok:
        live_result = json.loads((run_dir / "live_result.json").read_text(encoding="utf-8"))
        fsp_path = run_dir / "fdtd_test.fsp"
        if fsp_path.exists():
            attachments.append(str(fsp_path.resolve()))
        message = summarize_fdtd_test_result(live_result, spec)
        summary_path = run_dir / "summary.json"
        write_json(summary_path, {"message": message, "spec": asdict(spec), "result": live_result})
        attachments.append(str(summary_path.resolve()))
        pipeline_path = write_agent_pipeline(
            run_dir,
            {"kind": "fdtd_test", "route": "live_project_create"},
            spec_path,
            design_path,
            attachments,
            {"status": "created", "live_result": str((run_dir / "live_result.json").resolve())},
            {"message": message},
            issues=review_artifacts(attachments),
        )
        attachments.append(str(pipeline_path.resolve()))
        result.update({"status": "created", "message": message, "live_result": live_result, "attachments": attachments})
        return add_report_context(result, dsl)

    message = summarize_fdtd_test_result(result, spec, fallback_reason=live_error or "unknown error")
    summary_path = run_dir / "summary.json"
    write_json(summary_path, {"message": message, "spec": asdict(spec), "result": result, "live_error": live_error})
    attachments.append(str(summary_path.resolve()))
    pipeline_path = write_agent_pipeline(
        run_dir,
        {"kind": "fdtd_test", "route": "fallback"},
        spec_path,
        design_path,
        attachments,
        {"status": "fallback", "live_error": live_error},
        {"message": message},
        issues=review_artifacts(attachments),
    )
    attachments.append(str(pipeline_path.resolve()))
    result.update({"status": "fallback", "message": message, "live_error": live_error, "attachments": attachments})
    return add_report_context(result, dsl)


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
            "/dc width=500 height=220 gap=200 coupling_length=20",
            "/dc_sweep parameter=gap start=100 stop=300 step=50 width=500 height=220",
            "/fdtd_test width=500 height=220 length=2",
            "기본적인 etch depth 220nm SOI waveguide mode profile 그려줘",
            "50대 50 directional coupler 기본 설계하고 GDS랑 시뮬레이션 파일 보내줘",
            "아주 간단한 FDTD 테스트 프로젝트 만들어줘",
        ],
    }

    try:
        lumapi, api_dir = load_lumapi(explicit)
        payload["lumapi_importable"] = True
        payload["lumapi_file"] = getattr(lumapi, "__file__", None)
        payload["selected_lumapi_dir"] = str(api_dir)
        payload["message"] = (
            "lumapi import는 가능합니다. 이제 /mode, /sweep, /dc, /dc_sweep, /fdtd_test 명령을 사용할 수 있습니다."
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

    dc_parser = subparsers.add_parser("dc", help="Run or prepare a directional coupler design")
    add_dc_arguments(dc_parser)
    dc_parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    dc_parser.add_argument("--script-only", action="store_true")
    dc_parser.add_argument("--output-dir", default=None)
    dc_parser.add_argument("--json", action="store_true")

    dc_sweep_parser = subparsers.add_parser("dc_sweep", help="Run or prepare a directional coupler gap/length sweep")
    add_dc_arguments(dc_sweep_parser)
    dc_sweep_parser.add_argument("--parameter", choices=["gap_nm", "coupling_length_um"], required=True)
    dc_sweep_parser.add_argument("--start", type=float, required=True)
    dc_sweep_parser.add_argument("--stop", type=float, required=True)
    dc_sweep_parser.add_argument("--step", type=float, required=True)
    dc_sweep_parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    dc_sweep_parser.add_argument("--script-only", action="store_true")
    dc_sweep_parser.add_argument("--output-dir", default=None)
    dc_sweep_parser.add_argument("--json", action="store_true")

    fdtd_parser = subparsers.add_parser("fdtd_test", help="Create a minimal FDTD smoke-test project")
    add_fdtd_test_arguments(fdtd_parser)
    fdtd_parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    fdtd_parser.add_argument("--script-only", action="store_true")
    fdtd_parser.add_argument("--output-dir", default=None)
    fdtd_parser.add_argument("--json", action="store_true")

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

    live_dc_parser = subparsers.add_parser("_live_dc")
    live_dc_parser.add_argument("--spec", required=True)
    live_dc_parser.add_argument("--output-dir", required=True)

    live_dc_sweep_parser = subparsers.add_parser("_live_dc_sweep")
    live_dc_sweep_parser.add_argument("--spec", required=True)
    live_dc_sweep_parser.add_argument("--output-dir", required=True)

    live_fdtd_test_parser = subparsers.add_parser("_live_fdtd_test")
    live_fdtd_test_parser.add_argument("--spec", required=True)
    live_fdtd_test_parser.add_argument("--output-dir", required=True)

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


def add_dc_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--width-nm", type=float, default=DEFAULT_MODE_WIDTH_NM)
    parser.add_argument("--height-nm", type=float, default=DEFAULT_DEVICE_LAYER_NM)
    parser.add_argument("--wavelength-nm", type=float, default=DEFAULT_WAVELENGTH_NM)
    parser.add_argument("--gap-nm", type=float, default=DEFAULT_DC_GAP_NM)
    parser.add_argument("--coupling-length-um", type=float, default=DEFAULT_DC_COUPLING_LENGTH_UM)
    parser.add_argument("--input-length-um", type=float, default=DEFAULT_DC_ACCESS_LENGTH_UM)
    parser.add_argument("--output-length-um", type=float, default=DEFAULT_DC_ACCESS_LENGTH_UM)
    parser.add_argument("--target-split-ratio", type=float, default=0.5)
    parser.add_argument("--slab-nm", type=float, default=0.0)
    parser.add_argument("--sidewall-angle-deg", type=float, default=90.0)
    parser.add_argument("--core-material", default="Si (Silicon) - Palik")
    parser.add_argument("--clad-material", default="SiO2 (Glass) - Palik")
    parser.add_argument("--trial-modes", type=int, default=8)
    parser.add_argument("--mesh-accuracy", type=int, default=3)
    parser.add_argument("--side-margin-um", type=float, default=2.0)
    parser.add_argument("--vertical-margin-um", type=float, default=1.5)
    parser.add_argument("--show-gui", action="store_true")
    parser.add_argument("--lumapi-dir", default=None)


def add_fdtd_test_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--width-nm", type=float, default=DEFAULT_MODE_WIDTH_NM)
    parser.add_argument("--height-nm", type=float, default=DEFAULT_DEVICE_LAYER_NM)
    parser.add_argument("--length-um", type=float, default=2.0)
    parser.add_argument("--wavelength-nm", type=float, default=DEFAULT_WAVELENGTH_NM)
    parser.add_argument("--core-material", default="Si (Silicon) - Palik")
    parser.add_argument("--clad-material", default="SiO2 (Glass) - Palik")
    parser.add_argument("--mesh-accuracy", type=int, default=1)
    parser.add_argument("--simulation-time-fs", type=float, default=50.0)
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


def dc_spec_from_args(args: argparse.Namespace) -> DirectionalCouplerSpec:
    return DirectionalCouplerSpec(
        width_nm=args.width_nm,
        height_nm=args.height_nm,
        wavelength_nm=args.wavelength_nm,
        gap_nm=args.gap_nm,
        coupling_length_um=args.coupling_length_um,
        input_length_um=args.input_length_um,
        output_length_um=args.output_length_um,
        target_split_ratio=args.target_split_ratio,
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
        coupling_length_source="user",
    )


def dc_sweep_spec_from_args(args: argparse.Namespace) -> DirectionalCouplerSweepSpec:
    return DirectionalCouplerSweepSpec(
        base=dc_spec_from_args(args),
        parameter=args.parameter,
        start=args.start,
        stop=args.stop,
        step=args.step,
    )


def fdtd_test_spec_from_args(args: argparse.Namespace) -> FdtdTestSpec:
    return FdtdTestSpec(
        width_nm=args.width_nm,
        height_nm=args.height_nm,
        length_um=args.length_um,
        wavelength_nm=args.wavelength_nm,
        core_material=args.core_material,
        clad_material=args.clad_material,
        mesh_accuracy=args.mesh_accuracy,
        simulation_time_fs=args.simulation_time_fs,
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

        if args.command == "dc":
            output_dir = Path(args.output_dir).resolve() if args.output_dir else None
            payload = execute_dc_command(
                dc_spec_from_args(args),
                output_dir=output_dir,
                timeout_s=args.timeout_s,
                script_only=args.script_only,
            )
            emit(payload, json_only=args.json)
            return 0

        if args.command == "dc_sweep":
            output_dir = Path(args.output_dir).resolve() if args.output_dir else None
            payload = execute_dc_sweep_command(
                dc_sweep_spec_from_args(args),
                output_dir=output_dir,
                timeout_s=args.timeout_s,
                script_only=args.script_only,
            )
            emit(payload, json_only=args.json)
            return 0

        if args.command == "fdtd_test":
            output_dir = Path(args.output_dir).resolve() if args.output_dir else None
            payload = execute_fdtd_test_command(
                fdtd_test_spec_from_args(args),
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

        if args.command == "_live_dc":
            spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            result = live_dc_supermode_analysis(DirectionalCouplerSpec(**spec_data), Path(args.output_dir))
            write_json(Path(args.output_dir) / "live_result.json", result)
            return 0

        if args.command == "_live_dc_sweep":
            spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            base = DirectionalCouplerSpec(**spec_data.pop("base"))
            result = live_dc_sweep_analysis(DirectionalCouplerSweepSpec(base=base, **spec_data), Path(args.output_dir))
            write_json(Path(args.output_dir) / "live_result.json", result)
            return 0

        if args.command == "_live_fdtd_test":
            spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            result = live_fdtd_test_analysis(FdtdTestSpec(**spec_data), Path(args.output_dir))
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

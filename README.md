# Telegram Photonics Bridge

Telegram 메시지를 받아 이 PC에서 Codex와 Lumerical API를 사용해 photonic design 작업을 처리하는 브리지입니다.

목표는 두 가지입니다.

- 반복적이고 검증된 작업은 deterministic helper로 빠르게 처리합니다.
- helper에 없는 component는 Codex multi-agent workflow로 DSL, Lumerical code, GDS, project file, preview, debug/refine까지 수행합니다.

## Current Architecture

```text
Telegram Bot
  -> Intent Agent
  -> Spec Agent
  -> Code Writer Agent
  -> Code Reviewer Agent
  -> Sandbox Runner
  -> Result Evaluator Agent
  -> Debug / Refine Agent
  -> Telegram Report + Files
```

현재 구현은 두 경로로 라우팅합니다.

### Known Helper Path

아래 작업은 `photonics_agent.py` deterministic helper가 직접 처리합니다.

- MODE 기반 strip / rib waveguide mode solve
- waveguide width sweep
- directional coupler 기본 설계
- directional coupler gap sweep / coupling length sweep
- directional coupler FDE supermode solve 및 even/odd parity check
- directional coupler GDS / preview / FDE `.lms` / EME `.lms` 생성
- 2x2 MMI 기본 layout / GDS / EME project skeleton 생성
- YAML DSL (`design.yaml`) 및 pipeline trace (`agent_pipeline.yaml`) 생성

### General Component Path

아래처럼 helper에 없는 component나 더 추상적인 요청은 `codex_photonics` 경로로 보냅니다.

- ring resonator
- grating coupler
- MZI / Mach-Zehnder
- AWG
- splitter / Y-branch
- photonic crystal / cavity
- taper / bend
- modulator
- component-specific FDTD / EME / MODE workflow

General path는 자연어를 바로 Lumerical code로 만들지 않고, 먼저 YAML DSL을 만들도록 프롬프트되어 있습니다. Component는 node, optical link / excitation / monitor는 edge로 표현합니다.

## Debug And Research Policy

- Lumerical/Codex 실행이 실패하면 `codex_photonics` 경로는 transcript의 stdout/stderr를 Debug / Refine prompt에 넣어 한 번 더 재실행합니다.
- Lumerical 에러 메시지는 Debug / Refine Agent의 1차 입력으로 취급합니다.
- local helper가 모르는 component/API/property는 공식 Ansys/Lumerical 문서를 우선 확인하도록 프롬프트되어 있습니다.
- 필요하면 공식 문서 스니펫을 `data/docs/lumerical/` 아래에 source URL과 함께 캐시할 수 있습니다.
- 3D FDTD, 긴 EME sweep, optimization loop, 큰 parameter sweep은 실행 전에 ETA와 조건을 보여주고 사용자 확인을 받아야 합니다.

주요 공식 문서:

- https://optics.ansys.com/hc/en-us/articles/360037824513-Python-API-overview
- https://optics.ansys.com/hc/en-us/articles/38660003331859-Lumerical-Python-API-Reference
- https://optics.ansys.com/hc/en-us/articles/360034923553
- https://developer.ansys.com/docs/lumerical/python-lumapi
- https://developer.ansys.com/docs/lumerical/scripting-language

## Telegram Usage

Structured commands:

```text
/photonics_status
```

```text
/mode width=500 height=220 wavelength=1550
```

```text
/mode width=700 height=220 slab=90 wavelength=1550
```

```text
/mode width=500 height=220 etch=220 angle=88 wavelength=1550
```

```text
/sweep start=400 stop=700 step=25 height=220 wavelength=1550
```

```text
/dc width=500 height=220 gap=200 coupling_length=20
```

```text
/dc_sweep parameter=gap start=100 stop=300 step=50 width=500 height=220
```

```text
/dc_sweep parameter=length start=5 stop=60 step=5 width=500 height=220 gap=200
```

```text
/mmi ports_in=2 ports_out=2 material=sin routing_width=1500 body_width=6 body_length=60
```

Natural language examples:

```text
기본적인 etch depth 220nm, sidewall angle 90도인 SOI waveguide의 mode profile 그려서 보내줘
```

```text
SOI waveguide width를 400nm부터 700nm까지 25nm step으로 sweep해서 neff 그래프도 같이 보내줘
```

```text
50대 50 directional coupler 기본 설계하고 GDS랑 시뮬레이션 파일 보내줘
```

```text
directional coupler gap sweep 100nm to 300nm step 50nm 해줘
```

```text
2 by 2 MMI design해줘. 물질은 SiN이고 routing waveguide는 1.5um야
```

```text
ring resonator design해줘. SiN platform 기준으로 GDS랑 Lumerical project 만들어줘
```

```text
grating coupler FDTD project 만들어줘. 실행 전에 ETA 확인해줘
```

## Defaults

자연어 요청에서 값이 빠지면 아래 기본값을 사용하거나, 설계를 크게 바꾸는 값이면 먼저 확인을 요청합니다.

- Waveguide width: `500 nm`
- Wavelength: `1550 nm`
- SOI device layer thickness: `220 nm`
- Sidewall angle: `90 deg`
- 기본 waveguide: full-etch strip
- Rib slab 기본값: `90 nm`
- Directional coupler gap: `200 nm`
- Directional coupler initial coupling length: `20 um`
- Directional coupler input/output straight length: `10 um`
- Directional coupler target split: `50:50`
- MMI routing waveguide width: `1500 nm`
- SiN MMI core thickness: `400 nm`
- MMI body width / length: `6 um` / `60 um`
- MMI taper / access length: `15 um` / `10 um`
- MMI port pitch: `2 um`

## Output Policy

작업 결과는 기본적으로 `data/photonics` 아래에 저장됩니다.

생성될 수 있는 파일:

- `design.yaml`
- `agent_pipeline.yaml`
- `parsed_request.json`
- `mode_spec.json` / `sweep_spec.json`
- `directional_coupler_spec.json` / `directional_coupler_sweep_spec.json`
- `mmi_spec.json`
- `mode_job.lsf` / `seed_mode.lsf`
- `dc_supermode_fde.lsf`
- `dc_eme.lsf`
- `mmi_eme.lsf`
- `mode_job.lms`
- `dc_supermode_fde.lms`
- `dc_eme.lms`
- `mmi_eme.lms`
- `summary.json`
- `mode1_intensity.png`
- `neff_vs_width.png`
- `directional_coupler_preview.png`
- `mmi_preview.png`
- `directional_coupler.gds`
- `mmi.gds`
- sweep CSV / plot files

Telegram으로는 기본적으로 아래만 보냅니다.

- Preview image
- `.gds`
- Lumerical project file: `.lms`, `.fsp`, `.ldev`, `.icp`

`design.yaml`, JSON, LSF, CSV, task memory는 workspace에 저장하지만 Telegram 메시지에는 길게 붙이지 않습니다. 유사 작업 이력도 내부 판단용으로만 사용하고 Telegram에 출력하지 않습니다.

## FDTD Policy

FDTD smoke-test helper는 제거했습니다.

FDTD 요청은 이제 deterministic helper가 아니라 general Codex workflow로 처리합니다. 즉, component-specific FDTD project/code를 만들고, 실행이 무거운 경우 ETA와 조건 확인 후 진행합니다.

## Local Setup

`.env.example`을 `.env`로 복사해서 사용합니다.

```powershell
Copy-Item .env.example .env
```

필수 값:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_IDS`
- `BRIDGE_WORKDIR`
- `CODEX_CMD`

선택 값:

- `LUMERICAL_PYTHON_API_DIR`
- `PHOTONICS_OUTPUT_DIR`
- `PHOTONICS_LIVE_TIMEOUT_S`
- `LUMERICAL_MODE_HIDE`
- `PHOTONICS_DEFAULT_WIDTH_NM`
- `PHOTONICS_DEFAULT_HEIGHT_NM`
- `PHOTONICS_DEFAULT_WAVELENGTH_NM`
- `PHOTONICS_DEFAULT_DC_GAP_NM`
- `PHOTONICS_DEFAULT_DC_COUPLING_LENGTH_UM`
- `PHOTONICS_DEFAULT_DC_ACCESS_LENGTH_UM`
- `PHOTONICS_SIN_MATERIAL`
- `PHOTONICS_DEFAULT_MMI_ROUTING_WIDTH_NM`
- `PHOTONICS_DEFAULT_MMI_SIN_HEIGHT_NM`
- `PHOTONICS_DEFAULT_MMI_BODY_WIDTH_UM`
- `PHOTONICS_DEFAULT_MMI_BODY_LENGTH_UM`
- `PHOTONICS_DEFAULT_MMI_TAPER_LENGTH_UM`
- `PHOTONICS_DEFAULT_MMI_ACCESS_LENGTH_UM`
- `PHOTONICS_DEFAULT_MMI_PORT_PITCH_UM`

## Running The Bridge

```powershell
.\run_bridge.ps1
```

현재 실행 상태 확인:

```powershell
.\check_bridge.ps1
```

## Local CLI Checks

```powershell
python -B .\photonics_agent.py env --json
```

```powershell
python -B .\photonics_agent.py mode --width-nm 500 --height-nm 220 --wavelength-nm 1550
```

```powershell
python -B .\photonics_agent.py sweep --width-start-nm 400 --width-stop-nm 700 --width-step-nm 25 --height-nm 220 --wavelength-nm 1550
```

```powershell
python -B .\photonics_agent.py dc --width-nm 500 --height-nm 220 --gap-nm 200 --coupling-length-um 20
```

```powershell
python -B .\photonics_agent.py dc_sweep --parameter gap_nm --start 100 --stop 300 --step 50 --width-nm 500 --height-nm 220
```

```powershell
python -B .\photonics_agent.py mmi --ports-in 2 --ports-out 2 --routing-width-nm 1500 --height-nm 400 --core-material "Si3N4 (Silicon Nitride) - Luke"
```

FDTD helper command는 없습니다. FDTD 관련 요청은 Telegram natural language 또는 Codex general photonics workflow를 통해 처리합니다.

# Telegram Photonics Bridge

텔레그램 메시지를 받아 이 PC에서 Codex와 Lumerical API를 사용해 포토닉 설계 작업을 처리하는 브리지입니다.

## 현재 기능

- 일반 자연어 요청은 `codex exec` 로 전달
- 포토닉 관련 자연어 요청은 `photonics_agent.py nl` 로 직접 처리
- 포토닉 구조화 명령은 `photonics_agent.py` 로 직접 처리
- 기본 지원 작업
  - `MODE` 기반 strip / rib waveguide mode solve
  - width sweep
  - directional coupler 기본 설계
  - directional coupler gap sweep / coupling length sweep
  - YAML DSL 기반 중간 spec (`design.yaml`)
  - stage별 멀티 에이전트 trace (`agent_pipeline.yaml`)
  - 외부 GDS 패키지 없이 기본 directional coupler `.gds` 생성
  - directional coupler용 EME 재현 스크립트와 FDE supermode sandbox solve
  - 간단한 FDTD smoke-test project 생성 (`.fsp`, run 없음)
  - `etch depth / slab / wg thickness / sidewall angle` 자연어 해석
  - mode profile PNG 자동 생성
  - 재현용 `.lsf`, `.lms`, spec, summary 자동 저장

## 텔레그램에서 쓰는 방법

구조화 명령 예시:

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
/fdtd_test width=500 height=220 length=2
```

자연어 예시:

```text
기본적인 etch depth 220nm, sidewall angle 90도인 SOI waveguide의 mode profile 그려서 보내줘
```

```text
기본적인 etch depth 300nm, sidewall angle 88도인 SOI waveguide의 mode profile 그려서 보내줘
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
50대 50 directional coupler coupling length sweep 5um to 60um step 5um 해줘
```

```text
아주 간단한 FDTD 테스트 프로젝트 만들어줘
```

## 기본값

자연어 요청에서 값이 빠지면 아래 기본값을 사용합니다.

- width: `500 nm`
- wavelength: `1550 nm`
- SOI device layer thickness: `220 nm`
- sidewall angle: `90 deg`
- 명시가 없으면 기본은 full-etch strip
- rib라고만 쓰면 slab 기본값은 `90 nm`
- directional coupler gap: `200 nm`
- directional coupler 초기 coupling length: `20 um`
- directional coupler input/output straight length: 각각 `10 um`
- directional coupler target split: 명시 없으면 `50:50`

해석 규칙 예시:

- `etch depth 220nm` + SOI waveguide
  - 기본 `220 nm` SOI 위의 full-etch strip로 해석
- `etch depth 300nm` + top silicon thickness 미지정
  - `300 nm` device layer full-etch 구조로 추론
- `wg thickness 220nm`
  - 기본적으로 top silicon / device layer thickness로 해석
- `slab 90nm`, `height 220nm`
  - partial-etch rib로 해석

## 출력 파일

포토닉 작업 결과는 `data/photonics` 아래에 저장됩니다.

일반적으로 아래 파일이 생깁니다.

- `mode_spec.json` 또는 `sweep_spec.json`
- `directional_coupler_spec.json` 또는 `directional_coupler_sweep_spec.json`
- `parsed_request.json`
- `design.yaml`
- `agent_pipeline.yaml`
- `mode_job.lsf` 또는 `seed_mode.lsf`
- `dc_supermode_fde.lsf`
- `dc_eme.lsf`
- `mode_job.lms`
- `summary.json`
- `mode1_intensity.png`
- `sweep_results.csv`
- `neff_vs_width.png`
- `directional_coupler.gds`
- `directional_coupler_preview.png`
- `dc_gap_sweep.csv`
- `dc_length_sweep.csv`
- `dc_gap_sweep_l50.png` 또는 `dc_length_sweep_power.png`

## YAML DSL / Agent Pipeline

자연어 요청은 바로 Python/GDS 코드로 가지 않고 먼저 `design.yaml` 로 정규화됩니다.

Telegram 동작은 아래 정책을 따릅니다.

- `design.yaml` 파일 자체와 DSL 요약은 Telegram 메시지에 붙이지 않습니다.
- 파일 전송은 기본적으로 이미지, `.gds`, Lumerical project 파일만 보냅니다.
- Lumerical project 파일은 solver에 따라 `.lms`, `.fsp`, `.ldev`, `.icp` 등을 허용합니다.
- YAML/JSON/LSF/CSV는 작업 폴더에 보존됩니다.
- mode profile 이미지는 waveguide 또는 coupler 구조 outline을 함께 오버레이합니다.
- 자연어 작업은 `data/photonics/task_memory.jsonl`에 기록됩니다.
- 유사 요청 기록은 내부 재사용 판단용으로만 보존하고 Telegram 메시지에는 표시하지 않습니다.
- FDTD 계열 작업은 실행 전에 ETA와 조건을 보여주고 `yes` / `ㅇㅇ` 확인을 받은 뒤 실행합니다.
- 자연어 요청이나 일부 구조화 명령에서 기본값/추론이 들어가면 먼저 확인을 요청합니다.
- 확인 요청을 취소하려면 `no` 또는 `취소`라고 보내면 됩니다.

DSL은 대략 아래 구조를 가집니다.

```yaml
version: 1
intent:
  component: "directional_coupler"
  task: "simulate_and_layout"
graph:
  nodes:
    -
      id: "dc1"
      type: "directional_coupler"
  edges:
    -
      from: "in_top"
      to: "dc1"
      kind: "optical_link"
simulation:
  method: "eme"
  sandbox_metric: "even_odd_supermode_delta_neff"
```

`agent_pipeline.yaml` 은 아래 stage 결과를 저장합니다.

```text
Telegram Bot
Intent Agent
Spec Agent
Code Writer Agent
Code Reviewer Agent
Sandbox Runner
Result Evaluator Agent
Debug / Refine Agent
Telegram Report + Files
```

현재 구현은 stage마다 별도 LLM 프로세스를 띄우는 방식이 아니라, 같은 CLI 안에서 deterministic agent stage로 실행됩니다. 그래서 재현성과 속도는 유지하면서, 나중에 각 stage를 독립 에이전트로 분리할 수 있습니다.

## 설정

`.env.example` 를 `.env` 로 복사해서 사용합니다.

```powershell
Copy-Item .env.example .env
```

필수 값:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_IDS`
- `BRIDGE_WORKDIR`
- `CODEX_CMD`

포토닉 관련 선택 값:

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

## 실행

```powershell
.\run_bridge.ps1
```

## 로컬 CLI 테스트

```powershell
python .\photonics_agent.py env --json
```

```powershell
python .\photonics_agent.py mode --width-nm 500 --height-nm 220 --wavelength-nm 1550
```

```powershell
python .\photonics_agent.py mode --width-nm 500 --height-nm 220 --sidewall-angle-deg 88 --wavelength-nm 1550
```

```powershell
python .\photonics_agent.py sweep --width-start-nm 400 --width-stop-nm 700 --width-step-nm 25 --height-nm 220 --wavelength-nm 1550
```

```powershell
python .\photonics_agent.py nl --request "기본적인 etch depth 220nm, sidewall angle 90도인 SOI waveguide의 mode profile 그려줘"
```

```powershell
python .\photonics_agent.py dc --width-nm 500 --height-nm 220 --gap-nm 200 --coupling-length-um 20
```

```powershell
python .\photonics_agent.py dc_sweep --parameter gap_nm --start 100 --stop 300 --step 50 --width-nm 500 --height-nm 220
```

```powershell
python .\photonics_agent.py dc_sweep --parameter coupling_length_um --start 5 --stop 60 --step 5 --width-nm 500 --height-nm 220 --gap-nm 200
```

```powershell
python .\photonics_agent.py fdtd_test --width-nm 500 --height-nm 220 --length-um 2
```

## 현재 범위

- 지금은 `MODE` 기반 waveguide, sweep, directional coupler supermode 해석이 중심입니다.
- directional coupler는 EME `.lsf` 를 생성하고, 자동 수치 검증은 FDE even/odd supermode의 `delta_neff` 로 수행합니다.
- directional coupler supermode는 mode1/mode2의 좌우 parity correlation으로 even/odd 여부를 같이 검증합니다.
- FDTD 테스트는 빠른 smoke test용으로 project 생성과 `.fsp` 저장까지만 수행하며 time-domain `run`은 하지 않습니다.
- `FDTD`, `INTERCONNECT`, MMI / ring / grating 전용 템플릿은 아직 미구현입니다.
- 더 복잡한 fabrication stack, 재료 DB 매핑, 공정 PDK 룰은 다음 단계에서 붙이면 됩니다.

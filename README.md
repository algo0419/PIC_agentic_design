# Telegram Photonics Bridge

텔레그램 메시지를 받아 이 PC에서 Codex와 Lumerical API를 사용해 포토닉 설계 작업을 처리하는 브리지입니다.

## 현재 기능

- 일반 자연어 요청은 `codex exec` 로 전달
- 포토닉 관련 자연어 요청은 `photonics_agent.py nl` 로 직접 처리
- 포토닉 구조화 명령은 `photonics_agent.py` 로 직접 처리
- 기본 지원 작업
  - `MODE` 기반 strip / rib waveguide mode solve
  - width sweep
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

## 기본값

자연어 요청에서 값이 빠지면 아래 기본값을 사용합니다.

- width: `500 nm`
- wavelength: `1550 nm`
- SOI device layer thickness: `220 nm`
- sidewall angle: `90 deg`
- 명시가 없으면 기본은 full-etch strip
- rib라고만 쓰면 slab 기본값은 `90 nm`

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
- `parsed_request.json`
- `mode_job.lsf` 또는 `seed_mode.lsf`
- `mode_job.lms`
- `summary.json`
- `mode1_intensity.png`
- `sweep_results.csv`
- `neff_vs_width.png`

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

## 현재 범위

- 지금은 `MODE` 기반 waveguide / sweep 쪽이 중심입니다.
- `FDTD`, `INTERCONNECT`, MMI / ring / grating 전용 템플릿은 아직 미구현입니다.
- 더 복잡한 fabrication stack, 재료 DB 매핑, 공정 PDK 룰은 다음 단계에서 붙이면 됩니다.

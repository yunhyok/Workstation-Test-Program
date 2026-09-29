# Workstation Test Program

SPD PI 계산의 수치 재현성과 CPU/GPU 자원 계획을 검증하는 Windows 프로그램입니다.

1.3.0 설치 후보: 독립된 실험은 중간 실패 후에도 계속 실행하고, 종료 시 결과 ZIP을 저장합니다. Python·계산 엔진·CPU/GPU 라이브러리를 포함하며 SPD·Touchstone 폴더만 선택하면 됩니다. PR 병합은 소유자가 진행합니다.

## 설치하고 시작하기

1. `Workstation-Test-Program-1.3.0-Setup-x64.exe`를 실행하고 시작 메뉴에서 프로그램을 엽니다.
2. **데이터 폴더**에서 SPD와 Touchstone 파일이 들어 있는 폴더를 선택합니다. 하위 폴더도 검색합니다.
3. **전체 실험 순차 실행 / 재개**를 누릅니다. 파일 준비 후 실험이 이어집니다. **폴더 적용 / 파일 준비**로 먼저 파일 연결 결과만 확인할 수도 있습니다.

Python 설치, pip 명령, 엔진 저장소, JSON 편집, 인터넷 연결은 설치 후 실행에 필요하지 않습니다.
인스톨러는 현재 사용자에게 설치되며 관리자 권한을 요구하지 않습니다. GPU 테스트에는 지원되는 NVIDIA GPU와 드라이버가 필요합니다. GPU가 감지되지 않으면 CPU 기준선은 실행하고 GPU 항목은 미실행으로 기록합니다. GPU가 있지만 계산에 실패하면 통과로 바꾸지 않습니다.

원본 SPD·Touchstone 파일은 수정하지 않습니다. 생성한 참조 NPZ, 설정, 계산 결과는 기본적으로
`%LOCALAPPDATA%/WorkstationTestProgram/w15`에 저장합니다. 기존 작업 폴더를 바꿀 수도 있습니다.
최초 파일 준비 시간은 데이터 크기에 따라 달라지며 이후에는 해시로 확인한 변환 결과를 재사용합니다.
설치/제거는 작업 데이터를 지우지 않습니다. 설치 파일은 코드 서명되지 않은 빌드입니다.

## 자동 파일 연결과 검증 범위

Touchstone의 `! Port[n]` 헤더와 SPD의 실제 포트·전원망을 대조합니다. 같은 이름 계열의 파일을 우선 연결하며 여러 후보가 남으면 오류로 표시합니다. 추측한 포트 연결로 계산하지 않습니다.
지원 입력은 PowerSI 형식의 `.spd`, `.sNp`, 그리고 같은 v1 형식에 완전한 포트 헤더를 가진 `.ts`입니다. 지원하지 않는 형식이나 중복 포트 매핑은 파일 준비 화면에 이유를 표시합니다.

P9/P20은 발견한 포트 중 최대 9/20개, P92는 **모든 설계의 전체 포트**, E4는 기저 비교용 한 포트입니다. P92라는 이름이 실제 92개를 뜻하지는 않습니다. 자동 생성한 목록은 기존 W15 사전 등록 설계·포트 목록과 구분합니다. 입력에 package/PCB 분류가 선언되지 않으면 임의로 추측하지 않고 엄격한 비교 허용치를 사용합니다.

설치 모드의 gates는 내장 엔진의 합성 CPU/GPU 풀이와 입력 파일의 포트·참조 정합성을 확인합니다. 과거 사전 등록 재현 테스트를 실행했다는 뜻은 아닙니다. 실제 설계와 참조의 정확도는 baseline 영수증과 보고서의 수치로 판단합니다. 노트북 영수증을 지정하지 않아도 실행되며, 기계 간 비교는 미설정으로 표시합니다.

CPU/GPU 라이브러리는 고정 버전으로 포함됩니다: Python 3.12.10, NumPy 2.4.4, SciPy 1.18.0.
전체 버전과 엔진 해시는 `engine-runtime/runtime-manifest.json`에 있습니다.

## 고급 설정과 기존 수동 실험

고급 설정은 선택 사항입니다. 노트북 영수증/freeze를 비교하거나 외부 엔진으로 기존 사전 등록 실험을 재현할 때만 사용합니다. `config.example.json`은 이 수동 모드의 예시이며 일반 설치 사용자는 복사할 필요가 없습니다.
수동 설정은 `configuration_mode: "manual"`로 지정하고 설계·포트·외부 Python과 엔진 경로를 넣습니다. 수동 모드의 gates는 기존 기본/GPU/slow 재현 테스트를 그대로 요구합니다. 엔진·환경·설정이 바뀌면 gates를 다시 실행해야 합니다.

## CLI

소스 실행은 아래 명령의 `WorkstationTest.exe`를 `python app.py`로 바꾸면 됩니다.
설치 경로의 `WorkstationTest.exe`는 콘솔용, `WorkstationTestProgram.exe`는 GUI용입니다.

## 모든 조건 자동 실행

GUI의 **전체 실험 순차 실행 / 재개**는 아래 10단계를 순서대로 실행합니다.
**선택 단계의 모든 조건 실행 / 재개**는 선택한 단계에 해당하는 행만 실행합니다.
예를 들어 baseline은 P9 다음 P92, matrix는 B → C → D → E를 자동 실행합니다.
실행 목록에 대기·실행 중·완료·실패·선행 조건 미충족·중단 상태가 표시됩니다. 진행률은 처리한 단계 수이며 완료·실패·건너뜀을 따로 표시합니다.

| 순서 | 단계 | 자동으로 적용하는 조건 |
| --- | --- | --- |
| 1–2 | env → gates | 환경 확인 → 설치 모드 입력·런타임 게이트 (수동 모드는 기존 재현 게이트) |
| 3–4 | baseline | P9 → P92, 각각 CPU 및 GPU 2회 비교 |
| 5 | matrix B | P9, threads 1/2/4/8/16 × jobs 1/4/8/15/30, 곱 ≤64 |
| 6 | matrix C | P92, 논리 CPU affinity 8/16/32/64; 호스트 초과 마스크는 미실행 기록 |
| 7 | matrix D | P92, RAM 플래너 64/128/256/512 GB |
| 8 | matrix E | P20 GPU jobs 1/2/4/8, VRAM 플래너 8/24/48 GB, E4 기저 비교 |
| 9–10 | converge → report | P92 fine 수렴성 → 보고서 생성 |

자동 실행은 폴더에서 생성한 포트 집합 또는 수동으로 등록한 포트 집합과 위 조건을 사용합니다.
화면의 포트·축 선택은 **선택 조건 수동 실행 / 재개**에만 적용됩니다.
전체 실행에는 비용이 큰 선택 실험 F도 포함되어 수 시간 이상 걸릴 수 있습니다.
폴더를 새로 준비하면 발견한 설계와 포트 목록이 갱신됩니다. 수치 계산 옵션은 유지합니다.

기준선 P9/P92, 매트릭스 B/C/D/E, 수렴성은 각각 필요한 계산을 수행하므로 한 단계가 실패해도 독립된 다음 단계는 계속 실행합니다. `env`가 실패하면 `gates`와 측정을, `gates`가 실패하면 측정을 건너뛰고 이유를 기록합니다. 보고서는 앞선 단계의 합격 여부와 관계없이 생성을 시도합니다. 필수 검증을 통과하지 못한 계산을 강제로 진행하지 않습니다.

사용자가 중단하면 다음 실험을 시작하지 않습니다. 단계만 실행할 때도
측정 단계의 기존 게이트 검사를 통과해야 합니다. 다시 누르면 입력 신원이 일치하는
완료 영수증을 재사용합니다. 전체 재실행은 env와 gates를 다시 확인하므로 게이트 비용은
다시 듭니다. 기존 단계 완료 표시만으로 환경 검사를 생략하지 않습니다.
현재 단계만 재개하려면 해당 단계를 선택해 단계별 버튼을 사용합니다.

```powershell
.\WorkstationTest.exe validate batch --root D:\WS-Work\w15
.\WorkstationTest.exe validate batch --stage matrix --root D:\WS-Work\w15
.\WorkstationTest.exe validate batch --stage baseline --root D:\WS-Work\w15
```

한 일괄 실행이 끝날 때까지 작업 폴더 잠금을 유지합니다. 중단 버튼은 현재 계산과
다음 단계 모두에 적용됩니다. `batch_status.json`은 최신 일괄 상태이며,
`runs/<실행>/batch_summary.json`과 각 단계의 별도 실행 폴더에 결과·로그를 남깁니다.
이 기능은 GUI와 로컬 CLI에서 사용합니다. LAN 에이전트의 기존 명령 허용 목록은 유지합니다.

## 결과 ZIP 전달

전체 또는 단계별 자동 실행과 보고서 생성이 끝나면 작업 폴더의 `exports`에 날짜가 붙은 ZIP을 자동 저장합니다. 실패하거나 사용자가 중단한 실행도 당시 기록을 저장합니다. 실패가 있으면 후속 단계가 완료되어도 전체 실행은 실패로 남습니다.

GUI의 **결과 ZIP 저장**으로 기존 결과를 다시 묶을 수 있습니다. 데이터 폴더나 엔진 설정 없이 사용할 수 있으며 기존 계산 상태를 바꾸지 않습니다. 실행 중에는 같은 작업 폴더의 잠금이 해제될 때까지 기다려 주세요.

```powershell
.\WorkstationTest.exe validate export --root D:\WS-Work\w15
```

ZIP에는 보고서·설정·환경·게이트·단계 상태, 계산 영수증, 비교 결과, 실행 로그와 그림을 포함합니다. 원본 SPD/Touchstone, 변환 NPZ, 캐시와 엔진 런타임은 제외합니다. `manifest.json`에 포함 파일과 누락·변경 기록, 실행 상태를 남깁니다. ZIP이 생성되었다는 사실이 실험 합격을 뜻하지는 않습니다. `export.json`에는 마지막 ZIP 경로를 기록합니다.

발견된 GPU 오차와 재실행 문제의 후속 과제는 [후속 검증 계획](docs/engine/FOLLOWUP_20260930.md)에 정리했습니다. 현재 GPU 수치 불일치는 미해결이며, 이 릴리스는 검증 기준을 완화하지 않습니다.

### 수동 CLI 실행

```powershell
.\WorkstationTest.exe validate prepare --data-dir D:\Design-Data --root D:\WS-Work\w15
.\WorkstationTest.exe validate env --root D:\WS-Work\w15
.\WorkstationTest.exe validate gates --root D:\WS-Work\w15
.\WorkstationTest.exe validate baseline --ports P9 --root D:\WS-Work\w15
.\WorkstationTest.exe validate matrix --axis B --ports P9 --root D:\WS-Work\w15
.\WorkstationTest.exe validate matrix --axis E --ports P20 --root D:\WS-Work\w15
.\WorkstationTest.exe validate converge --ports P92 --root D:\WS-Work\w15
.\WorkstationTest.exe validate compare D:\Laptop-Receipts D:\WS-Work\w15\receipts --root D:\WS-Work\w15
.\WorkstationTest.exe validate report --root D:\WS-Work\w15
.\WorkstationTest.exe validate --self-check
```

기존 수동 모드에서는 전체 측정 전에 `gates`가 기본/--gpu/--slow 세 엔진 테스트를 실행합니다. 게이트는 CPU
대형 계산을 포함해 수십 분~수 시간이 걸릴 수 있습니다. 데이터가 없어서 필수 테스트가
skip된 경우에는 측정을 허용하지 않습니다. 일반 자기검사는 설계 데이터가 필요 없습니다.

P9·P20·P92는 config.json의 `port_sets` 라벨입니다. 소유자의 사전 등록에 맞춰
P9는 기준 포트, P20은 가장 큰 포트를 포함한 부분집합, P92는 전체 포트를 지정합니다.
`"all"`은 해당 설계의 전체 SPD 포트를 열거합니다. E4에는 기저 비교를 할 포트 하나를 지정합니다.
스레드/프로세스/affinity는 실제 제한이며 RAM/VRAM 프로필은 **플래너 에뮬레이션**입니다.
실제 OS 메모리 상한으로 표현하지 않습니다. 기본 격자·엔진 파라미터·허용치를 조정하지 않습니다.

다른 설계는 config.json의 `designs`와 `port_sets` 양쪽에 추가합니다.
`--design design_a --ports P9`처럼 설정된 설계 id를 전달합니다. 새 설계의 참조와 노트북
영수증도 별도로 준비해야 하며 `family`는 E3 허용치를 선택하는 package 또는 pcb입니다.

```json
{
  "designs": {
    "design_a": {"spd": "design_a.spd", "reference": "design_a_Zdiag.npz", "family": "package"}
  },
  "port_sets": {
    "P9": {"design_a": ["PortA"]},
    "P20": {"design_a": ["PortA", "PortB"]},
    "P92": {"design_a": "all"},
    "E4": {"design_a": ["PortB"]}
  }
}
```

노트북 영수증을 연결하지 않은 baseline은 계산과 내부 비교가 성공하면 exit 0을 반환하며,
상태·실행 요약에 `comparison: "not_configured"`를 기록합니다. 기계 간 동일성을 PASS했다는
의미는 아닙니다. 실패한 비교 이름은 실행 로그와 `runs/<timestamp>/run_summary.json`에 남습니다.

## LAN 원격 제어

표준 라이브러리 HTTP/JSON만 사용합니다. 모든 요청에 파일 기반 Bearer 토큰이 필요합니다.
HTTP는 신뢰하는 사설 LAN에서 사용하며, 다른 네트워크에서는 VPN/SSH 터널을 사용합니다.
토큰은 root 밖에 만들고 노트북에 별도 전달합니다. 토큰을 저장소에 커밋하지 않습니다.
토큰은 최소 32자입니다. 에이전트는 연결 최대 32개, 연결 타임아웃 30초,
요청 본문 최대 64 KiB를 적용하며 포화 상태에는 503을 반환합니다.

워크스테이션에서:

```powershell
python -c "import secrets,pathlib; pathlib.Path(r'D:\WS-Token.txt').write_text(secrets.token_urlsafe(32),encoding='utf-8')"
.\WorkstationTest.exe agent --bind 0.0.0.0 --port 8765 --token-file D:\WS-Token.txt --root D:\WS-Work\w15
```

필요하면 관리자 PowerShell에서 **사설망 / 로컬 서브넷 한정** 방화벽 규칙을 추가합니다.
설치 프로그램은 방화벽 설정을 자동 변경하지 않습니다.

```powershell
New-NetFirewallRule -DisplayName "Workstation Test Program LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Private -RemoteAddress LocalSubnet
```

노트북에서:

```powershell
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt health --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt start env --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt watch --interval 30 --until finished --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt start gates --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt start baseline --arg ports=P9 --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt log --tail 80 --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt receipts --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt fetch summary.json --out D:\WS-Results --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt fetch gates.json --out D:\WS-Results --json
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt fetch matrix_plan.json --out D:\WS-Results --json
```

실행마다 watch로 완료를 확인한 뒤 다음 start를 보냅니다. 임의 shell 명령/API는 없습니다.
동시 start는 409, 인증 실패는 401, root 밖 파일 요청은 403입니다.
`GET /health /env /status /log /receipts /receipt/<name> /artifact/<relpath>`,
`POST /start /stop`만 제공합니다. status.json은 실행 상태의 기준입니다.
접속 로그는 `<root>/agent-logs/agent.log`에 남으며 1 MiB마다 회전하고 백업 3개를 보존합니다.
쿼리에는 키 이름만 기록하고 값은 기록하지 않습니다. `agent --self-check`는 실제 `env`
왕복과 충돌·중단을 검사하는 합성 워커를 각각 실행합니다. 엔진 설정이 없을 때의 env는
연결 진단이며 수치 검증 PASS를 의미하지 않습니다.

## 중단과 재개

```powershell
# 이미 시작한 포트들이 완료된 뒤 중단
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt stop --json
# 워커 트리를 즉시 종료; 미완료 포트는 다음 실행 때 다시 계산
.\WorkstationTest.exe ctl --url http://WS:8765 --token-file D:\WS-Token.txt stop --now --json
```

GUI에도 두 중단 버튼이 있습니다. 로컬 CLI에서는 `--stop-file PATH`와 Ctrl+C를 지원합니다.
로컬 CLI의 stop-file을 직접 만든 경우 재개 전에 그 파일을 제거합니다. GUI와 원격 start는
자신의 이전 중단 요청을 새 실행 전에 정리합니다.
graceful 요청은 각 포트를 시작하기 직전에 검사합니다. 이미 실행 중인 포트들은 모두
완료될 때까지 기다리므로 **진행 중 배치(jobs개 이하 포트)의 완료**가 중단 대기 시간입니다.
엔진에는 주파수 사이 중단 콜백이 없습니다.
동일 옵션의 start를 다시 실행하면 검증된 기존 영수증은 건너뛰고 남은 작업을 이어갑니다.
입력/수치 신원/환경이 달라지면 이전 완료 기록을 잘못 재사용하지 않습니다.
과거 연구 영수증에 수치 ID가 없으면 [검증된 영수증 매핑](tools/engine_studies/workstation/README.md#과거-노트북-영수증-연결)을 준비해야 합니다.

로그는 `runs/<timestamp>/log.txt`, 영수증은 `receipts/*.json`, 최신 상태는 `status.json`,
보고서는 `summary.json`, `W15_REPORT.md`, `figures/`입니다. 원본 기록은 보존합니다.
한 작업 폴더는 한 엔진/하드웨어 검증 캠페인에 사용합니다. 엔진·드라이버·하드웨어를
교체한 별도 실험은 새 작업 폴더를 선택하고 env와 gates부터 실행합니다.
영수증 여러 파일은 파일별 fetch 또는 공유 폴더 복사로 회수합니다.
엔진 CLI sweep 대신 포트별 공개 API 워커를 사용하는 이유는 포트마다 다른 자원 제한,
VRAM 표본, 중단·재개 신원을 동일한 형식으로 기록하기 위해서입니다.

## Touchstone 변환

외부 엔진 Python으로 변환기를 실행합니다. 제품의 read_touchstone/s_to_z와 PowerSI 라벨
변환기를 그대로 호출합니다. 기존 결과를 덮어쓰지 않습니다.

```powershell
python tools/engine_studies/workstation/touchstone_to_zdiag.py --help
python tools/engine_studies/workstation/touchstone_to_zdiag.py --self-check
python tools/engine_studies/workstation/touchstone_to_zdiag.py design.s92p design_Zdiag.npz --against original_Zdiag.npz
```

설치본의 변환기 소스는 설치 폴더 `_internal/tools/engine_studies/workstation/`에 있습니다.
알려진 PowerSI 접두어를 해석할 수 없는 라벨은 `--against` 또는 `--port-manifest`로
정확한 포트 이름을 확인할 수 있을 때만 변환합니다.

새 held-out 설계는 SPD의 **정확한 포트명과 Touchstone 열 순서**를 소유자가 확인한
JSON 배열(예: `["PortA::RailA", "PortB::RailB"]`)로 준비해 `--port-manifest ports.json`을
전달합니다. 기존 기준 NPZ가 없을 때 PowerSI의 bare rail 이름만 저장되면 워커가 SPD 포트와
대응시키지 못합니다. 변환 후 NPZ 포트명을 확인하고 `::` 앞의 실제 SPD 포트명
(위 예시에서는 `PortA`, `PortB`)을 `port_sets`에 지정합니다.
매니페스트는 임의의 열 재배열·추측을 허용하는 옵션이 아닙니다.

원본 Touchstone이 있으면 기존 NPZ와 rtol=1e-12 비교하고, 없으면 합성 2포트 자기검사만
통과한 **미검증 변환기**로 기록합니다. 새로운 설계도 참조 포트가 SPD와 일치해야 합니다.

## 개발과 Windows 패키징

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python app.py gui --smoke
python app.py agent --self-check
python -m pip install -r requirements-build.txt
./scripts/build_windows.ps1 -EngineRoot C:/Projects/Owner-Engine
./scripts/smoke_install.ps1
```

빌드 도구: [PyInstaller spec](https://pyinstaller.org/en/stable/spec-files.html),
[Inno Setup](https://jrsoftware.org/ishelp/topic_setup_architecturesallowed.htm) 6.3 이상(검증 빌드 7).
출력은 `dist/installer/Workstation-Test-Program-1.2.0-Setup-x64.exe`입니다.
빌드는 소유자가 제공하는 엔진 체크아웃과 고정 버전 라이브러리가 설치된 Python 3.12.10 환경에서 수행합니다. `scripts/bundle_runtime.py`가 지정된 배포 패키지와 의존성만 복사하고 사용자 site-packages/PYTHONPATH와 격리합니다. 엔진 소스·원본 데이터·빌드 런타임은 Git에 올리지 않습니다. 설치 파일에는 엔진과 필요한 라이브러리·라이선스를 포함합니다.

사전 등록: [W15_PLAN_20260921.md](docs/engine/W15_PLAN_20260921.md).
구현/검사 증거: [W15_REPORT.md](https://github.com/yunhyok/Workstation-Test-Program/blob/main/docs/engine/W15_REPORT.md).
1.0.1 수정 검증: [REVIEW_FIXES_20260921.md](docs/engine/REVIEW_FIXES_20260921.md).
서브에이전트가 실행부, 원격 제어, 보고서/변환기를 분담했으며 통합 검사는 주 에이전트가 수행합니다.
프로그램 자체 검사 요약은 `program_validation_summary.json`입니다. 작업 폴더의
측정 보고서 `summary.json`과 구분합니다.

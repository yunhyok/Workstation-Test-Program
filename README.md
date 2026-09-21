# Workstation Test Program

SPD PI 계산 엔진의 기계 간 수치 재현성과 CPU/GPU 자원 계획을 검증하는 Windows 프로그램.
GUI, 검증 CLI, Bearer 인증 LAN 에이전트, 노트북 제어 CLI를 제공합니다.

1.0.1 변경: baseline 종료 상태, 로컬 설계 설정, 에이전트 연결 제한, 버전 잠금과 운용 지적을 수정했습니다. 현재 PR의 설치 후보이며 병합은 소유자가 진행합니다.

## 설치하고 시작하기

1. [Releases](https://github.com/yunhyok/Workstation-Test-Program/releases)에서
   해당 버전의 `Workstation-Test-Program-<version>-Setup-x64.exe`를 내려받아 실행합니다.
2. 작업 폴더에 `config.example.json`을 `config.json`으로 복사하고 소유자가 제공하는 설계·포트 목록을 입력합니다. 시작 메뉴의 **Workstation Test Program**에서 **엔진 연결 설정**을 입력합니다.
3. **env → gates → baseline → matrix → report** 순서로 실행합니다.

인스톨러는 현재 사용자에게 설치되며 관리자 권한이 필요하지 않습니다.
앱 실행용 Python/Tk는 포함됩니다. **수치 계산용 Python 3.12.10, 외부 계산 엔진과 GPU
라이브러리, 설계 데이터는 별도로 준비**합니다. 설치/제거는 작업 영수증을 지우지 않습니다.
코드 서명 인증서는 포함되지 않으므로 배포 파일은 서명되지 않은 빌드입니다.

## 엔진 환경 준비

외부 엔진은 **소유자가 제공하는 엔진 체크아웃**을 사용합니다. 이 저장소는 엔진 소스를
복제하거나 수치 모듈을 변경하지 않습니다. 해당 체크아웃에서 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,gpu]"
# 소유자가 제공한 노트북의 전체 freeze 파일과 버전을 맞춥니다.
.\.venv\Scripts\python.exe -m pip install -r D:\Laptop-Receipts\laptop_pip_freeze_system_python.txt
```

측정의 필수 버전은 Python **3.12.10**, numpy **2.4.4**, scipy **1.18.0**입니다.
노트북 비교 기준: matplotlib 3.10.9, nvmath-python 1.0.0, nvidia-cudss-cu12 0.8.0.10,
cuda-bindings 12.9.8, cupy-cuda12x 14.2.0, shapely 2.1.2, pydantic 2.13.3.
freeze 파일 안의 로컬 editable 경로는 대상 워크스테이션의 엔진 경로로 설치합니다.
나머지 버전 차이는 env.json과 gates.json에 기록하며 GPU 스택 차이는 경고합니다.
세 필수 버전을 제외한 차이만으로 최초 측정을 막지 않습니다. 게이트 통과 이후 패키지·엔진이
변경되면 기존 게이트는 무효가 되므로 다시 실행해야 합니다. GPU 드라이버는 탐지값을 사용합니다.

작업 폴더의 `config.json`에 [config.example.json](config.example.json)을 복사하거나 GUI에서
같은 값을 입력합니다. GUI 기본 작업 폴더는 `%LOCALAPPDATA%\WorkstationTestProgram\w15`이며
`SPD_PI_WORK_DIR` 환경변수가 있으면 그 아래 `w15`를 사용합니다.
`engine_python`은 외부 Python 실행 파일, `engine_root`는 `tests/engine`이 있는 저장소입니다.
SPD는 data_dir에, 참조 NPZ는 data_dir 또는 그 아래 analysis에 둡니다.
JSON 경로는 `D:/WS-Work/w15`처럼 `/`를 권장합니다. 역슬래시를 쓰려면
`"D:\\WS-Work\\w15"`처럼 `\\`로 이스케이프합니다.

실제 설계 파일명과 포트명은 소유자의 **로컬 config.json에만** 입력합니다. 예시의
`design_a`, `PortA`는 형식을 보여주는 값으로 실제 데이터를 의미하지 않습니다.
기존 1.0.0 설정도 아래 `designs`와 `port_sets`를 추가해야 측정을 시작할 수 있습니다.
선택 항목 `laptop_receipts`와 `laptop_freeze`의 기본값은 `null`입니다. 비교할 노트북
자료가 있을 때만 실제 경로로 설정합니다. 지정한 영수증 폴더가 없으면 비교 실패로 기록합니다.

## CLI

소스 실행은 아래 명령의 `WorkstationTest.exe`를 `python app.py`로 바꾸면 됩니다.
설치 경로의 `WorkstationTest.exe`는 콘솔용, `WorkstationTestProgram.exe`는 GUI용입니다.

```powershell
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

전체 측정 전에 `gates`가 기본/--gpu/--slow 세 엔진 테스트를 실행합니다. 게이트는 CPU
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
./scripts/build_windows.ps1
./scripts/smoke_install.ps1
```

빌드 도구: [PyInstaller spec](https://pyinstaller.org/en/stable/spec-files.html),
[Inno Setup](https://jrsoftware.org/ishelp/topic_setup_architecturesallowed.htm) 6.3 이상(검증 빌드 7).
출력은 `dist/installer/Workstation-Test-Program-1.0.1-Setup-x64.exe`입니다.
CPU/GPU 수치 라이브러리는 설치 파일에 중복 포함하지 않고 외부의 고정 환경에서 실행합니다.

사전 등록: [W15_PLAN_20260921.md](docs/engine/W15_PLAN_20260921.md).
구현/검사 증거: [W15_REPORT.md](https://github.com/yunhyok/Workstation-Test-Program/blob/main/docs/engine/W15_REPORT.md).
1.0.1 수정 검증: [REVIEW_FIXES_20260921.md](docs/engine/REVIEW_FIXES_20260921.md).
서브에이전트가 실행부, 원격 제어, 보고서/변환기를 분담했으며 통합 검사는 주 에이전트가 수행합니다.
프로그램 자체 검사 요약은 `program_validation_summary.json`입니다. 작업 폴더의
측정 보고서 `summary.json`과 구분합니다.

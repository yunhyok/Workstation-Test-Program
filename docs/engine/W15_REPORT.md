# W15 Workstation Test Program 구현·배포 보고서

작성일: 2026-09-21. 프로그램 버전: 1.0.0.

## 1. 산출물과 검증 범위

Windows GUI, 검증 CLI, LAN 에이전트/클라이언트, Touchstone 변환기, 영수증 비교·보고서 생성기와
사용자별 설치 프로그램을 구현했다. 공개 저장소는
[Workstation-Test-Program](https://github.com/yunhyok/Workstation-Test-Program)이며 설치 파일은
[릴리스](https://github.com/yunhyok/Workstation-Test-Program/releases/tag/v1.0.0)로 배포한다.

사전 등록은 `W15_PLAN_20260921.md`이며 측정 전에 커밋 `440935f`로 고정했다.
엔진 기준 커밋은 `e5b8b716b115364b30b8201adc61dc3d6deb0e12`이다.
외부 엔진의 `src/`, `tests/engine`, 연구 트리, 동결 자산은 변경하지 않았다.

**프로그램 검증과 대상 워크스테이션의 수치·성능 검증은 서로 다른 완료 항목이다.**
이번 실행 호스트는 노트북이다. Threadripper/A6000 장비의 주소·접속 정보가 제공되지 않았으므로
그 장비의 전체 게이트와 A–F 결과를 측정값으로 기재하지 않는다.

## 2. 확인한 환경

| 항목 | 이 실행 호스트 | 대상 워크스테이션 |
|---|---|---|
| CPU | Intel Core i9, 물리 14 / 논리 20 | Threadripper, 물리 32 / 논리 64 — 미접속 |
| RAM | 약 64 GB | 512 GB — 미측정 |
| GPU | RTX A2000 Laptop GPU, 8192 MB | RTX A6000 — 실측 VRAM 미확인 |
| 드라이버 | 528.79 | 미확인 |
| Python | 3.12.10 | 미확인 |
| numpy / scipy | 2.4.4 / 1.18.0 | 미확인 |
| matplotlib / nvmath-python | 3.10.9 / 1.0.0 | 미확인 |
| cuDSS / cuda-bindings | 0.8.0.10 / 12.9.8 | 미확인 |
| cupy-cuda12x / shapely / pydantic | 14.2.0 / 2.1.2 / 2.13.3 | 미확인 |

외부 Python에서 실제 `HardwareProfile.detect()`와 공개 플래너를 호출했다.
노트북 CPU 플랜은 jobs 6 / threads 3, **합성** WORKSTATION CPU 플랜은 jobs 15 / threads 4,
합성 GPU 플랜은 jobs 10 / threads 6이었다. 합성 프로필은 워크스테이션 실측이 아니다.
전체 freeze 비교 결과와 탐지 원본은 로컬 작업 폴더 `output/local-validation/env.json`에 있다.

## 3. 프로그램 검증

| 검증 | 결과 | 범위 |
|---|---|---|
| 프로그램 pytest | **47 PASS** | 실행 제어·프로세스 트리·잠금·원격 인증·비교·보고서 |
| validation 자기검사 | PASS | 케이스 신원, 재개, 중단 파일, 비교, 배타 잠금 |
| agent 자기검사 | PASS | 실제 env 왕복, watch, 401/403/409, 중단 |
| 외부 엔진 데이터 독립 / 하드웨어 테스트 | 35 PASS, 44 deselected | `pytest tests/engine -q -k 'datafree or hardware' --gpu` |
| GUI | PASS | Tk 창 생성·배치·갱신 smoke; 스크린샷 기반 시각 검사는 미실시 |
| Windows 패키징·설치·제거 | **PASS** | 설치본 CLI/GUI/자기검사, 제거 후 연구 데이터 보존 |
| 설치본 외부 Python 연동 | **PASS** | 실제 엔진 env 및 외부 보고서 worker; 연구 결과는 INCOMPLETE로 정확히 표시 |

위의 35개 테스트는 아래의 **전체 엔진 게이트 3종을 대신하지 않는다.**
프로그램은 필수 재현 테스트가 skip되거나 엔진·버전·입력이 바뀐 경우 측정을 허용하지 않는다.
설치본은 PyInstaller 6.20.0과 Inno Setup 7.0.1-beta로 빌드했다. 파일 해시는 저장소 루트의
`summary.json`과 릴리스의 `SHA256SUMS.txt`에 기록했다. 실행부·원격 제어·보고서/변환기를
서브에이전트 3개가 분담했고 주 에이전트가 소스·통합 검사·최종 설치본을 검증했다.

Touchstone 원본 세 종류를 제품 파서와 S→Z 변환으로 처리해 기존 NPZ와
`rtol=1e-12, atol=0`로 비교했다. 주파수와 포트 순서도 검사했다.

| 설계 | 주파수 수 | 포트 수 | 기존 NPZ 재현 |
|---|---:|---:|---|
| 260729 | 826 | 92 | PASS |
| 260804 | 826 | 92 | PASS |
| s5m6585 | 626 | 160 | PASS |

합성 비대칭 2포트 자기검사도 PASS했다. PCB PowerSI 헤더는 원본 NPZ의 정확한 라벨과
대조한 경우에만 접미어를 대응했다. 입력 파일과 기존 NPZ는 덮어쓰지 않았다.

## 4. 전체 엔진 게이트와 A–F 결과

| 항목 | 판정 규칙 | 실행·결과 | 예측 대 결과 |
|---|---|---|---|
| 기본 / GPU / slow 게이트 | 세 전체 pytest suite와 필수 재현 사례 모두 통과 | NOT_RUN — 대상 장비 실행 대기 | 판단 보류 |
| A CPU | 패키지 ≤1e-9, PCB ≤2e-9; 같은 스레드 비트 동일 기대 | NOT_RUN | 비트 동일 예측; 미측정 |
| A GPU | 패키지 ≤1e-8, PCB ≥1 MHz ≤1e-7 및 전체 ≤1e-6; 반복 비교 | NOT_RUN | 계약 내 예측; 미측정 |
| B 스레드×jobs | 같은 스레드 비트 동일, 다른 스레드 ≤1e-9 | NOT_RUN | jobs 15까지 개선 예측; 미측정 |
| C affinity | 실제 8/16/32/64 제한과 detect 반영 확인 | 워크스테이션 NOT_RUN; 노트북 8-core probe에서 문제 확인 | 아래 엔진 요구사항 참조 |
| D RAM | 동시 프로세스 peak RSS 합 ≤ 플랜 예산 | NOT_RUN | 상수 검증 보류 |
| E GPU / VRAM / E4 | 동시 jobs E3, P18 CPU/GPU 기저 ≤1e-5 | NOT_RUN | jobs 4까지 개선 예측; 미측정 |
| F fine 격자 | RMS/max dB와 비용 기록만; 격자 변경 판정 없음 | NOT_RUN (선택) | E6 판단 보류 |

보고서는 자료가 없으면 NOT_RUN/INCOMPLETE로 표시한다. 없는 벽시계/RSS/VRAM 측정으로
그래프를 만들지 않았다. 실제 영수증이 쌓인 뒤 `validate report`가
`figures/w15_wall_jobs_threads.png`, `w15_rss_unknowns.png`, `w15_vram_jobs.png`를 생성한다.
저장소의 `summary.json`은 배포 검증 요약이며 워크스테이션 연구 영수증이 아니다.

## 5. 플래너 상수 제안값

실제 A6000 프로세스별 VRAM·미지수별 RSS 표본이 없으므로 새 상수를 제안하지 않는다.
보고서 생성기는 서로 다른 미지수 크기의 표본이 충분할 때 기울기·절편을 계산한다.
VRAM 표본은 해당 GPU 전체 사용량이므로 다른 프로세스 사용량이 포함된다. 프로세스별
추정에는 독립 실행 자료와 유휴 사용량을 함께 검토해야 한다. RAM/VRAM 가짜 프로필은
플래너 입력 에뮬레이션이며 OS 메모리 상한을 강제한 실험이 아니다.

## 6. 엔진 요구사항

1. **affinity를 반영하는 하드웨어 탐지:** 노트북 자식 프로세스에 논리 CPU 8개 제한을
   실제로 적용했지만 `HardwareProfile.detect()`는 논리 20개를 보고하고 jobs 6 / threads 3을
   유지했다. 제한된 마스크를 반영하는 공개 탐지 API가 필요하다. 래퍼는 실제 마스크와
   탐지 결과를 함께 기록하고 이 불일치를 보고한다. 엔진은 패치하지 않았다.
2. **포트 내부 중단 API:** 현재 공개 API에는 주파수 경계의 중단 콜백이 없다. 정상 중단은
   실행 중인 포트를 끝낸 뒤 멈춘다. 즉시 중단은 프로세스 트리를 종료하고 미완료 포트의
   완료 영수증을 만들지 않는다.

수치 불일치나 A6000 OOM은 아직 측정하지 않았으므로 엔진 결함으로 주장하지 않는다.

## 7. 운용과 남은 장비 검증

앱은 Python/Tk 런타임을 포함한다. 계산용 Python 3.12.10과 외부 엔진, 고정 GPU 의존성,
SPD/NPZ 및 노트북 영수증 번들은 워크스테이션에 별도로 준비한다. GUI에서 경로를 설정하고
`env → gates → baseline P9 → baseline P92 → matrix → report`를 실행한다.
fine 수렴성 검사는 선택 항목이다. LAN 제어와 토큰·방화벽 설정은 README에 있다.

기본 연구 폴더는 `%LOCALAPPDATA%\WorkstationTestProgram\w15` 또는
`%SPD_PI_WORK_DIR%\w15`이다. `runs/`, `receipts/`, `failures/`, `archives/`는 여기에 보존하며
공개 저장소에 실제 설계·영수증·로컬 설정·토큰을 업로드하지 않는다.
이번 로컬 검사 증거는 작업 저장소의 `output/local-validation`, `output/converter-check`,
`output/worker-api-check`, `output/install-smoke-*`에 있다.

대상 장비 검증 후에는 생성된 summary와 그림만 검토해 저장소에 반영하고,
엔진 파라미터·격자·허용치 변경은 별도 판단으로 남긴다.

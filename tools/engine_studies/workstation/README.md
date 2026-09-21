# 워크스테이션 도구

설치·외부 엔진 준비·LAN 방화벽·중단·재개·회수 방법은 저장소 루트의
[README](../../../README.md)에 있습니다.

- `ws_validate.py`: env/gates/baseline/matrix/converge/compare/report 및 --self-check
- `ws_agent.py`: 고정 HTTP 엔드포인트, Bearer 인증, 단일 작업 실행
- `ws_ctl.py`: 노트북 원격 제어, --json 자동화 출력
- `touchstone_to_zdiag.py`: 제품 Touchstone 파서 기반 참조 변환
- `engine_worker.py`: 외부 Python 환경에서 엔진 공개 API 호출
- `study_report.py`: 영수증 비교와 한국어 보고서/그림

소스 실행: `python tools/engine_studies/workstation/ws_validate.py --help`.
설치 실행: `WorkstationTest.exe validate --help`.
수치 엔진·원본 데이터·토큰은 이 디렉터리에 포함하지 않습니다.

## 과거 노트북 영수증 연결

`receipt_version` 또는 `numerics_id`가 없는 연구 영수증은 자동으로 신원을 추측하지 않습니다.
해당 디렉터리에 `legacy_receipt_map.json`을 두고 소유자가 확인한 수치 신원과 실행 조건을
명시합니다. 파일 내용의 SHA-256이 일치해야 비교가 허용됩니다.

```json
{
  "schema_version": 1,
  "verified": true,
  "receipts": {
    "port_a.json": {
      "sha256": "원본 JSON 파일의 실제 SHA-256 64자리",
      "numerics_id": "동일 옵션의 재현으로 확인한 실제 수치 ID",
      "design": "design_a",
      "family": "package",
      "port": "PortA",
      "backend": "splu",
      "threads": 4,
      "jobs": 1,
      "profile": "laptop"
    }
  }
}
```

예시의 ID·해시·스레드 수를 실제 기록으로 바꿉니다. `verified`는 검증의 대체 수단이 아니라
소유자의 검증 기록 표시입니다. 수치 ID를 추측하거나 비교 대상에서 복사해 넣으면 안 됩니다.
매핑이 없거나 해시가 다른 과거 영수증은 malformed로 보고하며 PASS하지 않습니다.
P9/P92 비교에는 해당 포트 집합의 번들을 따로 사용해 불필요한 미대응 사례를 섞지 않습니다.

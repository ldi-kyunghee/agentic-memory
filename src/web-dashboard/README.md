# web-dashboard — mem0×HaluMem 정성분석 웹앱

실험 산출물(러너/A′/trace/judge)을 유저 단위로 조인해 **flow 뷰 + column 뷰**로 탐색하고,
분석가들이 **코멘트**를 남겨 공유하는 내부 툴. Hamster에 호스팅, SSH 터널로 접속.

## 원칙

- **Additive & 가역**: 이 디렉토리 밖의 코드/데이터/환경을 절대 수정하지 않는다.
  `results/`·`traces/`·`dataset/`은 읽기 전용. 쓰기는 `src/web-dashboard/data/` (gitignore) 안에서만.
- 자체 uv 프로젝트 (루트 pyproject 무수정): `uv run --project src/web-dashboard ...`
- 인증 없음 (연구실 내부 + localhost 바인딩 + SSH 터널). 이름은 최초 접속 시 1회 입력 → localStorage.

## 실행 (Hamster)

```bash
uv run --project src/web-dashboard uvicorn app:app --app-dir src/web-dashboard --port 8501
```

분석가 접속 (각자 로컬에서):

```bash
ssh -J <ariel계정>@ariel.khu.ac.kr <hamster계정>@163.180.160.129 -N -L 8501:localhost:8501
# 브라우저에서 http://localhost:8501
```

## 데이터 모델

- **런 레지스트리** `runs.yaml`: version → 백본/프롬프트/유저수 메타 + merged jsonl/trace/judge 경로.
  네이밍 불규칙(초기 judge 디렉토리 `-nano`/`-mini` vs 이후 `-30b4`)은 여기서 명시 매핑으로 흡수.
- **유저 번들** (`/api/bundle`): 세션별로 골든 MP(+integrity/update 라벨), 추출 메모리(+accuracy 라벨
  +유래 op), 이벤트, probe 스냅샷, QA(+judge 라벨)를 조인한 단일 JSON. 조인 키는
  세션 인덱스(=judge session_id=trace session) + 텍스트 매칭.
- **필드 사전** `fielddict.yaml`: 난해한 키에 대한 설명 — UI 호버 툴팁의 출처.
- **코멘트** `data/comments.sqlite3`: (run, uuid, anchor, author, tag, body, ts).
  anchor 형식: `session:12` / `session:12/mp:3` / `session:12/qa:1` / `run` (유저 전역).

## 화면 (Phase)

1. **P1 (뷰어)**: 런×유저 선택 → 세션 flow (대화 → 추출 → 이벤트 → QA 체인, judge 라벨 배지)
   + 노드 클릭 시 column 뷰(json 탐색 + 필드 사전 툴팁) + trace 원문 패널
   + 턴 오버레이: 대화 턴 위에 골든/추출 근사 앵커 배지 (앵커는 사전 계산, "추정" 표기)
2. **P2 (코멘트)**: 노드 앵커 코멘트 + 유저별 digest 탭 + Markdown/JSON export
3. **P3 (비교)**: 동일 유저 다중 런 나란히 (지표 카드 + 세션/QA 대조)

## 구조

```
src/web-dashboard/
├── README.md          # 이 파일 (설계 계약)
├── pyproject.toml     # fastapi/uvicorn/pyyaml — 격리 환경
├── runs.yaml          # 런 레지스트리 (런 추가 시 여기만 수정)
├── fielddict.yaml     # 필드 사전
├── app.py             # FastAPI: 데이터 조인 + API + 정적 서빙
├── static/            # SPA (vanilla JS — 빌드 스텝 없음)
└── data/              # 런타임 산출물 (comments.sqlite3, anchors/) — gitignore
```

# 정성분석 온보딩 가이드 — mem0 × HaluMem 대시보드

> 처음 오신 분도 이 문서 하나로 접속 세팅부터 분석 시작까지 가능하도록 쓴 가이드입니다.
> 막히면 원진에게 문의. (대시보드 안의 거의 모든 용어·수치는 **마우스 호버**하면 설명이 떠서, 배경지식은 아래 최소한만 읽으면 됩니다.)

## 1. 접속 세팅 (최초 1회, ~5분)

대시보드는 연구실 서버(Hamster)에서 돌고, SSH 터널로 접속합니다. ariel/Hamster 계정이 있어야 합니다.

**① `~/.ssh/config`에 추가** (계정명은 본인 것으로):

```
Host *
  UseKeychain yes
  AddKeysToAgent yes

Host Ariel
  HostName ariel.khu.ac.kr
  User <본인 ariel 계정>
  Port 30080
  ServerAliveInterval 30
  ServerAliveCountMax 3

Host Hamster
  HostName 163.180.160.129
  User <본인 hamster 계정>
  ProxyJump Ariel
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

**② 비밀번호 입력 없애기** (자동 재연결의 전제):

```bash
ssh-copy-id Ariel                                  # ariel 비밀번호 마지막 1회 입력
ssh-add --apple-use-keychain ~/.ssh/id_ed25519    # 키 패스프레이즈 키체인 저장 (맥 기준)
ssh Hamster echo ok                                # 아무것도 안 묻고 ok 나오면 성공
```

**③ 터널 열기** (매 분석 세션마다 이 한 줄 — 슬립/끊김 자동 복구):

```bash
brew install autossh   # 최초 1회
autossh -f -M 0 -o "ExitOnForwardFailure yes" Hamster -N -L 8501:localhost:8501
```

**④ 브라우저에서 `http://localhost:8501` 접속** → 이름 입력 (코멘트 작성자 표시용, 최초 1회).

- 터널 종료: `pkill -f autossh` · 화면이 이상하면 우하단 **↻ 강제 재로딩**

## 2. 3분 배경지식 — 무엇을 평가한 실험인가

- **벤치마크 (HaluMem)**: 가상 유저의 세션별 대화마다 "시스템이 기억했어야 할 정답 목록"(**골든 메모리**)이 딸려 있고, 세션마다 QA 문항도 있습니다. 메모리 시스템을 ①추출(골든을 얼마나 담았나) ②갱신(바뀐 정보를 정확히 고쳤나) ③QA(기억으로 질문에 답했나) 3단계로 채점합니다. 골든 중 일부는 **미끼(interference)** — AI 발화에만 있고 유저가 확정 안 한 내용 — 라서 이걸 삼키면 감점(FMR)입니다.
- **시스템 (mem0)**: 세션 대화를 받으면 LLM이 ①핵심 정보(fact)를 추출하고 ②기존 메모리 중 비슷한 걸 검색한 뒤 ③fact마다 ADD(신규)/UPDATE(기존 메모리를 재작성)/DELETE를 결정합니다. **UPDATE 재작성본이 원 정보를 망가뜨리는 것(drift)** 이 핵심 관찰 대상 중 하나입니다.
- **실험 그리드**: memory agent 백본 4종(Qwen3-4B / Qwen3-30B / GPT-5-Nano / GPT-5-Mini) × 추출 프롬프트 2종(default / custom(문단형 지침)). 채점(judge)은 GPT-5-Nano 라벨이 신뢰 기준이며 **첫 4유저(★)만** 채점되어 있습니다. 답변 생성·임베딩은 전 실험 공통 고정.
- 정량 결론 요약: QA 1위는 mini×default. Qwen 계열은 재작성 품질이 낮고(=drift), custom 프롬프트는 모든 백본에서 QA 손해. **왜 그런지의 실물 확인이 정성분석의 목적**입니다.

## 3. 대시보드 구성

**상단바** (좌→우): `User`(★=judge 라벨 보유 4유저 — **★ 유저만 분석 대상**) · `Judge LLM`(nano 고정 권장) · `Embedding`(고정 표시) · `Agent A`+`A 추출 프롬프트`(기준 세팅) · `+ 비교(B)`(두 번째 세팅을 열어 나란히 비교 — 보라색).

**탭 5개**:

| 탭 | 용도 |
|---|---|
| **Sessions** | 메인 화면. 세션 선택(사이드바 빨간 원=실패 골든 수, 보라 원=오답 QA 수) → QA 목록(최상단) → 대화(턴 오른쪽에 골든🟡/A추출🟢/B추출🟣 칩) → 골든·추출A·추출B 나란히 대조 |
| **QA** | 전체 문항을 판정(C/H/O)별 필터로 훑기 — 실패 사례부터 여는 용도 |
| **Compare** | A/B 세팅의 유저 전체 요약 + 같은 질문의 판정 대조표 (행 클릭=양쪽 답변 펼침) |
| **Metrics** | 전체 실험 지표 테이블 (칼럼·수치 호버=정의·순위) — 큰 그림 파악용 |
| **Digest** | 이 유저에 남은 모든 분석가의 코멘트 모아보기 + Markdown export |

**우측 패널**: 상세(클릭한 항목의 원본 JSON — 키 호버 시 설명) · **Trace**(그 세션에서 mem0가 실제로 한 일: 추출 프롬프트/응답 전문, 검색 쿼리·hit, ADD/UPDATE 기록 — 클릭하면 펼쳐지고 중앙에 상응 지점 하이라이트) · 코멘트.

**배지 읽는 법**: 골든·추출 앞의 `2/1/0` = judge 점수(2=완전, 1=부분, 0=실패). QA의 `C/H/O` = Correct/Hallucination(날조)/Omission(누락). `–` = 이 judge 라벨 없음(★ 아닌 유저 등). ADD/UPDATE 칩 = 그 메모리를 만든 연산 — **UPDATE는 재작성본이라는 뜻**.

## 4. 코멘트 남기는 법 (분석 결과물 = 코멘트)

- 아무 항목(대화 턴, 골든/추출 메모리, QA)이나 **클릭** → 우측 코멘트 탭에서 작성. 텍스트를 **드래그**하면 💬 버튼으로 인용 코멘트도 가능
- 양식: **관찰 → 태그(강점/약점/병목/judge오판/추출누락/재작성drift/기타) → 시사점** 한 줄
- 코멘트는 실시간 공유됩니다 (남의 코멘트가 노란 하이라이트+이니셜로 표시, 클릭=스레드). 유저/세션 단위 종합 의견은 항목 클릭 없이 코멘트 탭에서 바로 작성
- 데이터는 가상 인물 대화지만 리포 밖 유출은 자제 (라이선스 CC-BY-NC-ND)

## 5. 무엇을 볼 것인가 — 추천 워크플로와 집중 질문

**분담**: 유저 단위 — ★ 4명(Martin Mark / Sarah Garcia / Donald Brown / Johnson Joseph)을 나눠 맡습니다.

**세션 하나당 워크플로**: ① 세션의 QA부터 — 오답(H/O)이 있으면 클릭해 4자 대조(질문→정답→답변→검색 context: 정답 재료가 context에 있었는데 틀렸나? 아예 없나?) → ② 대화를 스크롤하며 골든🟡 옆에 추출🟢이 따라붙는지 (골든만 있고 추출이 빈 턴 = 추출 누락 지점) → ③ 수상하면 우측 Trace로 "추출 LLM이 실제로 뭘 뱉었는지" 확인 → ④ 발견은 그 자리에서 코멘트.

**집중 질문 5가지** (정량분석이 남긴 미결 — 이걸 판별하는 게 이번 정성분석의 기여):

1. **재작성 drift의 실물** — `UPDATE` 칩이 붙고 accuracy 0점인 메모리를 열어(상세의 `previous_memory`와 대조) 재작성 과정에서 정보가 실제로 망가졌는지, 아니면 과거 세션 내용이라 채점만 불리했는지 (Qwen 4B/30B 세팅에서. 태그: 재작성drift)
2. **추출 누락의 유형** — 안 뽑힌 골든은 어떤 종류인가? (관계 정보? 시간·조건부? 대화 후반? assistant 발화에만 존재?) 특히 nano/mini의 "절제 추출"이 버리는 것의 패턴 (태그: 추출누락)
3. **custom 프롬프트의 오염** — Qwen×custom(B로 걸어 비교)에서 문단 메모리가 미끼(골든의 `interference` 태그)나 assistant 제안을 삼키는 장면 (태그: 약점)
4. **생성 실패** — 정답 재료가 검색 context에 다 있는데 오답인 QA: 4B generator가 초장문 context에서 근거를 못 찾는 사례 (Qwen×custom에서 다수 예상)
5. **judge 오판** — 라벨이 이상하면 그 자체가 발견 (사람이 보기에 맞는데 0점 등. 태그: judge오판) — 라벨은 필터일 뿐 정답이 아닙니다

**추천 비교 조합** (A vs B): `Qwen3-4B×default vs custom` (프롬프트 효과), `Qwen3-4B vs gpt-5-mini (default)` (백본 효과 — 왜 mini가 QA 1위인가), `gpt-5-nano×default vs custom` (절제 vs 과잉절제).

같은 유형이 3번 반복되면 그게 발견입니다. Digest 탭에서 수시로 서로의 코멘트를 확인하세요 — 미팅 자료는 Digest의 Markdown export로 만듭니다.

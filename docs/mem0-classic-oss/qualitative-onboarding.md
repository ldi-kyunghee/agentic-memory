# 정성분석 온보딩 가이드 - mem0 × HaluMem 대시보드

> 처음 오신 분도 이 문서 하나로 접속 세팅부터 분석 시작까지 가능하도록 쓴 가이드입니다.
> 막히면 원진에게 문의. (대시보드 안의 거의 모든 용어·수치는 **마우스 호버**하면 설명이 떠서, 배경지식은 아래 최소한만 읽으면 됩니다.)

## 1. 접속 세팅 (최초 1회, ~5분)

대시보드는 연구실 서버(Hamster)에서 돌고, SSH 터널로 접속합니다. Ariel/Hamster 계정이 있어야 합니다.

macOS 기준으로 먼저 설명하고, **Windows는 ①~③의 차이만 §1-W에** 정리했습니다.

**① `~/.ssh/config`에 추가** (계정명은 본인 것으로. 키가 없으면 먼저 `ssh-keygen -t ed25519`):

```
Host *  # ~/.ssh/config 파일의 최상단에 추가
  UseKeychain yes  # ⚠ macOS 전용 - Windows는 추가하지 말 것
  AddKeysToAgent yes  # ⚠ macOS 전용 - Windows는 추가하지 말 것

Host Ariel  # 파일 내 아무데나 적용
  HostName ariel.khu.ac.kr
  User <본인 ariel 계정>
  Port 30080
  ServerAliveInterval 30  # 시간 지나도 연결 끊기지 않고 유지하는 목적
  ServerAliveCountMax 3  # 시간 지나도 연결 끊기지 않고 유지하는 목적

Host Hamster  # 파일 내 아무데나 적용
  HostName 163.180.160.129
  User <본인 hamster 계정>
  ProxyJump Ariel
  ServerAliveInterval 30  # 시간 지나도 연결 끊기지 않고 유지하는 목적
  ServerAliveCountMax 3  # 시간 지나도 연결 끊기지 않고 유지하는 목적
```

**② 비밀번호 입력 없애기** (ssh 자동 재연결을 위해 필요):

```bash
ssh-copy-id Ariel                                  # ariel 비밀번호 마지막 1회 입력
ssh-add --apple-use-keychain ~/.ssh/id_ed25519    # 키 패스프레이즈 키체인 저장 (맥 기준)
ssh Hamster echo ok                                # 아무것도 안 묻고 ok 나오면 성공
```

**③ 터널 열기** (매 분석 세션마다 이 한 줄 - 슬립/끊김 자동 복구):

```bash
brew install autossh   # 최초 1회
autossh -f -M 0 -o "ExitOnForwardFailure yes" Hamster -N -L 8501:localhost:8501
```

- 터미널 세션에선 아무 반응 없이 hang 되고 있을 것입니다. (정상 동작임)
- 별도 에러 메세지 안 뜬다면 바로 ④로 넘어가면 됩니다. (터미널 창은 종료하지 말고 유지해야 함)

**④ 브라우저에서 `http://localhost:8501` 접속** → 이름(first name) 입력 (코멘트 작성자 표시용, 최초 1회).

- 터널 종료: `pkill -f autossh` · 화면이 이상하면 우하단 **↻ 강제 재로딩**

### 1-W. Windows 사용자 (PowerShell 기준 차이점만)

- config 위치는 `C:\Users\<이름>\.ssh\config`, 위 내용에서 **`UseKeychain`·`AddKeysToAgent` 두 줄은 삭제** (Windows OpenSSH가 모르는 옵션이라 에러 남)
- ② 대신:

```powershell
# 키 등록 (ssh-copy-id가 없어서 수동 - ariel 비밀번호 1회 입력)
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh Ariel "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

# 패스프레이즈 기억시키기 (ssh-agent 서비스 - 관리자 PowerShell에서 최초 1회)
Set-Service ssh-agent -StartupType Automatic; Start-Service ssh-agent
ssh-add $env:USERPROFILE\.ssh\id_ed25519
```

- ③ 대신 (autossh가 없어서 재연결 루프 - 이 창을 켜두면 끊겨도 3초 후 자동 재접속):

```powershell
while ($true) { ssh -o ExitOnForwardFailure=yes Hamster -N -L 8501:localhost:8501; Start-Sleep 3 }
```

- 대시보드 사용 종료 후 자동 재연결 루프를 종료하기 위해선 그 창에서 Ctrl+C 두 번 입력

## 2. 3분 배경지식 - 무엇을 평가한 실험인가

- **벤치마크 (HaluMem)**: 가상 유저의 세션별 대화마다 "시스템이 기억했어야 할 정답 목록"(**골든 메모리**)이 딸려 있고, 세션마다 QA 문항도 있습니다. 메모리 시스템을 ①추출(골든을 얼마나 담았나) ②갱신(바뀐 정보를 정확히 고쳤나) ③QA(기억으로 질문에 답했나) 3단계로 채점합니다. 골든 중 일부는 **미끼(interference)** - AI 발화에만 있고 유저가 확정 안 한 내용 - 라서 이걸 삼키면 감점(FMR 하락)입니다.
- **시스템 (mem0)**: 세션 대화를 받으면 Agent LLM이 ①핵심 정보(fact)를 추출(추출 프롬프트 변형 가능: default / custom)하고 ②기존 메모리 중 비슷한 걸 검색(Retrieval)한 뒤 ③fact마다 ADD(신규)/UPDATE(기존 메모리를 재작성)/DELETE를 결정합니다. **UPDATE 재작성본이 원 정보를 망가뜨리는 것(drift)** 이 핵심 관찰 대상 중 하나입니다.
- **실험 조합**: memory agent 백본 4종(Qwen3-4B / Qwen3-30B / GPT-5-Nano / GPT-5-Mini) × 추출 프롬프트 2종(default(Mem0 표준) / custom(문단형 지침, HaluMem 표준)). 채점(judge)은 GPT-5-Nano를 표준으로 하며 **첫 4유저(★표시된 유저: Martin Mark, Sarah Gracia, Donal Brown, Johnson Joseph)만** 채점되어 있습니다. 답변 생성·임베딩은 전 실험 공통 고정으로 Qwen3-Embedding-4B를 사용했습니다.
- 정량분석 요약: QA 1위는 GPT-5-Mini × Default 프롬프트. Qwen 계열은 재작성(UPDATE) 품질이 낮고(=update drift), Custom 프롬프트는 모든 백본에서 QA 성능을 저하시켰습니다. **원인 확인이 정성분석의 목적 중 하나**입니다.

## 3. 대시보드 구성

**상단바** (좌→우): `User`(★=GPT-5-Nano Judge로 평가한 4유저 - **★ 유저만 분석 대상**) · `Judge LLM`(GPT-5-Nano) · `Embedding`(Qwen3-Embedding-4B로 고정) · `Agent A`+`A 추출 프롬프트`(기준 세팅) · `+ 비교(B)`(두 번째 세팅을 열어 나란히 비교 - 보라색).

**탭 5개**:

| 탭 | 용도 |
|---|---|
| **Sessions** | 메인 화면. 세션 선택(좌측 사이드바 빨간 원=실패 골든 메모리 수, 보라 원=오답 QA 수) → QA 목록(최상단) → 대화(턴 오른쪽에 골든🟡/A추출🟢/B추출🟣 칩) → 골든·추출A·추출B 나란히 대조 |
| **QA** | 전체 문항을 판정(C/H/O)별 필터로 훑기 - 실패 사례부터 여는 용도 |
| **Compare** | A/B 세팅의 유저 전체 요약 + 같은 질문의 판정 대조표 (행 클릭=양쪽 답변 펼침) |
| **Metrics** | 전체 실험 지표 테이블 (칼럼·수치 호버=정의·순위) - 큰 그림 파악용 |
| **Digest** | 이 유저에 대해 작성된 모든 정성분석가의 코멘트 모아보기 + Markdown export 기능도 제공됨 |

**우측 패널**: 상세(클릭한 항목의 원본 JSON - 키 호버 시 설명) · **Trace**(그 세션에서 mem0가 실제로 한 일: 추출 프롬프트/응답 전문, 검색 쿼리·hit, ADD/UPDATE 기록 - 클릭하면 펼쳐지고 중앙 패널에 상응 지점 노란색 하이라이트 됨) · 정성분석가 코멘트.

**배지 읽는 법**: 골든·추출 메모리 앞의 `2/1/0` = judge 점수(2=완전, 1=부분, 0=실패). 골든 메모리에 붙은 점수는 추출 메모리들 전체에 해당 골든이 포함되었는지 여부에 대한 평가 결과(HaluMem 평가셋 중 Memory Integrity)이며, 추출 메모리에 붙은 점수는 추출된 메모리가 세션 대화 및 골든 메모리에 그라운딩 되는지 여부에 대한 평가 결과(HaluMem 평가셋 중 Memory Accuracy)임. QA의 `C/H/O` = Correct/Hallucination(날조)/Omission(누락). `–` = 이 judge 라벨 없음(★ 아닌 유저 등). ADD/UPDATE 칩 = 그 메모리를 만든 연산 - **UPDATE는 재작성본이라는 뜻**.

## 4. 코멘트 남기는 법 (분석 결과물 = 코멘트)

- 아무 항목(대화 턴, 골든/추출 메모리, QA)이나 **클릭** → 우측 코멘트 탭에서 작성. 텍스트를 **드래그**하면 💬 버튼으로 인용 코멘트도 가능
- 양식: **관찰 → 태그(강점/약점/병목/judge오판/추출누락/재작성drift/기타) → 시사점** 한 줄
- 코멘트는 실시간 공유됩니다 (남의 코멘트가 노란 하이라이트+이니셜로 표시, 클릭=스레드). 유저/세션 단위 종합 의견은 항목 클릭 없이 코멘트 탭에서 바로 작성

## 5. 평가 대상

**분담**: 분석 대상이 되는 유저는 일단 **Martin Mark**로 통일하겠습니다.

**세션 하나당 워크플로**: ① 세션의 QA부터 - 오답(H/O)이 있으면 클릭해 4자 대조(질문→정답→답변→검색 context: 정답 재료가 context에 있었는데 틀렸나? 아예 없나?) → ② 대화를 스크롤하며 골든🟡 옆에 추출🟢이 따라붙는지 (골든만 있고 추출이 빈 턴 = 추출 누락 지점) → ③ 수상하면 우측 Trace로 "추출 LLM이 실제로 뭘 뱉었는지" 확인 → ④ 발견은 그 자리에서 코멘트.

**포커스**:

1. **재작성(UPDATE) drift의 존재여부** - `UPDATE` 태그가 붙고 accuracy 0점인 메모리를 열어(우측탭 상세 정보의 `previous_memory`와 대조하기) 재작성(UPDATE) 과정에서 정보가 실제로 망가졌는지, 아니면 과거 세션 내용이라 채점만 불리했는지 검증해야 함 (HaluMem 평가 세팅의 특성 때문에 불필요한 페널티를 받고 있는 건 아닌지 확인할 필요가 있음. 채점 과정에선 해당 세션의 대화 내용만 Judge에게 제공되므로, Agent가 앞선 세션의 정보를 제대로 UPDATE 한 케이스인데도 Judge는 앞선 세션 정보를 모르므로 Hallucination이라고 평가했을 가능성이 있다고 보고 있음.)
2. **추출 누락의 유형** - 안 뽑힌 골든 메모리는 어떤 종류인지 (관계 정보? 시간·조건부? 대화 후반? assistant 발화에만 존재?) 모델/리즈닝/프롬프트 세팅별로 추출 양상의 차이가 어떠한지, 추출 단계에서의 병목은 무엇인지 식별이 필요함.
3. **프롬프트의 영향** - 동일 모델에서 프롬프트 변경에 따른 메모리 추출 양상 변화를 관찰 -> 달라진다면 후행하는 메모리 결정(ADD/UPDATE/DELETE)에도 큰 차이가 발생하는지도 중요한 관찰 지점임
4. **답변 생성 모델의 실패** - 정답을 맞추기 위한 재료가 검색 context에 다 있는데 오답이 생성된 QA 샘플. e.g. 4B generator가 장문 context에서 근거를 못 보는 사례 등
5. **judge 모델의 오판** - Judge가 판정을 제대로 못 내리는 경우(사람이 보기에 맞는데 0점 등)가 많을 것으로 예상됨 - GPT 모델의 경우 비용 이슈로 reasoning effort = minimal로 돌림
6. **벤치마크 품질** - 정답으로 제시된 golden memory의 품질 검수 (단, golden memory 중 interference인 경우는 해석을 신경써야 함. golden memory라고 표현은 하지만 실제로는 방해용으로 넣은 미끼 메모리이므로, 성능이 좋은 메모리 시스템이라면 해당 메모리는 저장하지 않았어야 함을 기억할 것). 세션 내 대화 자체의 품질 등도 문제가 있을 경우 코멘트 부탁함.

**우선 비교할 조합 (순서대로)** (A vs B): `Qwen3-4B vs GPT-5-Mini (default)` (백본 효과 - 왜 GPT-5-Mini가 QA 1위인가), `GPT-5-Mini × default vs custom` (프롬프트 효과),  `Qwen3-4B×default vs custom` (프롬프트 효과), `Qwen3-4B vs Qwen3-30B (default)` (백본 효과)
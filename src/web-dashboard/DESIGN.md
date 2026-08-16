---
version: alpha
name: HaluMem Analysis Console
description: mem0 × HaluMem 정성분석 대시보드의 시각 언어. 수치를 오래 대조하는 분석 도구용 — 장식보다 판독성과 밀도를 우선한다.
colors:
  bg: "#f2f3f5"
  panel: "#ffffff"
  chrome: "#f8f9fb"
  sunk: "#f4f5f7"
  line: "#e5e6eb"
  line-soft: "#eef0f3"
  line-firm: "#d3d5dd"
  ink: "#1b1b21"
  dim: "#6a6a76"
  faint: "#9a9aa6"
  chip: "#eceef2"
  accent: "#3f5ed3"
  ok: "#2f8641"
  warn: "#dd930f"
  bad: "#c53131"
  vio: "#7048a8"
  gold: "#c4900f"
  cmt: "#0e8398"
  bcol: "#495057"
typography:
  nano:
    fontFamily: var(--font-sans)
    fontSize: 9.5px
    fontWeight: 700
    lineHeight: 1.5
  micro:
    fontFamily: var(--font-sans)
    fontSize: 10px
    fontWeight: 700
    lineHeight: 1.5
  chip:
    fontFamily: var(--font-sans)
    fontSize: 10.5px
    fontWeight: 800
    lineHeight: 1.5
  label-xs:
    fontFamily: var(--font-sans)
    fontSize: 11px
    fontWeight: 600
    lineHeight: 1.45
  table-sm:
    fontFamily: var(--font-sans)
    fontSize: 11.5px
    fontWeight: 400
    lineHeight: 1.45
  table-md:
    fontFamily: var(--font-sans)
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.45
  body:
    fontFamily: var(--font-sans)
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.55
  verdict:
    fontFamily: var(--font-sans)
    fontSize: 15px
    fontWeight: 800
    lineHeight: 1.5
  mono:
    fontFamily: var(--font-mono)
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.45
rounded:
  xs: 4px
  sm: 5px
  md: 6px
  lg: 8px
  2xl: 12px
  pill: 999px
spacing:
  1: 2px
  2: 4px
  3: 6px
  4: 8px
  5: 10px
  6: 12px
  7: 16px
components:
  card:
    background: "{colors.panel}"
    border: "{colors.line}"
    rounded: "{rounded.2xl}"
  card-header:
    background: "{colors.chrome}"
    typography: "{typography.label-xs}"
  table-header:
    background: "{colors.chrome}"
    typography: "{typography.label-xs}"
  badge:
    rounded: "{rounded.pill}"
    typography: "{typography.label-xs}"
  button:
    border: "{colors.line-firm}"
    rounded: "{rounded.md}"
    typography: "{typography.table-sm}"
  tooltip:
    background: "#23232b"
    rounded: "{rounded.lg}"
    typography: "{typography.table-sm}"
---

## Overview

수치를 **오래 들여다보고 서로 대조하는** 연구용 콘솔이다. 한 화면에 표가 여러 개 겹치고, 분석가는 한 세션에 수백 개의 셀을 눈으로 훑는다. 그래서 이 시스템의 성패는 "예쁜가"가 아니라 **두 숫자가 다르다는 것을 얼마나 빨리 알아채는가**로 갈린다.

세 가지 원칙이 나머지를 다 결정한다.

**① 채도는 의미에만 쓴다.** 배경·테두리·보조 텍스트는 전부 무채색으로 눌러두고, 색이 있는 것은 판정 결과·설정 소속·골든·코멘트뿐이다. 이 규칙을 깨면 표에서 진짜 신호(빨간 0점 셀)가 장식에 묻힌다.

**② 밀도를 낮추지 않는다.** 여백을 넓히면 한 화면에 들어오는 행이 줄고, 그건 곧 비교 능력이 주는 것이다. 실제로 표 셀 패딩을 4px→6px로만 올렸다가 Metrics의 '직전 대비' 열이 잘렸다. 판독성은 여백이 아니라 **자간·행간·숫자 정렬**로 확보한다.

**③ 색의 의미 매핑은 고정이다.** 분석가들이 수백 시간에 걸쳐 익힌 규칙이고, 여기서 색을 재배치하면 과거 판정 기록과 화면이 어긋난다. 색상값(채도·명도)은 조정해도 **hue는 건드리지 않는다.**

## Colors

### 의미 매핑 (변경 금지)

| 계열 | 의미 | 토큰 |
|---|---|---|
| 초록 | 판정 2점 / Correct | `ok` `ok-bg` `ok-ink` |
| 주황 | 판정 1점 / 부분 | `warn` `warn-bg` `warn-bg2` `warn-ink` |
| 빨강 | 판정 0점 / Hallucination / 미끼 | `bad` `bad-bg` |
| 보라 | Other · 오라클 레인 | `vio` `vio-bg0` `vio-bg` `vio-bg2` `vio-ink` `vio-ink2` |
| 파랑 | 세팅 A · 링크 · 선택 | `accent` `accent-bg`~`accent-bg4` `accent-line` |
| 진회색 | 세팅 B | `bcol` `bcol-bg` `bcol-line` |
| 금 | 골든 메모리 | `gold` `gold-bg`~`gold-bg3` `gold-ink` `gold-ink2` |
| 청록 | 사람이 단 코멘트 | `cmt` `cmt-bg` `cmt-bg2` `cmt-ink` |

각 계열은 **본색 / 연한 배경 / 진한 배경 / 잉크** 네 역할로 나뉜다. 칩·배지 배경에는 `-bg`, 그 위 글자에는 `-ink`. **본색은 테두리·막대·아이콘 전용**이며 글자색으로 쓰면 대비가 모자란다.

### 무채색 층

`bg`(창 바닥) → `chrome`(사이드바·인스펙터·표 머리글) → `panel`(카드·표의 면) → `sunk`(코드·인용).

창 바닥을 패널보다 한 단계 낮춰야 카드가 면으로 떠서 3단 레이아웃이 한눈에 갈린다. 글자는 `ink`(본문) → `dim`(보조) → `faint`(주석·비활성) **세 단계뿐**이다. 네 번째 회색을 만들지 말 것.

경계선도 세 단계다: `line-soft`(표 세로 괘선처럼 있는 듯 없는 듯) → `line`(기본) → `line-firm`(입력·버튼).

### 국소 스케일 — Retriever 칼럼

Retriever는 판정 축과 무관한 별도 축이라 **이 칼럼 안에서만** 초록(BM25)/남색(임베딩)을 쓴다. 한쪽만 색을 주면 강조처럼 보이므로 둘 다 준다. 다른 칼럼에서 이 두 색을 retriever 뜻으로 재사용하지 않는다.

## Typography

폰트 스택의 **순서가 곧 설계**다.

```
-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo",
"Pretendard Variable", Pretendard, "Segoe UI", "Malgun Gothic", ...
```

맥은 `-apple-system`(SF Pro + Apple SD Gothic Neo)이 힌팅도 한글도 가장 또렷하므로 맨 앞에 둔다. 윈도우는 이 셋을 모르고 지나쳐 **Pretendard를 잡는다** — `Segoe UI` + 맑은고딕 조합은 라틴과 한글의 무게가 어긋나 `Upd C 74.44` 같은 혼합 문자열이 든 셀에서 줄이 튀기 때문에, 그 앞에 세운다. Pretendard는 CDN에서 받고 실패해도 시스템 폰트로 안전하게 떨어진다.

> ⚠ Pretendard를 스택 맨 앞에 두면 맥에서 오히려 읽기 나빠진다. 한 번 그렇게 했다가 되돌렸다.

크기는 **8단**이며 원본에서 튜닝된 값을 그대로 계승했다. 표 폭에 직결돼 있어 바꾸면 열이 잘린다.

`nano`(9.5) · `micro`(10) · `chip`(10.5) · `label-xs`(11) · `table-sm`(11.5) · `table-md`(12) · `body`(13) · `verdict`(15)

**숫자는 전부 `tabular-nums`다.** 이 화면 작업의 대부분이 세로로 늘어선 수치를 비교하는 일이라, 자릿수가 어긋나면 눈이 매번 다시 정렬해야 한다. 표·배지·막대 수치·κ 값에 일괄 적용한다. **수치를 표시하는 컴포넌트를 새로 만들면 이 목록에 추가한다.**

행간은 넷: `tight`(1.5, 칩·배지 — 행 높이가 튀지 않게 고정) · `snug`(1.45, 표) · `base`(1.55, 본문) · `loose`(1.7, 여러 줄 읽는 안내 박스).

## Layout

**레이아웃은 이 문서의 관할이 아니다.** 3단 그리드(사이드바 240 / 본문 / 인스펙터 420)와 드래그 리사이즈, 모달 구조는 분석가의 작업 흐름에 맞춰 굳은 것이라 시각 갱신에서 건드리지 않는다.

두 곳은 바꾸면 깨진다:

- `#topbar`의 padding — 높이가 바뀌면 `#layout`의 `calc(100vh - 46px)`가 어긋난다
- `table.cmp`에 `overflow: hidden` — 칼럼 리사이즈 핸들(`.colrz`, `right: -3px`)이 잘려 드래그가 죽는다

간격은 2·4·6·8·10·12·16px. 표 셀은 `4px 8px`, 카드 헤더·본문은 `8px 12px`가 기본이다.

## Elevation & Depth

그림자는 세 단계뿐이다.

- `sh-1` — 카드·상단바. 종이 한 장 뜬 정도
- `sh-2` — 떠 있는 컨트롤(선택 코멘트 버튼 등)
- `sh-3` — 모달·툴팁

**테두리와 그림자를 동시에 강하게 주지 않는다.** 카드는 `line` 한 겹 + `sh-1`, 모달은 테두리 없이 `sh-3`.

## Shapes

`xs`(4) 작은 칩 · `sm`(5) 태그 · `md`(6) 버튼·입력·탭 · `lg`(8) 큰 버튼·코드블록·툴팁 · `2xl`(12) 카드·모달 · `pill` 배지·진척막대.

## Components

- **표** — 세로 괘선은 정보를 나르지 않으므로 `line-soft`로 낮추고, 행을 가르는 가로선을 살려 눈이 가로로 미끄러지게 한다. 머리글은 `chrome` 바탕에 대문자·볼드 + 2px 밑줄. 행 호버 시 `accent-bg`를 55% 섞어 옅게 깐다.
  - ⚠ 머리글에 `position: sticky`를 쓰지 않는다. 한 스크롤 컨테이너(모달)에 표가 여러 개라 지나간 표의 머리글이 위에 떠서 다른 표 것처럼 보인다.
- **카드** — 헤더에 `chrome` 면을 줘 본문과 층을 나눈다. 상단 모서리만 둥글린다.
- **배지/칩** — `line-height`를 `tight`(1.5)로 고정한다. 안 그러면 배지가 든 행만 높이가 튀어 표가 울퉁불퉁해진다.
- **버튼** — 기본은 흰 바탕 + `line-firm` 테두리. 선택 상태만 채움(`accent` 또는 `ink`). 전이는 `.12s` — 연속 클릭하는 화면이라 길면 방해가 된다.
- **탭** — 활성 탭은 `ink` 채움 + 흰 글자. 지금 어느 화면인지 즉시 보이게.
- **툴팁** — 이 대시보드는 설명을 툴팁에 싣는다(지표 정의, 마스킹 이유, 판정 기준). 보조 요소가 아니라 **본문급**이다: 폭 360px, 행간 1.45, `sh-3`.
- **안내 박스** — `noisebar`(노랑, 신뢰 한계) / `jbasis`(파랑, 정의·기준) / `oracle-note`(보라, 오라클 레인)를 왼쪽 3px 색띠로 구분한다.
- **포커스** — `:focus-visible`에만 링을 준다. 마우스 클릭에는 뜨지 않고 Tab 이동에만 뜬다.

## Do's and Don'ts

**Do**
- 새 색이 필요하면 먼저 기존 8계열 중 의미가 맞는 것을 찾는다
- 수치를 표시하는 컴포넌트에는 `tabular-nums` 목록에 셀렉터를 추가한다
- 값은 `:root` 토큰에서만 바꾼다
- 표를 건드린 뒤에는 **반드시 Metrics 탭을 열어** 열이 잘리지 않았는지 본다

**Don't**
- 색의 **의미 매핑**을 바꾸지 않는다 (초록=좋음, 빨강=나쁨, 금=골든, 청록=코멘트, 파랑=A, 진회색=B)
- 네 번째 회색을 만들지 않는다 — `ink`/`dim`/`faint`로 충분하다
- 표 셀 패딩·글자 크기를 키우지 않는다 — 한 화면에 들어오는 행 수가 곧 비교 능력이다
- `.btn-off`를 "비활성 스타일"로 쓰지 않는다 — `visibility: hidden`이라 요소가 통째로 사라진다
- Pretendard를 폰트 스택 맨 앞에 두지 않는다 — 맥에서 가독성이 떨어진다

---
version: alpha
name: HaluMem Analysis Console
description: mem0 × HaluMem 정성분석 대시보드의 시각 언어 "계기판(Instrument)". 문서 대신 계측 장비처럼: 평면·각·어두운 크롬·데이터 시트.
colors:
  chrome-bg: "#1b1c21"
  chrome-ink: "#e9e9ee"
  chrome-dim: "#8b8c97"
  bg: "#eeeff2"
  panel: "#ffffff"
  band: "#f4f5f8"
  sunk: "#f2f3f6"
  rule: "#dcdee5"
  rule-soft: "#e9eaef"
  rule-firm: "#c6c9d3"
  ink: "#16171c"
  dim: "#63646f"
  faint: "#94959f"
  chip: "#e8eaef"
  accent: "#2f4fd0"
  ok: "#22803a"
  warn: "#d68a06"
  bad: "#c02a2a"
  vio: "#6a3fa5"
  gold: "#bd8a08"
  cmt: "#0b7d92"
  bcol: "#41474e"
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
    lineHeight: 1.4
  table-sm:
    fontFamily: var(--font-sans)
    fontSize: 11.5px
    fontWeight: 400
    lineHeight: 1.4
  table-md:
    fontFamily: var(--font-sans)
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.4
  body:
    fontFamily: var(--font-sans)
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  verdict:
    fontFamily: var(--font-sans)
    fontSize: 15px
    fontWeight: 800
    lineHeight: 1.5
  mono:
    fontFamily: var(--font-mono)
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.4
rounded:
  0: 0
  1: 2px
  2: 3px
  3: 4px
spacing:
  1: 2px
  2: 4px
  3: 6px
  4: 8px
  5: 10px
  6: 12px
  7: 16px
components:
  topbar:
    background: "{colors.chrome-bg}"
    color: "{colors.chrome-ink}"
  card:
    background: "{colors.panel}"
    border: "{colors.rule}"
    rounded: "{rounded.2}"
  card-header:
    background: "{colors.band}"
    typography: "{typography.micro}"
  table-cell:
    border: "{colors.rule}"
    typography: "{typography.table-md}"
  table-header:
    background: "{colors.band}"
    typography: "{typography.micro}"
  badge:
    rounded: "{rounded.1}"
    typography: "{typography.label-xs}"
  button:
    border: "{colors.rule-firm}"
    rounded: "{rounded.1}"
    typography: "{typography.table-sm}"
  tooltip:
    background: "{colors.chrome-bg}"
    rounded: "{rounded.2}"
    typography: "{typography.table-sm}"
---

## Overview: 계기판(Instrument)

이 화면은 **읽어내는 화면**이다 (읽는 화면과 다르다). 한 세션에 수백 개의 셀을 눈으로 훑고, 두 숫자가 다른지를 판단한다. 그래서 문서 대신 **계측 장비**처럼 만든다.

다섯 가지가 나머지를 다 결정한다.

**① 평면.** 그림자를 쓰지 않는다. 깊이는 1px 헤어라인과 배경 단차로만 만든다. 떠 있어야 하는 것(모달·툴팁)만 예외다. 그림자는 면을 흐리게 만들고, 면이 흐려지면 격자가 흐려진다.

**② 각.** 모서리는 0~4px. 알약(pill) 배지를 사각 태그로 바꿨다. 둥근 모서리는 "문서·앱"의 신호이고 각진 모서리는 "계기·표"의 신호다. **형태 하나가 색 열 개보다 인상을 크게 바꾼다**: 색만 만지던 이전 시도들이 체감되지 않았던 이유다.

**③ 어두운 크롬.** 상단바와 모달 헤더는 근검정(`chrome-bg`)이다. **도구 영역과 데이터 영역을 색으로 가른다**: 설정을 만지는 곳과 결과를 읽는 곳이 눈으로 즉시 구분된다. 밀도 비용이 0이라는 것도 중요하다.

**④ 데이터 시트.** 표는 전면 헤어라인 격자다. 세로 괘선을 지우면 문서의 표처럼 보이는데, 우리는 열을 따라 눈이 내려가야 하므로 격자가 있는 편이 맞다.

**⑤ 위계는 크기 대신 굵기·대문자·자간으로.** 라벨은 10px 대문자에 자간 `.07em`, 값은 굵게. **글자를 키우지 않으므로 밀도가 줄지 않는다.**

### 어기면 안 되는 두 가지

**밀도.** 여백을 넓히면 한 화면의 행 수가 줄고 그건 곧 비교 능력이 주는 것이다. 실제로 표 셀 패딩을 4px→6px로만 올렸다가 Metrics의 '직전 대비' 열이 잘렸다. **표를 건드린 뒤에는 반드시 Metrics 탭을 열어 확인한다.**

**색의 의미.** 분석가들이 수백 시간에 걸쳐 익힌 규칙이라, 재배치하면 과거 판정 기록과 화면이 어긋난다. 색상값(채도·명도)은 조정해도 **hue는 건드리지 않는다.**

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

도구 영역은 `chrome-bg`(근검정) 하나, 데이터 영역은 네 층이다:

`bg`(창 바닥) → `band`(머리글 띠·사이드바·인스펙터) → `panel`(카드·표의 면) → `sunk`(코드·인용).

글자는 `ink`(본문) → `dim`(보조) → `faint`(주석·비활성) **세 단계뿐**이다. 네 번째 회색을 만들지 말 것. 어두운 크롬 위에서는 `chrome-ink` / `chrome-dim` 두 단계를 쓴다.

경계선도 세 단계다: `rule-soft`(목록 구분선) → `rule`(격자선, 기본) → `rule-firm`(입력·구조선).

### 국소 스케일: Retriever 칼럼

Retriever는 판정 계열과 무관한 별도 항목이라 **이 칼럼 안에서만** 초록(BM25)/남색(임베딩)을 쓴다. 한쪽만 색을 주면 강조처럼 보이므로 둘 다 준다. 다른 칼럼에서 이 두 색을 retriever 뜻으로 재사용하지 않는다.

## Typography

**폰트는 원본 그대로다.**

```
-apple-system, "Segoe UI", "Noto Sans KR", sans-serif
```

웹폰트를 얹지 않고 `-webkit-font-smoothing`도 건드리지 않는다. 두 번 다 시도했다가 되돌렸다:

- Pretendard를 스택 맨 앞에 두면 **맥에서 오히려 읽기 나빠진다** (SF Pro + Apple SD Gothic Neo가 힌팅·한글 모두 더 또렷하다)
- `-webkit-font-smoothing: antialiased`는 맥에서 글자를 **얇게** 만들어 작은 글씨 가독성을 떨어뜨린다

크기는 **8단**이며 원본에서 튜닝된 값을 그대로 계승했다. 표 폭에 직결돼 있어 바꾸면 열이 잘린다.

`nano`(9.5) · `micro`(10, 라벨·범례) · `chip`(10.5) · `xs`(11) · `sm`(11.5) · `md`(12, 표 본문) · `base`(13, 본문) · `lg`(15, 판정 라벨)

행간은 본문 **1.5**(원본 값: 올리면 화면당 행 수가 준다), 표 **1.4**, 칩·배지 **1.5 고정**(안 그러면 배지가 든 행만 높이가 튄다), 안내 박스 **1.65**.

**숫자는 전부 `tabular-nums`다.** 이 화면 작업의 대부분이 세로로 늘어선 수치를 비교하는 일이라, 자릿수가 어긋나면 눈이 매번 다시 정렬해야 한다. **수치를 표시하는 컴포넌트를 새로 만들면 style.css 상단의 셀렉터 목록에 추가한다.**

## Layout

**레이아웃은 이 문서의 관할이 아니다.** 3단 그리드(사이드바 240 / 본문 / 인스펙터 420)와 드래그 리사이즈, 모달 구조는 분석가의 작업 흐름에 맞춰 굳은 것이라 시각 갱신에서 건드리지 않는다.

두 곳은 바꾸면 깨진다:

- `#topbar`의 padding: 높이가 바뀌면 `#layout`의 `calc(100vh - 46px)`가 어긋난다
- `table.cmp`에 `overflow: hidden`: 칼럼 리사이즈 핸들(`.colrz`, `right: -3px`)이 잘려 드래그가 죽는다

간격은 2·4·6·8·10·12·16px. 표 셀은 `4px 8px`, 카드 헤더·본문은 `8px 12px`가 기본이다.

## Elevation & Depth

**그림자를 쓰지 않는다.** 층은 배경 단차와 1px 선으로만 만든다:

`bg`(창 바닥) → `band`(머리글 띠·사이드바) → `panel`(카드·표의 면) → `sunk`(코드·인용)

경계선도 세 단계다: `rule-soft`(목록 구분선) → `rule`(격자선, 기본) → `rule-firm`(입력·구조선).

예외는 **떠 있어야 하는 것** 둘뿐이다. 모달(`0 18px 60px`)과 툴팁(`0 8px 28px`). 이들은 아래 내용과 겹치므로 분리 신호가 필요하다.

## Shapes

`0` 막대·구분 · `2px` 칩·배지·버튼 · `3px` 카드·코드블록 · `4px` 모달.

**알약(999px)을 쓰지 않는다.** 둥근 알약은 앱의 신호이고, 이 화면은 계기다.

## Components

- **상단바 / 모달 헤더**: 근검정 크롬. 셀렉트·버튼도 크롬 톤으로 맞춘다(어두운 배경에 밝은 글자). 세팅 A 라벨은 `#8fa6ff`처럼 어두운 배경에서 읽히는 밝은 파랑을 쓴다.
  - ⚠ `#topbar`의 padding은 고정이다. 높이가 바뀌면 `#layout`의 `calc(100vh - 46px)`가 어긋난다.
- **표**: 전면 헤어라인 격자(`rule`). 머리글은 `band` 바탕에 10px 대문자·자간 `.07em`·굵게 + 2px 밑줄. 행 호버는 `accent-bg`.
  - ⚠ 머리글에 `position: sticky`를 쓰지 않는다. 한 스크롤 컨테이너(모달)에 표가 여러 개라 지나간 표의 머리글이 위에 떠서 다른 표 것처럼 보인다.
  - ⚠ `table.cmp`에 `overflow: hidden`을 주지 않는다. 칼럼 리사이즈 핸들(`.colrz`, `right: -3px`)이 잘려 드래그가 죽는다.
- **카드**: 1px 테두리 + `band` 머리글 띠. 그림자 없음. 머리글은 대문자 10px.
- **배지/칩**: 사각 태그(2px). `line-height`를 1.5로 고정한다.
- **버튼**: 흰 바탕 + `rule-firm` 테두리, 모서리 2px. 선택 상태만 채움(`accent` 또는 `ink`). 전이 `.1s`.
- **탭**: 어두운 크롬 위에서 활성 탭만 흰 바탕으로 뚫린다. 인스펙터 탭은 밝은 배경이라 반대로 `ink` 채움.
- **툴팁**: 지표 정의·마스킹 이유·판정 기준을 전부 툴팁에 싣는다. 보조 요소가 아닌 **본문급**이다: 폭 350px, 크롬 톤.
- **안내 박스**: `noisebar`(노랑, 신뢰 한계) / `jbasis`(파랑, 정의·기준) / `oracle-note`(보라, 오라클 레인). 모서리 0, 왼쪽 3px 색띠로 구분.
- **포커스**: `:focus-visible`에만 링. Tab 이동에만 뜬다.

## Do's and Don'ts

**Do**
- 새 색이 필요하면 먼저 기존 8계열 중 의미가 맞는 것을 찾는다
- 수치를 표시하는 컴포넌트를 만들면 `tabular-nums` 셀렉터 목록에 추가한다
- 값은 `:root` 토큰에서만 바꾼다
- 표를 건드린 뒤에는 **반드시 Metrics 탭을 열어** 열이 잘리지 않았는지 확인한다

**Don't**
- 색의 **의미 매핑**을 바꾸지 않는다 (초록=좋음, 빨강=나쁨, 금=골든, 청록=코멘트, 파랑=A, 진회색=B)
- 네 번째 회색을 만들지 않는다. `ink`/`dim`/`faint`로 충분하다
- 그림자를 쓰지 않는다. 모달·툴팁만 예외
- 알약(999px) 모서리를 쓰지 않는다
- 표 셀 패딩·글자 크기를 키우지 않는다. 한 화면의 행 수가 곧 비교 능력이다
- 웹폰트를 얹거나 `-webkit-font-smoothing`을 건드리지 않는다. 맥에서 가독성이 떨어진다
- `.btn-off`를 "비활성 스타일"로 쓰지 않는다. `visibility: hidden`이라 요소가 통째로 사라진다

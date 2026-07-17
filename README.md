# Agentic Memory Management

### 참고 사항

1. 레포 clone 시 `--recurse-submodules` 플래그를 붙어 실행하세요

```git
git clone --recurse-submodules https://github.com/ldi-kyunghee/agentic-memory.git
```

2. HaluMem 레포는 전체 데이터셋이 포함되지 않아 허깅페이스 레포를 다운받아 `dataset/`에 놓았습니다

## 실행 방법

1. 환경 설치
```shell
uv sync
```

2. 스크립트 실행
```shell
# 일반
bash scripts/bm25_eval/run.sh [GPU_NUM]

# 세라프에서 실행 시
bash scripts/bm25_eval/run_slurm.sh
```

QA 생성 및 평가 결과는 각각 `results/bm25/exp[num]/`, `scores/bm25/exp[num]/`에서 확인 가능합니다.
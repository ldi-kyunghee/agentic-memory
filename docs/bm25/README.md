# Naive Memory System
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
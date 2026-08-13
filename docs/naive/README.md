# Naive Memory System
## 디렉토리 구조
```
eval/bm25 # 평가 코드
docs/bm25 # documentation 
results/bm25 # 평가 생성 결과
scores/bm25 # 평가 결과 점수
configs/bm25_eval # 생성 모델용 configuration file (즉, model_kwargs)
scripts/bm25_eval # 평가 코드 실행 스크립트
```
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
# LiteLLM · Langfuse EKS 실습 구성

배포는 스크립트 없이 Helm 릴리스 두 개와 kubectl로 적용하는 YAML만으로 합니다.
단계별 설명, IAM·Secrets Manager 준비, 문제 해결은 [docs/EKS(LITELLM+LANGFUSE).md](<docs/EKS(LITELLM+LANGFUSE).md>)에 있습니다.

## 파일 구성

```
manifests/
├── 01-litellm-prereqs/        kubectl apply -k: LiteLLM Helm hook보다 먼저 있어야 하는 리소스
│   ├── kustomization.yaml     아래 파일을 묶고 ConfigMap 두 개를 파일에서 생성
│   ├── secretstore.yaml       SecretStore aws-secretsmanager
│   ├── backend-secrets.yaml   ExternalSecret litellm-backend-secrets (DB 관리자, Redis)
│   ├── runtime-secrets.yaml   ExternalSecret litellm-runtime-secrets (DATABASE_URL, REDIS_URL, 앱 키)
│   ├── certs/                 ConfigMap litellm-lab-ca 원본 (RDS CA, 공개 CA)
│   └── py/                    ConfigMap litellm-callbacks 원본 (커스텀 콜백·가드레일)
├── 02-gp3-pvc.yaml            (선택) EBS CSI 단독 확인용 PVC + Pod
├── 03-langfuse-values.yaml    Langfuse Helm values (ServiceAccount, StorageClass, ExternalSecret 포함)
└── 04-litellm-values.yaml     LiteLLM Helm values (config, DB 초기화 hook, smoke 테스트 포함)
iam/                           IAM 권한·신뢰 정책 JSON
docs/                          구축 가이드
```

## 배포 순서 요약

```bash
cd ~/projects/litellm-lab/aws-eks-litellm/litellm-eks-minimal
CTX=arn:aws:eks:us-east-1:762491855071:cluster/aws-ue1-litellm-eks
CHART=.vendor/litellm/helm/litellm-helm

# 1. LiteLLM 사전 준비물 (SecretStore, ExternalSecret 2개, ConfigMap 2개)
kubectl --context "$CTX" apply --server-side -k manifests/01-litellm-prereqs
kubectl --context "$CTX" wait --for=condition=Ready -n litellm \
  externalsecret/litellm-backend-secrets externalsecret/litellm-runtime-secrets --timeout=120s

# 2. LiteLLM 차트 소스 (v1.103.3 태그, 커밋 ecae261b100cdf6bcb1d024ae69e4efa4a891be0 고정)
git clone --depth 1 --filter=blob:none --sparse --branch v1.103.3 \
  https://github.com/BerriAI/litellm.git .vendor/litellm   # 최초 1회
git -C .vendor/litellm sparse-checkout set helm/litellm-helm
git -C .vendor/litellm rev-parse HEAD

# 3. LiteLLM 배포 (DB 초기화 → migration hook → 앱)
helm dependency build "$CHART"
helm upgrade --install litellm "$CHART" -n litellm --kube-context "$CTX" \
  -f manifests/04-litellm-values.yaml \
  --set-string podAnnotations.checksum/callbacks="$(cat manifests/01-litellm-prereqs/py/*.py | sha256sum | cut -c1-16)" \
  --wait --timeout=10m

# 4. 동작 확인 (Bedrock 호출 1회 과금)
helm test litellm -n litellm --kube-context "$CTX" --filter name=litellm-smoke-test --logs

# 5. Langfuse 배포
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm upgrade --install langfuse langfuse/langfuse --version 2.1.7 -n langfuse --kube-context "$CTX" \
  -f manifests/03-langfuse-values.yaml --wait --timeout=20m
```

- `--set-string podAnnotations.checksum/callbacks=…`는 `py/` 파일이 바뀌었을 때 LiteLLM 파드를 다시 띄우기 위한 값입니다.
- Secrets Manager 값만 바꿨을 때는 ExternalSecret에 `force-sync` 어노테이션을 붙여 동기화한 뒤 `kubectl rollout restart deployment/litellm -n litellm`을 실행합니다.
- 예전에 kubectl로 만든 리소스가 남아 있는 클러스터에서는 첫 Helm 배포 전에 가이드 부록 B의 소유권 이전이 필요합니다.

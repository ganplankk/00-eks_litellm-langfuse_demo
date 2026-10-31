# EKS LiteLLM + Langfuse 구축가이드
> Git Repogitory 주소 : https://github.com/ganplankk/00-eks_litellm-langfuse_demo 
> 
> 본 가이드는 EKS 환경에서 LiteLLM과 Langfuse를 연동하여 구축 및 테스트를 진행하기 위한 목적으로 작성되었습니다.
> * *네트워크 설정 및 Bedrock 설정 등 사전 구성 항목은 본 가이드 범위에서 제외되었습니다.*
> 

## 구성도

!images/architecture.png

- **`LiteLLM`**: LLM API를 표준화된 OpenAI 호환 형식으로 단일화하여 호출하고 관리하는 프록시 게이트웨이
- **`Langfuse Web`**: LLM 애플리케이션의 모니터링, 트레이싱(Tracing), 프롬프트 관리 및 비용 분석 등을 확인할 수 있는 웹 대시보드 인터페이스
- **`Langfuse Worker`**: Langfuse로 수집되는 대규모 트레이싱 및 로그 데이터를 백그라운드에서 비동기적으로 처리하고 DB에 적재하는 백엔드 프로세스
- **`ClickHouse`**: Langfuse가 수집하는 방대한 양의 LLM 트레이싱 및 로그 데이터를 고속으로 저장하고 조회하기 위한 컬럼 기반 분산 데이터베이스
- **`ClickKeeper`**: ClickHouse 클러스터의 상태 모니터링, 백업 또는 관리 보조 역할을 수행하는 유틸리티/에이전트
- **`ESO (External Secrets Operator)`**: 외부 비밀 관리 소스(예: AWS Secrets Manager)의 인증 정보나 API 키를 쿠버네티스 시크릿(Secret)으로 안전하게 동기화해주는 오퍼레이터

---

## 1. 준비

### 1.1 로컬 도구

```bash
aws --version          # AWS CLI v2
kubectl version --client
helm version --short   # v3.13 이상 또는 v4
git --version
```

### 1.2 사전준비 구축 리소스

VPC, RDS(PostgreSQL), ElastiCache(Redis, TLS + AUTH)는 이미 만들어져 있고, EKS 노드에서 RDS(5432)·Redis(6379)로 접속할 수 있다고 가정합니다. 이 리소스들을 만드는 방법은 다루지 않습니다. 3단계에서 아래 값만 가져다 씁니다.

리소스 이름 (`aws-ue1-litellm-eks`, `aws-ue1-litellm-app-role` 등)은 Git Repo 파일과 맞춰져 있으므로 이름 그대로 사용합니다.

| 3단계에서 채울 값 | 어디서 확인 |
| --- | --- |
| `VPC_ID`, `PUBLIC_SUBNETS` | VPC ID, ALB를 둘 퍼블릭 서브넷 2개의 이름(Name 태그) |
| `RDS_ENDPOINT`, `RDS_MASTER_USER`, `RDS_SECRET_ARN` | RDS 엔드포인트, Secrets Manager의 `rds!db-…` 시크릿(사용자 이름, ARN) |
| `REDIS_ENDPOINT` | ElastiCache 기본 엔드포인트 |
| `LOG_BUCKET`, `LANGFUSE_BUCKET` | S3 버킷 2개 (LiteLLM 요청 로그용, Langfuse 이벤트용) |
| `ACM_CERT_ARN` | LiteLLM·Langfuse 도메인을 포함한 ACM 인증서 |
| `GUARDRAIL_ID` | Bedrock Guardrail ID (Claude Sonnet 4.6 모델 액세스 필요) |
| `LITELLM_HOST`, `LANGFUSE_HOST` | 사용할 도메인 2개 (DNS는 7·8단계에서 연결) |
| `ALLOWED_CIDR` | ALB에 접속할 내 공인 IP (`curl <https://checkip.amazonaws.com`>) |

---

## 2. Secrets Manager 시크릿 만들기 (콘솔)

**경로: Secrets Manager › 보안 암호 › 새 보안 암호 저장 › 다른 유형의 보안 암호(키/값)**

!images/secrets-manager.png

| 시크릿 이름 | 키 |
| --- | --- |
| `rds!db-…` | RDS 관리형 마스터 암호 `username`, `password` |
| `aws-ue1-litellm-redis-auth` | `REDIS_PASSWORD` = ElastiCache AUTH 토큰과 같은 값 |
| `aws-ue1-litellm-app-secrets` | `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `TAVILY_API_KEY` (Langfuse 키 2개는 7단계에서 추가) |
| `aws-ue1-langfuse-runtime` | `dbPassword`, `salt`, `encryptionKey`, `nextauthSecret`, `clickhousePassword` |

무작위 값은 아래 명령으로 만들어 붙여 넣습니다.

```bash
echo "LITELLM_MASTER_KEY  sk-$(openssl rand -hex 24)"
echo "LITELLM_SALT_KEY    $(openssl rand -hex 24)"
echo "dbPassword          $(openssl rand -hex 16)"
echo "salt                $(openssl rand -hex 16)"
echo "encryptionKey       $(openssl rand -hex 32)"
echo "nextauthSecret      $(openssl rand -hex 32)"
echo "clickhousePassword  $(openssl rand -hex 16)"
```

### litellm-sececrets

!images/app-secret-keys.png

- `LITELLM_SALT_KEY`, `LITELLM_MASTER_KEY` 는 LiteLLM이 DB에 모델 자격 증명을 저장하기 시작한 뒤에는 바꾸지 않습니다.
- `TAVILY_API_KEY`  : Tavily API 키(웹 검색 도구)를 설정합니다. (1D/1000Token 무료)
    - 회원가입 후 로그인한 뒤 API 발급

### langfuse-secrets

!images/langfuse-secret-keys.png

- `dbPassword`는 7.1에서 Langfuse DB 계정을 만들 때 같은 값을 씁니다.

**ARN 3개 기록:** `rds!db-…`, `aws-ue1-litellm-redis-auth`, `aws-ue1-litellm-app-secrets` 상세 화면의 "보안 암호 ARN"을 복사해 둡니다.

!images/image.png

---

## 3. Git 다운로드 후 구축을 위한 환경변수 값 작성

 `manifests/`, `iam/` 파일에는  `__AWS_ACCOUNT_ID__` 와 같은 값이 들어 있습니다. 
접속 AWS Account 값과 설정 값들은 아래 **①** 환경변수 값 파일 만들기에서 변경해주어야 합니다.

```bash
git clone https://github.com/ganplankk/00-eks_litellm-langfuse_demo.git
```

**① 환경변수 값 파일 만들기** 

```bash
cat > lab.env <<'EOF'
export AWS_PROFILE='default'
export AWS_ACCOUNT_ID='<AWS ACCOUNT ID>'
export VPC_ID='<VPD ID>'
export PUBLIC_SUBNETS='<퍼블릭서브넷 이름>,<퍼블릭서브넷 이름>'
export RDS_ENDPOINT='RDS 엔드포인트 주소'
export RDS_MASTER_USER='<RDS DB Master 계정>'
export RDS_SECRET_ARN='<Secrets Manager ARN>'
export REDIS_ENDPOINT='<Redis Endpoint 주소>'
export REDIS_SECRET_ARN='<Secrets Manager ARN>'
export APP_SECRET_ARN='<Secrets Manager ARN>'
export ACM_CERT_ARN='<LiteLLM 도메인 ACM>'
export GUARDRAIL_ID='<AWS 가드레일 ID>'
export LOG_BUCKET='<LiteLLM 로그 Bucket 이름>'
export LANGFUSE_BUCKET='<Langfuse Bucket 이름>'
export LITELLM_HOST='<LiteLLM 접속도메인>'
export LANGFUSE_HOST='<Langfuse 접속도메인>'
export ALLOWED_CIDR='<접속 허용 IP주소 대역>'
EOF
lab.env
```

- `PUBLIC_SUBNETS`는 서브넷 **이름(Name 태그)** 2개를 쉼표로 이어 씁니다.
- `RDS_MASTER_USER`는 RDS 시크릿의 `username` 값입니다(7.1에서 사용).

**② 환경변수 값 자동 적용 → /iam*, /manifests***

```bash
source lab.env

# 현재 디렉터리의 manifests와 iam 파일 내의 템플릿 변수를 직접 치환합니다.
grep -rl '__[A-Z_]*__' manifests iam | xargs sed -i \
  -e "s|__RDS_SECRET_ARN__|${RDS_SECRET_ARN}|g" \
  -e "s|__REDIS_SECRET_ARN__|${REDIS_SECRET_ARN}|g" \
  -e "s|__APP_SECRET_ARN__|${APP_SECRET_ARN}|g" \
  -e "s|__ACM_CERT_ARN__|${ACM_CERT_ARN}|g" \
  -e "s|__RDS_ENDPOINT__|${RDS_ENDPOINT}|g" \
  -e "s|__REDIS_ENDPOINT__|${REDIS_ENDPOINT}|g" \
  -e "s|__ALLOWED_CIDR__|${ALLOWED_CIDR}|g" \
  -e "s|__GUARDRAIL_ID__|${GUARDRAIL_ID}|g" \
  -e "s|__LOG_BUCKET__|${LOG_BUCKET}|g" \
  -e "s|__LANGFUSE_BUCKET__|${LANGFUSE_LANGFUSE_BUCKET}|g" \
  -e "s|__LANGFUSE_HOST__|${LANGFUSE_HOST}|g" \
  -e "s|__LITELLM_HOST__|${LITELLM_HOST}|g" \
  -e "s|__PUBLIC_SUBNETS__|${PUBLIC_SUBNETS}|g" \
  -e "s|__AWS_ACCOUNT_ID__|${AWS_ACCOUNT_ID}|g"

# 원본 파일들에 미처 치환되지 못한 변수가 남아있는지 확인합니다.
grep -rn '__[A-Z_]*__' manifests iam || echo "모든 값이 채워졌습니다"
```

---

## 4. EKS 클러스터 만들기 (콘솔)

### 4.1 IAM 역할 2개

| 역할 | 신뢰 대상 | 연결할 AWS 관리형 정책 |
| --- | --- | --- |
| `aws-ue1-litellm-eks-cluster-role` | EKS (`eks.amazonaws.com`) | `AmazonEKSClusterPolicy` |
| `aws-ue1-litellm-eks-node-role` | EC2 (`ec2.amazonaws.com`) | `AmazonEKSWorkerNodePolicy`, `AmazonEC2ContainerRegistryPullOnly` |

IAM › 역할 › 역할 생성 › AWS 서비스에서 "EKS - Cluster", "EC2"를 고르면 신뢰 정책이 자동으로 들어갑니다.

### 4.2 클러스터 생성

**경로: EKS › 클러스터 추가 › 생성**

| 항목 | 설정값 |
| --- | --- |
| 클러스터 이름 | `aws-ue1-litellm-eks` |
| 클러스터 유형 | 일반 EKS 클러스터, Auto Mode 비활성화 |
| Kubernetes 버전 | 1.37 |
| 클러스터 IAM 역할 | `aws-ue1-litellm-eks-cluster-role` |
| 인증 모드 | EKS API, 클러스터 생성자 관리자 액세스 허용 |
| 네트워크 | 실습 VPC, 프라이빗 서브넷 2개 |
| API 엔드포인트 | 퍼블릭 및 프라이빗, 퍼블릭 접근 CIDR은 내 공인 IP |
| 애드온 추가 | VPC CNI, CoreDNS, kube-proxy, EKS Pod Identity Agent, Amazon EBS CSI Driver |

### 4.3 애드온 IAM 역할 (VPC CNI, EBS CSI)

**경로: EKS › 클러스터 › 추가 기능 › 해당 애드온 › 편집**

VPC CNI와 EBS CSI Driver는 "Pod Identity 연결"에서 **권장 역할 생성**을 눌러 역할을 만들고 연결합니다.

| 애드온 | 네임스페이스 / ServiceAccount | 권장 역할의 정책 |
| --- | --- | --- |
| Amazon VPC CNI | `kube-system` / `aws-node` | `AmazonEKS_CNI_Policy` |
| Amazon EBS CSI Driver | `kube-system` / `ebs-csi-controller-sa` | `AmazonEBSCSIDriverPolicy` |

!images/vpc-cni-association.png

### 4.4 노드 그룹

**경로: EKS › 클러스터 › aws-ue1-litellm-eks › 컴퓨팅 › 노드 그룹 추가**

| 항목 | 설정값 |
| --- | --- |
| 이름 | `aws-ue1-litellm-eks-m7` |
| 노드 IAM 역할 | `aws-ue1-litellm-eks-node-role` |
| AMI | Amazon Linux 2023 (x86_64) Standard |
| 인스턴스 / 용량 | `m7i.2xlarge` / 온디맨드 |
| 디스크 | 30 GiB |
| 크기 | 최소 1, 원하는 크기 1, 최대 1 |
| 서브넷 | 프라이빗 서브넷 1개 |
| 원격 액세스 | 끔 |

!images/nodegroup.png

### 4.5 kubectl 연결

```bash
source lab.env
aws eks update-kubeconfig --region us-east-1 --name aws-ue1-litellm-eks
kubectl get nodes
kubectl get pods -n kube-system
```

노드 1개가 `Ready`이고, kube-system 파드가 모두 `Running`이면 됩니다.

---

## 5. IAM 역할과 Pod Identity 연결

파드가 ServiceAccount에 연결된 IAM 역할의 권한으로 AWS API를 호출하게 하는 설정입니다. 역할마다 ① 권한 정책, ② 신뢰 정책, ③ Pod Identity 연결을 만듭니다.

!images/pod-identity-flow.png
AWS Secrets Manager에서 값을 가져오기 위한 ESO Pod Identity 연결 예시

**Pod Identity 연결 대상 IAM Role 4개** 

| 역할 이름 | 권한 정책 | 신뢰 정책 | 네임스페이스 / ServiceAccount |
| --- | --- | --- | --- |
| `aws-ue1-litellm-eso-role` | `eso-read-secrets.json` | `eso-trust.json` | `external-secrets` / `external-secrets` |
| `aws-ue1-litellm-lbc-role` | `lbc-policy.json` (아래에서 내려받음) | `lbc-trust.json` | `kube-system` / `aws-load-balancer-controller` |
| `aws-ue1-litellm-app-role` | `litellm-bedrock.json`, `litellm-callbacks.json` | `litellm-trust.json` | `litellm` / `litellm` |
| `aws-ue1-langfuse-app-role` | `langfuse-s3.json` | `langfuse-trust.json` | `langfuse` / `langfuse` |

LBC 권한 정책을 내려받고, 콘솔에 붙여 넣을 JSON을 출력합니다.

```bash
curl -fsSL <https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v3.6.2/docs/install/iam_policy.json> \
  -o build/iam/lbc-policy.json
for f in build/iam/*.json; do echo "===== $f"; cat "$f"; echo; done
```

**IAM Role 마다 반복**

1. **권한 정책:** IAM › 정책 › 정책 생성 › JSON에 권한 정책 파일 내용을 붙여 넣고 저장합니다. 
2. **역할:** IAM › 역할 › 역할 생성 › 사용자 지정 신뢰 정책에 신뢰 정책 파일 내용을 붙여 넣고, 1번 정책을 연결해 표의 이름으로 저장합니다. `aws-ue1-litellm-app-role`은 권한 정책 2개를 모두 연결합니다.
3. **Pod Identity 연결:** EKS › 클러스터 › `aws-ue1-litellm-eks` › 액세스 › Pod Identity 연결 › 생성에서 역할, 네임스페이스, ServiceAccount를 표대로 고릅니다. **"세션 태그 비활성화"는 체크하지 않습니다.** 신뢰 정책이 세션 태그로 클러스터·네임스페이스·ServiceAccount를 확인하기 때문입니다.
    
    !images/pod-identity-create.png
    

---

## 6. 클러스터 공통 구성 설치

```bash
source lab.env

# 네임스페이스
kubectl create namespace litellm --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace langfuse --dry-run=client -o yaml | kubectl apply -f -

# Helm 저장소
helm repo add external-secrets <https://charts.external-secrets.io>
helm repo add eks <https://aws.github.io/eks-charts>
helm repo add langfuse <https://langfuse.github.io/langfuse-k8s>
helm repo update

# External Secrets Operator: Secrets Manager → Kubernetes Secret 동기화
helm upgrade --install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace --version 2.14.0 \
  --set installCRDs=true --set serviceAccount.name=external-secrets \
  --wait --timeout 5m

# AWS Load Balancer Controller: Ingress → ALB 생성
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system --version 3.6.2 \
  --set clusterName=aws-ue1-litellm-eks --set region=us-east-1 --set vpcId="$VPC_ID" \
  --set replicaCount=1 \
  --set serviceAccount.create=true --set serviceAccount.name=aws-load-balancer-controller \
  --set controllerConfig.featureGates.ALBGatewayAPI=false \
  --set controllerConfig.featureGates.NLBGatewayAPI=false \
  --wait --timeout 5m

# cert-manager, ClickHouse operator: Langfuse 차트의 ClickHouse에 필요
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
  --version v1.20.2 -n cert-manager --create-namespace \
  --set crds.enabled=true --wait --timeout 10m
helm upgrade --install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm \
  --version 0.0.8 -n clickhouse-operator --create-namespace \
  --wait --timeout 10m
```

확인:

```bash
kubectl get pods -n external-secrets
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller
kubectl get endpoints aws-load-balancer-webhook-service -n kube-system
kubectl get pods -n cert-manager
kubectl get pods -n clickhouse-operator
```

모든 파드가 `Running`이고, AWS Ingress Operator 서비스의 `ENDPOINTS`에 IP가 보이면 됩니다.
*AWS Load Balancer Controller는 쿠버네티스 클러스터 안에서 Ingress 등의 리소스를 감시하고 실제 AWS ALB를 생성·관리하는 오퍼레이터(Operator)입니다.*

---

## 7. Langfuse 배포

### 7.1 Langfuse DB 계정 만들기

RDS 마스터 계정으로 접속해 Langfuse 전용 계정과 DB를 만듭니다.

```bash
source lab.env
kubectl run pg-admin -n litellm --rm -it --restart=Never \
  --image=postgres:16.15-bookworm \
  --env=PGHOST="$RDS_ENDPOINT" --env=PGSSLMODE=require \
  -- psql -U "$RDS_MASTER_USER" -d postgres
```

- 암호를 물으면 RDS 시크릿의 `password`를 붙여 넣습니다 (Secrets Manager › 보안 암호 값 검색).

psql 프롬프트에서 실행합니다.

```sql
CREATE ROLE langfuse_app LOGIN;
\password langfuse_app
GRANT langfuse_app TO CURRENT_USER;
CREATE DATABASE langfuse OWNER langfuse_app;
\q
```

`\password`가 암호를 물으면 2단계의 `dbPassword` 값을 입력합니다.

### 7.2 Helm 배포

```bash
source lab.env
helm upgrade --install langfuse langfuse/langfuse --version 2.1.7 \
  -n langfuse -f build/manifests/03-langfuse-values.yaml \
  --wait --timeout 20m
kubectl get pods,pvc,ingress -n langfuse
```

`langfuse-web`, `langfuse-worker`, `langfuse-clickhouse-0-0-0`, `langfuse-keeper-0-0`이 `Running`이고 PVC가 `Bound`되어야 합니다.

### 7.3 DNS 연결

```bash
kubectl get ingress -n langfuse -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}{"\n"}'
```

사용하는 DNS 도메인이 있는 경우 Authorization DNS에서 ALB 도메인을 CNAME레코드를 통해 위임합니다.

### 7.4 프로젝트 키 발급 → 앱 시크릿에 추가

1. 브라우저에서 `https://<LANGFUSE_HOST>`에 접속해 가입하고, 조직과 프로젝트를 만듭니다.
2. 프로젝트 Settings › API Keys › Create new API keys로 Public Key와 Secret Key를 발급합니다.
3. Secrets Manager › `aws-ue1-litellm-app-secrets` › 편집에서 두 키를 추가합니다.
    
    
    | 키 | 값 |
    | --- | --- |
    | `LANGFUSE_PUBLIC_KEY` | `pk-lf-…` |
    | `LANGFUSE_SECRET_KEY` | `sk-lf-…` |

---

## 8. LiteLLM 배포

```bash
source lab.env

# ① 사전 준비물: SecretStore, ExternalSecret 2개, ConfigMap 2개(CA 인증서, 콜백 py)
kubectl apply --server-side -k build/manifests/01-litellm-prereqs
kubectl wait --for=condition=Ready -n litellm --timeout=120s \
  secretstore/aws-secretsmanager \
  externalsecret/litellm-backend-secrets externalsecret/litellm-runtime-secrets

# ② LiteLLM Helm 차트 소스 (v1.103.3, 커밋 고정)
[ -d .vendor/litellm/.git ] || {
  git clone --depth 1 --filter=blob:none --sparse --branch v1.103.3 \
    <https://github.com/BerriAI/litellm.git> .vendor/litellm
  git -C .vendor/litellm sparse-checkout set helm/litellm-helm
}
[ "$(git -C .vendor/litellm rev-parse HEAD)" = ecae261b100cdf6bcb1d024ae69e4efa4a891be0 ] \
  && echo "차트 커밋 확인" || echo "차트 커밋이 다릅니다. 진행하지 마세요"
helm dependency build .vendor/litellm/helm/litellm-helm

# ③ 배포: DB 초기화 → migration → 앱
helm upgrade --install litellm .vendor/litellm/helm/litellm-helm -n litellm \
  -f build/manifests/04-litellm-values.yaml \
  --set-string podAnnotations.checksum/callbacks="$(cat build/manifests/01-litellm-prereqs/py/*.py | sha256sum | cut -c1-16)" \
  --wait --timeout 10m
kubectl logs job/litellm-db-init -n litellm
kubectl get pods,ingress -n litellm
```

- ① 다음 `wait`가 시간 초과되면 2단계 시크릿의 키 이름과 5단계 ESO 역할 정책을 확인합니다(10단계).
- `litellm-…` 파드가 `1/1 Running`이면 됩니다. 끝난 Job 파드의 `0/1 Completed`는 정상입니다.

DNS 연결:

```bash
kubectl get ingress litellm -n litellm -o jsonpath='{.status.loadBalancer.ingress[0].hostname}{"\n"}'
```

사용하는 DNS 도메인이 있는 경우 Authorization DNS에서 ALB 도메인을 CNAME레코드를 통해 위임합니다.


---
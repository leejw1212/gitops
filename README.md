# k8s 실습 클러스터

맥북(Apple M1)에 kind 로 올린 학습용 쿠버네티스 클러스터.

## 구성

| 항목 | 값 |
|---|---|
| 도구 | kind v0.33.0 |
| 쿠버네티스 | v1.37.0 (`kindest/node:v1.37.0`) |
| 클러스터 이름 | `lab` (컨텍스트: `kind-lab`) |
| 노드 | control-plane 1 + worker 2 |
| 파드 IP 대역 | 10.244.0.0/16 (Calico 기본값 192.168.0.0/16 은 집 대역과 충돌) |
| CNI | **Calico v3.32.2** (기본 kindnet 을 끄고 교체) |
| StorageClass | `standard` (local-path-provisioner, 기본값) |
| Ingress | ingress-nginx `controller-v1.15.1` |
| 컨테이너 런타임 | Docker Desktop 29.2.0 |

호스트 80/443 → control-plane 노드로 매핑되어 있어 맥에서 `http://localhost` 로 바로 접근된다.

## 디렉터리

```
~/k8s
├── cluster/kind-config.yaml          클러스터 정의
├── manifests/
│   ├── ingress-nginx/                업스트림 + kustomize 패치
│   ├── calico/                       tigera-operator + Installation
│   ├── netpol-demo/                  NetworkPolicy 실습 세트
│   └── smoke-test/echo.yaml          종단 검증용 샘플 앱
└── docs/                             단계별 정리
```

## 자주 쓰는 명령

```bash
# 클러스터 생성 / 삭제
kind create cluster --config ~/k8s/cluster/kind-config.yaml
kind delete cluster --name lab

# 컨텍스트
kubectl config use-context kind-lab

# ingress-nginx 재적용
kubectl apply -k ~/k8s/manifests/ingress-nginx

# Calico 재적용 (클러스터를 새로 만들었을 때)
kubectl apply --server-side -f ~/k8s/manifests/calico/tigera-operator.yaml
kubectl apply -f ~/k8s/manifests/calico/installation.yaml

# 종단 검증
kubectl apply -f ~/k8s/manifests/smoke-test/echo.yaml
curl http://localhost/hostname

# 로컬 이미지를 클러스터에 넣기 (레지스트리 없이)
kind load docker-image <이미지> --name lab
```

## 자원 사용 (유휴 상태)

| | |
|---|---|
| 메모리 | 약 3.0GB (control-plane 1.40GB + 워커 각 ~810MB) |
| 노드 이미지 | 5.5GB |
| 볼륨 | 5.0GB |

kindnet 이었을 때는 메모리 합이 약 1.25GB 였다. Calico 로 바꾸면서 2.4배가 됐다.

## 알아둘 것

- `ingress-nginx-controller` 서비스의 `EXTERNAL-IP` 는 **`<pending>` 이 정상**이다.
  kind 에는 LoadBalancer 를 할당할 클라우드 컨트롤러가 없다. 트래픽은
  hostPort + kind 포트 매핑으로 들어온다.
- 컨트롤러는 `ingress-ready=true` 라벨로 control-plane 에 **고정**돼 있다.
  이유는 `docs/01-cluster-setup.md` 참고.

- 클러스터를 다시 만들면 **Calico 를 먼저** 깔아야 노드가 Ready 가 된다.
  CNI 가 없는 동안 노드는 `NotReady`, CoreDNS 는 `Pending` 인데 정상이다.
- kindnet 시절 설정은 `cluster/kind-config.kindnet.yaml.bak` 에 남겨 두었다.

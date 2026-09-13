# 맥북에 kind 로 쿠버네티스 실습 클러스터 세우기

구성일: 2026-09-12
대상: Apple M1 / 16GB / macOS 14.8.3

---

## 0. 왜 이 조합인가

로컬에 쿠버네티스를 올리는 방법은 여럿이다. 고르기 전에 내 맥의 상태부터 봤다.

| 항목 | 값 | 판단 |
|---|---|---|
| 기기 | Apple M1 (8코어), 16GB RAM, arm64 | 충분 |
| 디스크 | 245GB 중 27GB 여유 (88%) | **여기가 제약** |
| Docker Desktop | 설치돼 있으나 중지, VM 이미 6GB 점유 | 재활용 가능 |
| kubectl / helm | 이미 있음 | 그대로 사용 |

디스크가 걸려서 정리부터 했다. 27GB → 46GB 확보한 뒤 진행했다.

### 런타임: Docker Desktop 재활용

Colima 로 갈아탈까 고민했지만, Docker Desktop 을 지우기 전까지는 VM 이 두 개가 되어
오히려 디스크를 더 먹는다. 이미 6GB 짜리 VM 이 있으니 그걸 그대로 쓰는 쪽이 추가 비용이 없다.
개인 맥이라 라이선스 문제도 없다.

### 배포판: k3d 가 아니라 kind

처음엔 k3d(k3s)를 잡았다가 kind 로 바꿨다. 학습이 목적이라면 이쪽이 맞다.

|  | k3d (k3s) | **kind** |
|---|---|---|
| 배포판 | k3s — 경량화된 변형 | **업스트림 vanilla k8s** |
| 부트스트랩 | k3s 자체 방식 | **kubeadm** (실제 운영과 동일) |
| Ingress | Traefik 내장 | 직접 설치 |
| LoadBalancer | ServiceLB 내장 | 없음 |

k3s 는 편하라고 여러 컴포넌트를 미리 넣고 일부를 단순화해 둔 배포판이다.
빠르게 뭔가 띄우기엔 좋지만, "쿠버네티스를 배운다"는 목적에선 그 편의가 오히려 가린다.
kind 는 CNCF 적합성 테스트에 쓰이는 도구답게 kubeadm 으로 정식 쿠버네티스를 그대로 올린다.
없는 건 직접 넣어야 하는데, **그 과정 자체가 실습 소재**가 된다.

---

## 1. 설치와 버전 맞추기

```bash
brew install kind
```

여기서 한 번 걸렸다. kind v0.33.0 의 기본 노드 이미지는 `kindest/node:v1.37.0` 인데,
맥에 깔려 있던 kubectl 은 v1.31.3 이었다. **6개 마이너 차이**다.

쿠버네티스의 버전 스큐 정책은 kubectl 과 API 서버 사이를 **±1 마이너**까지만 보장한다.
그 밖은 "동작할 수도 있다" 수준이라 이상한 곳에서 터진다. 맞춰 준다.

```bash
brew upgrade kubernetes-cli   # 1.31.3 → 1.37.0
```

---

## 2. 클러스터 정의

kind 는 명령줄 플래그로도 클러스터를 만들 수 있지만, 설정 파일로 두면
언제든 똑같이 재현되고 왜 그렇게 했는지가 파일에 남는다.

`cluster/kind-config.yaml`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: lab

nodes:
  - role: control-plane
    image: kindest/node:v1.37.0
    labels:
      ingress-ready: "true"
    extraPortMappings:
      - containerPort: 80
        hostPort: 80
        protocol: TCP
      - containerPort: 443
        hostPort: 443
        protocol: TCP
  - role: worker
    image: kindest/node:v1.37.0
  - role: worker
    image: kindest/node:v1.37.0
```

세 가지를 의도했다.

**워커를 2대 둔 이유.** 단일 노드면 스케줄러가 고를 대상이 하나뿐이라
nodeSelector, affinity, taint/toleration, PodDisruptionBudget 실습이 전부 무의미해진다.
"어느 노드로 갔는지"가 눈에 보여야 배울 게 생긴다.
kind 는 노드가 도커 컨테이너라 2대를 더 띄워도 메모리 400MB 수준이다.

**extraPortMappings.** kind 에는 LoadBalancer 에 실제 IP 를 꽂아 줄 클라우드 컨트롤러가 없다.
맥의 80/443 을 control-plane 노드 컨테이너로 직접 매핑해 두는 것이
외부 트래픽이 들어올 유일한 통로다.

**ingress-ready 라벨.** 뒤에 나온다.

생성:

```bash
kind create cluster --config cluster/kind-config.yaml
```

1분 25초 걸렸다. 노드 이미지 내려받는 시간이 대부분이다.

```
NAME                STATUS   ROLES           AGE   VERSION
lab-control-plane   Ready    control-plane   37s   v1.37.0
lab-worker          Ready    <none>          22s   v1.37.0
lab-worker2         Ready    <none>          22s   v1.37.0
```

StorageClass 는 `standard`(local-path-provisioner)가 기본값으로 이미 들어와 있다.
PV/PVC 실습은 추가 작업 없이 바로 된다.

---

## 3. Ingress 컨트롤러 — 여기서 한 번 막힌다

ingress-nginx 는 kind 전용 매니페스트를 따로 제공한다. 버전을 고정해서 받았다.

```bash
curl -sSL https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml \
  -o manifests/ingress-nginx/deploy.yaml
```

적용하기 전에 열어 봤는데, 예상과 달랐다.

```yaml
ports:
- containerPort: 80
  hostPort: 80        # ← 파드가 뜬 "그 노드의" 80 을 점유한다
...
nodeSelector:
  kubernetes.io/os: linux    # ← 이게 전부다
```

**문제가 보인다.**

과거 이 매니페스트는 `ingress-ready=true` 를 nodeSelector 로 썼는데, v1.15.1 에는 그게 없다.
nodeSelector 가 `kubernetes.io/os: linux` 뿐이라 컨트롤러 파드는 **세 노드 어디로든** 갈 수 있다.

그런데 컨트롤러는 `hostPort` 로 80/443 을 연다. 즉 **파드가 떠 있는 그 노드에서만** 포트가 열린다.
내 kind 설정은 맥의 80/443 을 control-plane 컨테이너에만 매핑해 뒀다.

→ 컨트롤러가 워커로 스케줄되면, hostPort 는 그 워커에서 열리고
맥에서 `http://localhost` 를 때려도 **아무 데도 닿지 않는다.**
게다가 스케줄 결과에 따라 되기도 하고 안 되기도 하니, 원인 찾기가 고약한 종류의 문제다.

### 해결: 노드 고정

kind-config 에 심어 둔 `ingress-ready` 라벨이 여기서 쓰인다.
업스트림 파일은 건드리지 않고 kustomize 패치로 차이만 분리했다.
나중에 버전을 올릴 때 원본만 갈아끼우면 되기 때문이다.

`manifests/ingress-nginx/pin-to-control-plane.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-nginx-controller
  namespace: ingress-nginx
spec:
  template:
    spec:
      nodeSelector:
        kubernetes.io/os: linux
        ingress-ready: "true"
```

`kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deploy.yaml
patches:
  - path: pin-to-control-plane.yaml
    target:
      kind: Deployment
      name: ingress-nginx-controller
```

```bash
kubectl apply -k manifests/ingress-nginx
```

배치 확인:

```
파드: ingress-nginx-controller-575c4d8bc6-mbgz5 | Running | 노드: lab-control-plane
```

의도한 자리에 붙었다.

### EXTERNAL-IP 가 pending 인 건 정상이다

```
NAME                       TYPE           EXTERNAL-IP   PORT(S)
ingress-nginx-controller   LoadBalancer   <pending>     80:31596/TCP,443:32121/TCP
```

kind 에는 LoadBalancer 에 IP 를 할당해 줄 주체가 없어서 영원히 pending 이다.
고장이 아니다. 트래픽은 이 서비스가 아니라 hostPort → 포트 매핑 경로로 들어온다.

---

## 4. 종단 검증

구성이 실제로 이어졌는지 확인한다. 확인할 경로는 이렇다.

```
브라우저(맥) :80
  → kind extraPortMappings (control-plane 컨테이너 :80)
    → ingress-nginx hostPort :80
      → Ingress 규칙
        → Service (ClusterIP)
          → Pod (워커에 분산)
```

`agnhost`(쿠버네티스 e2e 테스트 공식 이미지, arm64 지원)를 3 레플리카로 띄우고
`topologySpreadConstraints` 로 노드에 흩뿌렸다. `/hostname` 으로 응답한 파드 이름이 나온다.

파드 배치:

```
echo-557947968f-6mppk   Running   lab-worker
echo-557947968f-nlxwv   Running   lab-worker2
echo-557947968f-wd8nq   Running   lab-worker
```

워커 2대에 나뉘었다. control-plane 에는 안 갔다 — taint 가 걸려 있기 때문이다.

맥에서 30번 호출한 결과:

```bash
$ for i in $(seq 1 30); do curl -s http://localhost/hostname; echo; done | sort | uniq -c
   9 echo-557947968f-6mppk
  10 echo-557947968f-nlxwv
  11 echo-557947968f-wd8nq
```

맥 브라우저에서 시작한 요청이 노드 컨테이너를 지나 워커의 파드까지 닿고,
Service 가 고르게 분산하고 있다. 구성 끝.

---

## 5. 실제 비용

유휴 상태 기준.

| | |
|---|---|
| 메모리 | 약 1.25GB (control-plane 833MB, 워커 각 ~208MB) |
| 노드 이미지 | 4.8GB |
| 볼륨 | 3.0GB |
| Docker VM 증가분 | 6.0GB → 8.5GB |

16GB 맥에서 1.25GB 는 부담이 아니다. 디스크는 8GB 정도를 새로 썼다.

---

## 다음에 할 것

- kindnet 을 Calico 로 교체해 NetworkPolicy 실습
- PV/PVC + StatefulSet
- metrics-server 올리고 HPA
- taint/toleration, affinity 로 스케줄링 직접 조작

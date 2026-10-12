# vllm-autoscaling-kit

Kustomize manifests for running vLLM on Kubernetes and autoscaling it on **queue depth** with KEDA, instead of CPU or GPU utilization.

Plus a Grafana dashboard to watch it work and a small load generator to prove it.

## why

Put vLLM behind a normal HPA and it scales on CPU. CPU sits low because the real work is on the GPU, so the HPA does nothing while requests pile up. GPU utilization isn't much better: with continuous batching a GPU at 95% might be fine or might be one request from queueing.

What you actually want to know is: are requests waiting? vLLM exports that as `vllm:num_requests_waiting`. This kit scales on it.

Longer writeup: [Stop autoscaling vLLM on CPU](https://bhaddre.github.io/2026/10/autoscaling-vllm-on-queue-depth/)

## what's in here

```
base/                    vLLM Deployment, Service, PDB, KEDA ScaledObject
components/servicemonitor/  Prometheus Operator scrape config (optional)
overlays/gke-l4/         GKE with NVIDIA L4 node pool
overlays/eks-g6/         EKS with g6 (L4) instances
dashboards/              Grafana dashboard: queue, replicas, TTFT, latency
loadtest/                burst load generator that reports TTFT per phase
scripts/validate.sh      render + schema-validate every overlay
```

## the choices that matter

| setting | value | why |
|---|---|---|
| scale metric | `vllm:num_requests_waiting` | it's what users are waiting on |
| `minReplicaCount` | 1 | cold start is minutes (node + image + weights). scale-to-zero means the first request after idle times out |
| scale up | 2 pods / min, no stabilization | react fast, but don't grab every GPU in the pool on one spike |
| scale down | 1 pod / 5 min, 10 min window | killing a replica as soon as the queue drains means paying the cold start again on the next burst |
| `fallback` | 2 replicas after 3 failed polls | if Prometheus is down, hold a safe count instead of scaling blind |
| `maxSurge: 0` | | rollouts shouldn't need a spare GPU you might not have |
| startup probe | up to 15 min | weight loading is slow. without it liveness kills pods mid-load |
| `/dev/shm` | 2Gi memory emptyDir | default 64Mi breaks tensor parallel |

Threshold is 4 waiting requests per replica. That's a starting point, not an answer. Tune it against your TTFT target using the load test.

## prerequisites

- GPU node pool with the NVIDIA device plugin, tainted `nvidia.com/gpu:NoSchedule`
- [KEDA](https://keda.sh) 2.x
- Prometheus scraping the vLLM pods. The overlays use the Prometheus Operator `ServiceMonitor`. If you scrape another way, drop the component.
- kube-state-metrics, for the replica panel on the dashboard

## use it

```bash
# render first, read it, then apply
kustomize build overlays/gke-l4 | less
kubectl create namespace llm
kustomize build overlays/gke-l4 | kubectl apply -f -

# gated model? add a token
kubectl -n llm create secret generic hf-token --from-literal=token=hf_xxx
```

Change the model in `base/deployment.yaml` (`MODEL_ID`, `SERVED_MODEL_NAME`). If you change the served name, update the query in `base/scaledobject.yaml` too. The `model_name` label comes from `--served-model-name`.

Check the trigger is actually reading a number:

```bash
kubectl -n llm get scaledobject vllm
kubectl -n llm get hpa keda-hpa-vllm -o yaml | grep -A5 currentMetrics
```

## prove it scales

```bash
kubectl -n llm port-forward svc/vllm 8000:8000 &
pip install -r loadtest/requirements.txt
python loadtest/burst.py --url http://localhost:8000 --model qwen2.5-7b \
  --phases 60:2,180:32,120:32,300:2
```

Output is one line per phase:

```
   60s x2    reqs=...  errors=0  ttft p50=...s p95=...s
  180s x32   reqs=...  errors=0  ttft p50=...s p95=...s
```

Open the dashboard while it runs. You should see the queue spike, desired replicas jump, available replicas follow after roughly one cold start, and TTFT come back down. If replicas go up and TTFT doesn't recover, the bottleneck is somewhere else (usually node provisioning or image pulls) and scaling faster won't help.

## heads up

- vLLM metric names have changed between versions. Curl `/metrics` on the version you run before trusting the query or the dashboard.
- The image is pinned. Bump it on purpose, not by accident.
- `hf-cache` is an emptyDir, so every new pod downloads weights again. For big models, bake weights into the image or use a read-only volume. That cuts cold start more than any autoscaler setting.
- On GKE with managed Prometheus, KEDA needs a Prometheus-compatible query endpoint. Point `serverAddress` at whatever you run.

## development

```bash
./scripts/validate.sh          # needs kustomize + kubeconform
pip install -r loadtest/requirements.txt pytest && pytest -q loadtest/tests
```

CI runs both on every push.

## license

MIT

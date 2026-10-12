#!/usr/bin/env bash
# Render every overlay and validate it against the k8s + CRD schemas.
# Needs: kustomize, kubeconform.
set -euo pipefail

cd "$(dirname "$0")/.."

K8S_VERSION="${K8S_VERSION:-1.30.0}"
CRD_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

status=0
for overlay in overlays/*/; do
  name="$(basename "$overlay")"
  echo "--- $name"
  if ! kustomize build "$overlay" \
      | kubeconform -strict -summary \
          -kubernetes-version "$K8S_VERSION" \
          -schema-location default \
          -schema-location "$CRD_SCHEMAS"; then
    status=1
  fi
done

python3 -m json.tool dashboards/vllm-autoscaling.json > /dev/null && echo "--- dashboard json ok"
exit "$status"

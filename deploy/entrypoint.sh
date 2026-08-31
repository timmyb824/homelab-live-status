#!/bin/sh
# In-cluster, kubectl has no config — synthesize one from the mounted
# service account so the k3s collector works unchanged.
set -e

SA=/var/run/secrets/kubernetes.io/serviceaccount
if [ -f "$SA/token" ] && [ -z "$KUBECONFIG" ]; then
    cat > /tmp/kubeconfig <<EOF
apiVersion: v1
kind: Config
clusters:
- name: in-cluster
  cluster:
    server: https://kubernetes.default.svc
    certificate-authority: $SA/ca.crt
users:
- name: service-account
  user:
    token: $(cat "$SA/token")
contexts:
- name: in-cluster
  context:
    cluster: in-cluster
    user: service-account
current-context: in-cluster
EOF
    export KUBECONFIG=/tmp/kubeconfig
fi

exec python -m homelab_live_status.main

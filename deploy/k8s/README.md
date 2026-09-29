# Kubernetes manifests

**Status: written, not run.** There is no cluster in the development environment,
so these have been validated for syntax and schema but have never been applied.
Treat them as a starting point that needs a real `kubectl apply --dry-run=server`
against your cluster before you trust them.

That caveat is the point — manifests that have never touched an API server are
not "deployed", and saying so is cheaper than having someone find out during a
rollout.

## What is here

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace + resource quota |
| `secrets.example.yaml` | Shape of the required secrets. **Not real values** |
| `configmap.yaml` | Non-secret configuration |
| `api.yaml` | API Deployment, Service, HPA, PDB |
| `web.yaml` | Console Deployment + Service |
| `postgres.yaml` | StatefulSet for the OMS store (optional) |

## Apply order

```bash
kubectl apply -f namespace.yaml
kubectl apply -f configmap.yaml
# Create secrets from real values -- never commit them:
kubectl -n quantgenlabs create secret generic qgl-secrets \
  --from-literal=jwt-secret="$(openssl rand -base64 48)" \
  --from-literal=postgres-password="$(openssl rand -base64 32)" \
  --from-literal=anthropic-api-key="sk-ant-..."
kubectl apply -f postgres.yaml     # optional; omit to keep SQLite
kubectl apply -f api.yaml
kubectl apply -f web.yaml
```

## Known follow-up

`deploy/Dockerfile.web` is `nginx:alpine`, which starts as root to bind port 80.
Moving it to `nginxinc/nginx-unprivileged` on 8080 is the right hardening, but it
has to change three things together — the image, `docker-compose.yml`'s `5174:80`
mapping, and the sibling Meridian stack's `proxy_pass http://qgl-web:80/` — so it
belongs in its own commit rather than hidden inside a manifest that has never been
applied. The manifest here matches what the image actually does.

## Things deliberately NOT included

- **No Ingress.** Ingress is cluster-specific (controller, annotations, TLS
  issuer) and a generic one is more likely to mislead than help.
- **No ServiceMonitor.** Requires the Prometheus Operator CRDs; the API already
  exposes `/metrics` and the annotations for scrape-by-annotation are set.
- **No autoscaling on the API's replica count below 2.** The desk holds in-memory
  FIX sessions, so scaling to zero drops live sessions. See the note in `api.yaml`.

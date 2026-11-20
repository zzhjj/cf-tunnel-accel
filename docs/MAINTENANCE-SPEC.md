# Maintenance and Acceptance Specification

This is a release/maintenance checklist, **not a record that every gate has passed**. Inspect the actual package and record the commands and results for each candidate. Operator procedures: [MAINTENANCE.md](MAINTENANCE.md). Configuration and profile boundaries: [DEPLOYMENT.md](DEPLOYMENT.md). Contributor checks: [CONTRIBUTING.md](../CONTRIBUTING.md).

## Scope

Maintain a public, reproducible Linux / Python 3.9+ CLI and local web admin for existing Cloudflare Tunnel deployments using a **Cloudflare for SaaS auxiliary zone and DNS-only preferred CNAME**. This is not a replacement Tunnel protocol, Anycast controller, guaranteed speed product, universal OS package, or public multi-tenant control plane.

Cloudflare zone/SaaS enablement, Tunnel creation, scoped credentials and correct origin routing are operator prerequisites. No private host defaults may be required to reproduce documented setup.

## Acceptance requirements

### 1. Complete package and installation

- Ship `cf-tunnel-accel`, `cfa_config.py`, `cfa_ingress.py`, `cfa_cdn.py`, and the complete matching UI. The helpers remain importable next to the extensionless CLI and are included in upgrades/backups/rollback.
- Prefer `--user` with HOME/XDG package/config paths; root system install uses explicit system package/launcher paths. Neither assumes an original author's account, filesystem or Tunnel.
- Check `--source-dir`, `--no-deps`, `--dry-run`, custom prefixes, missing/incomplete sources and existing paths with temporary fixtures. A dry-run must not write files, download, install dependencies or modify services.
- Python 3.9+ is a prerequisite; optional dependency absence is reported without claiming related features work. Automatic cloudflared dependency installation is Linux-specific and uses the implemented official HTTPS validation path, not arbitrary mirror prefixes.
- `--service` / `--ui-service` require root system installation, explicit Tunnel targets where applicable and running systemd; absent systemd has an explained foreground fallback. Unit installation does not automatically enable/start services. The separate `schedule install` command has its own explicit activation semantics.
- Validate local staging/backups and failure paths without claiming atomic rollback of package managers, network operations or Cloudflare state.

### 2. Configuration, credentials and migration

- Default env comes from HOME/XDG; a missing normal CLI env is treated as unconfigured, while an explicit registry env must exist and be readable. Plain `KEY=VALUE` is data, not executable shell and not variable-expanded configuration.
- `init --non-interactive --config PATH --service UNIT --aux-zone example.org` creates local private configuration only. Explain `--tunnel-id`, `--account-id`, `--aux-zone-id`, `--preferred-target`, `--probe-hostname`, `--token-file`, output/overwrite behavior, and UUID extraction from the selected config.
- Normal settings use process > file > generic defaults. Credential **source groups** prevent a lower-priority inline alias from overriding a higher-priority token-file selection. Registry profiles with explicit credentials clear inherited credential aliases as a group.
- Old root deployments must explicitly preserve their own account, auxiliary zone/ID, Tunnel UUID/config/service, credential source and origin/entry settings **before upgrading**. Never publish those values or silently infer credentials from unrelated local files.
- Enforce or document the actual scope/ownership checks before writes. Minimum API permissions are limited to needed zone reads, DNS edits and auxiliary-zone SSL/custom-hostname management; do not request account-wide Tunnel write permission for managing an existing local Tunnel.
- Protect API/UI tokens, env, Tunnel JSON, registry, jobs, audit and backups with appropriate ownership/permissions. Redact public reports; do not imply cache metadata is a secret-free publication artifact.

### 3. Route and ingress safety

- The supported editor is a **pure block-style YAML subset**, not a general YAML parser. Unsupported structures fail closed with actionable errors and no partial rewrite; test boundary examples such as flow syntax, aliases/anchors and multiline structures against the actual implementation.
- Preserve unrelated supported configuration, rule order, hostname-specific origin settings and final catch-all. Avoid introducing implicit backend ports or personal fallback routes.
- Validate selected config and restart only the selected service; report local validation/restart/rollback outcomes, including failure to restore. Do not equate service active with business HTTPS success.
- HTTPS origin verification policy is explicit. A `noTLSVerify` fallback/probe is not authenticated origin identity.
- DNS, SaaS and Tunnel steps are not one transaction. An error/partial result must not be presented as a clean success or proof of no writes.

### 4. CDN evidence and bounded recovery

- Clearly distinguish candidate/network-only measurement from HTTPS using the actual business **Host and SNI**. The candidate's own certificate/page is not business validation.
- Automatic best selection requires configured `PROBE_HOSTNAME`, valid business HTTPS timings, at least **2 healthy samples** and **≥80% success**. Reject missing/invalid/non-finite data, certificate errors, failed curl, 5xx and TCP-only eligibility. Current healthy HTTP range is 2xx–4xx; that is reachability, not application correctness.
- `--force` bypasses only improvement/hysteresis, not business validity or health gates. No mutation follows invalid candidates or wholly failed evidence.
- A verified change captures prior DNS state, confirms the write and performs subsequent checks. Failure triggers a bounded restore attempt with explicit outcome, including restore failure. Do not claim global atomicity or public propagation from API read-back.
- Tests cover no prior record, unchanged target, failed confirmation, unhealthy post-check, restore failure and threshold boundaries. Operator acceptance separately covers real DNS/cache and business behavior.
- Explain shared `PREFERRED_ENTRY` impact, single-vantage sampling and uncertain public candidate/network availability; do not promise speed.

### 5. Profiles and isolation

- A trusted JSON registry selects stable IDs/display names and **absolute env-file paths**. Validate explicit local Tunnel/config/service fields, unique config/service targets, unknown IDs, duplicate query parameters and bad/missing registries. No browser-supplied arbitrary config path.
- Each account/profile explicitly selects credentials and preferably separate caches. Validate cache scope; do not silently use another account's zones. Account filtering and auxiliary-zone ownership must match the chosen profile.
- Preserve backward-compatible single-default mode when no registry exists, not when an explicitly selected registry is invalid.
- Profile views, CDN history, result ownership and stale response handling do not mix targets. Global jobs/audit/sessions and same-process execution remain documented.
- Profile CLI settings are fixed per imported module and runtime helpers use that snapshot rather than a shared active environment. Global jobs/audit/sessions, single admission gate, locks and equal cloud targets remain shared across profiles.
- One mutation worker/admission gate and configured file locks coordinate local writes; equal cloud targets, schedules and resources are still shared across profiles.

### 6. UI and HTTP boundaries

- Route-first admin navigation has six pages: routes, local services, preferred entry, diagnostics, tasks/audit and environment. Avoid documenting removed decorative topology views as current behavior.
- Expose section-level errors, unknown/stale states, timestamps and partial data; a failed read must not become an apparently healthy zero count. Health task completion and health outcome are separate.
- Validate authentication, CSRF, origin, method, content type, JSON size/type, numeric queries, route names and selected profile before dispatch. Bound queue/log/history capacity and reject conflicting writes atomically.
- Persist recent display-only job snapshots. Queued/running jobs found after restart become `interrupted`; **never automatically replay them**. Storage errors and unknown effects remain visible; operators reconcile actual resources first.
- External manual DNS tasks remain **CLI-only**, including interactive verification prompts; UI does not dispatch an operation that waits for stdin. Document manual switch/cleanup too.
- Default listener is loopback. Non-loopback requires explicit opt-in and a trusted TLS proxy/network boundary; the flag does not enable TLS. Sessions and API credentials are not per-profile tenant roles.

## Negative controls

Use mock/stub boundaries and temporary HOME/XDG/prefixes; no real cloud/service mutation during development checks.

- Missing helper/config, invalid UUID/zone/account, wrong origin ownership and unsupported YAML fail without unintended writes.
- Candidate network connectivity without business validation never qualifies automatic best; `--force` does not rescue bad evidence.
- Invalid auth/CSRF/origin/input/profile requests never call Cloudflare or systemd functions.
- Two simultaneous mutation submissions accept at most one conflicting mutation; browser reconnect does not create a second task.
- Restored job snapshots cannot resume mutation arguments; interrupted work is not silently retried.
- A profile-specific token file wins over an inherited token alias when explicitly configured; cache contents from another scope are rejected/refreshed rather than trusted.
- Non-loopback plain HTTP cannot start without explicit opt-in; bad ports and explicit bad registries fail startup.

## Non-goals and remaining limits

- No API-token management UI, automatic Cloudflare account/SaaS onboarding, remote agents, multi-host orchestration or unified cloud transaction.
- No guarantee that local tests provision a real Cloudflare account, issue real certificates, prove public DNS propagation, cover all regions or demonstrate speed improvement.
- No live acceptance transcript or fixed passing test count in public documentation. Record actual local checks, failures/skips and untested paths for each release.

## Completion record for a release

1. Record exact candidate/version, intended files and behavior; inspect implementation before updating claims.
2. Run and record actual offline syntax/test/document checks and Python versions; do not report CI or network checks that were not observed.
3. Review failure paths, privacy, relative links, placeholder examples and configuration/service privileges.
4. Verify documented upgrade/backups/rollback include helpers, explicit old-root migration and interrupted-job reconciliation.
5. Separately arrange operator-approved cloud/business acceptance if needed. A clean local diff and passing mocks are necessary evidence, not proof of real provisioning or production readiness.

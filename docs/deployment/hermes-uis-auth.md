# Hermes UIS authentication deployment

This runbook covers deployment and rollback of the Hermes UIS Dashboard bridge.
Protocol and authorization design remain in
[Hermes UIS auth bridge](../architecture/hermes-uis-auth-bridge.md).

## Trust and configuration

TLS trust and JWT signing are independent:

- `NGINX_SSL_CERT`/`NGINX_SSL_KEY` serve HTTPS. Prefer school or public PKI;
  use an internal CA only in controlled environments. The leaf certificate SAN
  must cover the hostname or IP in `HERMES_AUTH_BRIDGE_ISSUER`.
- `HERMES_AUTH_BRIDGE_SIGNING_KEY_HOST_FILE` is the dedicated Ed25519 PKCS#8
  private key. `HERMES_AUTH_BRIDGE_ACTIVE_KID` identifies it in JWT/JWKS.
  Current Compose mounts one key; do not configure secondary rotation keys
  without adding and validating their explicit read-only mounts.
- `HERMES_AUTH_BRIDGE_CA_HOST_FILE` contains only the public CA chain used to
  validate the issuer. Never put a private key in this file.

Use the configured certificate names; do not assume `nginx.crt` or
`openclaw.crt`. Keep all real keys and campus-specific values outside Git.

## Prepare and deploy

Back up `config/openclaw-manager.env`, the Nginx certificate files, and the
metadata database. The initialization command previews by default:

```bash
python3 scripts/init_hermes_uis_signing_key.py \
  --config config/openclaw-manager.env \
  --key-file /data/docker/openclaw-manager-secrets/hermes-auth-bridge-ed25519.pem \
  --kid <active-kid>
```

Review the output, then repeat with `--apply`. It creates a missing signing key
and configuration backup; it never replaces or rotates an existing key and
does not create TLS certificates. Install the TLS certificate, key and public
CA at the paths configured in the environment, then run the read-only preflight:

```bash
python3 scripts/check_hermes_uis_readiness.py
bash scripts/check_bootstrap_readiness.sh
```

Production service rebuilds must use the deployment script so preflight and
Compose readiness run before Nginx is changed:

```bash
bash scripts/deploy_services.sh
```

Do not replace this with a manual `docker compose up -d`.

## Verify and migrate

```bash
curl --fail --cacert <public-ca-file> \
  https://<manager-host>:30015/auth/hermes/jwks.json
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --cacert <public-ca-file> --data 'grant_type=readiness_probe' \
  https://<manager-host>:30015/auth/hermes/token
python3 scripts/check_metadata_consistency.py
bash scripts/check_runtime_security.sh
```

JWKS must return `200`; the deliberately invalid token probe must return `400`.
Metadata consistency checks instance records, Provider/callback files and
instance CA permissions. Runtime security separately checks live mounts,
networks, TLS and endpoints.

Preview one existing instance, inspect its backup plan, then explicitly apply:

```bash
python3 scripts/migrate_hermes_uis_auth.py \
  --db /data/docker/openclaw-public/manager.db \
  --instance <instance-public-uuid> \
  --issuer https://<manager-host>:30015/auth/hermes \
  --ca-file <public-ca-file>
# Repeat the reviewed command with --apply.
```

Finally, sign in through UIS in a browser and confirm direct Dashboard entry.
Do not batch-migrate instances automatically.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Empty CA or `/dev/null` mount | Set the host CA path; rerun preflight and the deployment script. |
| Self-signed, missing SAN, or SAN mismatch | Install a leaf certificate chained to the configured CA with issuer host/IP in SAN. |
| JWKS `503` | Validate the Ed25519 key, mode `0600`, active KID and read-only mount. |
| Token probe `400` | Expected for the deliberately invalid grant. |
| `access_denied` | Confirm the UIS identity has access to the exact instance. |
| UID `10000` cannot read CA | Fix the staged instance CA ownership/mode through the migration workflow. |
| Provider/plugin permission error | Reapply the single-instance migration after restoring its backup. |

## Rollback

Stop further migrations. Revert the application change, restore the environment
and certificate backups, then rebuild through `scripts/deploy_services.sh`.
For a migrated instance, restore the backup directory printed by
`migrate_hermes_uis_auth.py` and restart only that instance. Do not rotate or
delete the signing key while tokens issued with its KID may still be valid.

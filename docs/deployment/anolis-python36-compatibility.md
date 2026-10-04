# Anolis OS 8.6 / Python 3.6 compatibility

Anolis OS 8.6 ships Python 3.6.8, while the WSL development environment and
the Ubuntu 22.04 production environment use newer Python versions. Runtime
scripts therefore avoid subprocess arguments introduced after Python 3.6.

## Compatibility changes

- `scripts/check_hermes_uis_readiness.py` uses `universal_newlines=True`,
  `stdout=subprocess.PIPE`, and `stderr=subprocess.PIPE` instead of
  `text=True` and `capture_output=True`.
- `tests/test_hermes_uis_readiness.py` uses the same Python 3.6-compatible
  subprocess options when invoking the checker and signing-key initializer.
- `tests/test_tenant_network_allocator.py` uses the same options when invoking
  the allocator subprocess, so the compatibility suite itself runs on Python
  3.6.
- `scripts/lib_tenant_network.sh` uses the same options in its embedded Python
  helper for Docker inspection and network connection operations. This keeps
  the deployment post-check compatible with Anolis Python 3.6.
- `scripts/tenant_network_allocator.py` performs the required subcommand check
  in `main()` instead of passing `required=True` to `add_subparsers()`, which
  was added after Python 3.6.
- The allocator's shared Docker and route command runner uses
  `universal_newlines=True`, `stdout=subprocess.PIPE`, and
  `stderr=subprocess.PIPE`; this covers the runtime path exercised by
  `create_user.sh`.
- Existing tenant subnet validation compares network and broadcast boundaries
  directly instead of using `IPv4Network.subnet_of()`, which is unavailable in
  Python 3.6.
- `scripts/metadata_cli.py` performs the required subcommand check after
  parsing instead of passing `required=True` to `add_subparsers()`.
- Metadata consistency tests use Python 3.6-compatible subprocess options and
  explicitly select an isolated temporary SQLite database. They override the
  Manager config path and cannot inherit a production PostgreSQL URL.
- Host-side SQLite callers convert `pathlib.Path` values to strings before
  calling `sqlite3.connect()`. Python 3.6 does not accept path-like database
  arguments; this covers metadata writes, consistency checks, inventory, and
  every SQLite schema migration.
- Host-side authentication migration and rollback paths avoid
  `capture_output`, `text`, and `Path.unlink(missing_ok=True)`, while retaining
  the same captured output and missing-file behavior on newer Python releases.
- `scripts/check_metadata_consistency.py` uses `collections.namedtuple`
  instead of the Python 3.7 `dataclasses` module and uses Python 3.6-compatible
  subprocess and filesystem calls.
- Modules shared by Python 3.12 service containers and host-side Hermes tools
  avoid newer-only subprocess, pathlib, string-prefix, and dataclass APIs.
  Containers keep the same behavior, while `metadata_cli.py` and migration
  tools can safely import the modules with Anolis Python 3.6.
- `tests/test_python36_compatibility.py` scans the complete host runtime import
  closure for prohibited newer APIs and guards SQLite path conversion. Add any
  new host-imported service module to `HOST_RUNTIME_FILES`.
- `check_metadata_consistency.py` accepts `OPENCLAW_MANAGER_CONFIG_FILE`, and
  metadata/bridge tests explicitly select temporary SQLite and clear the
  production PostgreSQL URL. Tests therefore work as a normal deployment user
  even when the production env file is root-readable only.
- Subprocess-based metadata registration tests pass the same isolated config
  and SQLite environment to the child process; this prevents a child CLI from
  falling back to a root-only production env file.
- Metadata consistency config loading tolerates an unreadable optional env
  file and continues with explicit environment variables or safe defaults.
  This keeps read-only inventory/checker imports usable for non-root operators
  without changing production permission requirements.
- Tests pass filesystem paths through `str()` before `sqlite3.connect()` and
  inspect `unittest.mock` calls through tuple indexes instead of the
  Python 3.8+ `.args` and `.kwargs` properties.
- Metadata consistency tests clear the production instance-auth token so a
  developer or deployment host environment cannot enable unrelated UIS
  authorization checks inside isolated fixtures.
- The compatibility suite parses every `tests/test_*.py` file using the Python
  3.6 grammar and statically checks the SQLite and mock-call rules above.

## Verification

Run the compatibility checks from the repository root:

```bash
python3 -m unittest \
  tests.test_hermes_uis_readiness \
  tests.test_python36_compatibility \
  tests.test_tenant_network_allocator \
  tests.test_upgrade_metadata_consistency
```

Run the full test discovery suite on Ubuntu 22.04 / Python 3.10 before merging
and compare any unrelated failures with the same `main` revision. The
compatibility substitutions must not introduce new failures on newer runtimes:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

The replacement is behavior-preserving on Python 3.10+ and Python 3.6: it
requests decoded stdout/stderr through the older spelling and captures both
streams explicitly.

## PostgreSQL limitation on Python 3.6 hosts

The project uses psycopg 3 for PostgreSQL metadata. Psycopg 3 does not support
Python 3.6, so an Anolis host's system Python cannot run direct PostgreSQL
metadata writes even though the host-side script syntax remains compatible.
Create and manage instances through the Manager Admin UI and its containerized
Python 3.12 control plane. Do not treat a successful runtime container creation
as complete until the instance is present in PostgreSQL.

`scripts/check_bootstrap_readiness.sh` reports this host-driver limitation as a
warning. It does not reject the deployment because `manager-control` includes
psycopg and is the intended PostgreSQL metadata authority.

The readiness check also verifies that PostgreSQL deployments have the tenant
subnet pool, internal service tokens, and effective lock-file parent
directories configured. It reports a missing initial Nginx container as a
warning because Nginx is intentionally started once before Manager services.

`scripts/check_metadata_consistency.py` currently checks SQLite metadata only.
Its `manager.db` warnings are not PostgreSQL consistency results; query through
`manager-control` when validating a PostgreSQL deployment.

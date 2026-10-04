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

## Verification

Run the compatibility checks from the repository root:

```bash
python3 -m unittest \
  tests.test_hermes_uis_readiness \
  tests.test_python36_compatibility \
  tests.test_tenant_network_allocator
```

The replacement is behavior-preserving on Python 3.10+ and Python 3.6: it
requests decoded stdout/stderr through the older spelling and captures both
streams explicitly.

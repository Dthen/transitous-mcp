# Development

Run from the repository root with Python 3.10+:

```sh
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r tools/dev-requirements.txt
.venv-dev/bin/python -m pytest -q -p no:cacheprovider
```

Dependency installation may need network access. Tests run offline against
[synthetic fixtures](fixtures/README.md), not live captures. They cover parsing,
requests, routing/time rules, backend/MCP contracts and CLI subprocess behavior.
No external fixture directory or generation step is required.

## Build and check a wheel

```sh
.venv-dev/bin/python -m pip install build
.venv-dev/bin/python -m build --wheel
python3 -m venv .venv-wheel
.venv-wheel/bin/pip install --no-index --no-deps dist/transitous_mcp-3.0.0-py3-none-any.whl
.venv-wheel/bin/transitous --help
```

Builds may download tools; wheel installation above is offline. Rebuild after
README changes because package metadata embeds it. The wheel also provides
`transitous-mcp`. Source tests alone do not verify an installed package.

# Phase 1 Verification and Testing Protocol

Verify environment validation, local Ollama inventories, HTTP health checks,
short inference requests, timeouts, and node fallback. Run project commands from
the repository root on Node 1. Node 1 needs Python 3.11+; Node 2 needs Ollama.
The hardware baseline is two Intel N100-or-better computers with 8–16 GB RAM each.

## 1. Setup and prerequisites

### Hardware and LAN

Run on each node:

```bash
hostname
lscpu
free -h
ip -brief address
```

Expected: two distinct machines meeting the baseline, each with a private LAN
address. Reserve stable addresses through your router/DHCP service. Node 1 must
reach Node 2's Ollama port, normally TCP 11434. If the nodes use different subnets,
configure the required router and firewall rules.

From Node 1, enter Node 2's actual private IPv4 address:

```bash
read -r -p "Node 2 private LAN IP: " NODE2_LAN_IP
ip route get "$NODE2_LAN_IP"
```

Expected: a route containing `src <node1-source-ip>`. Use this selected source
address when allowing traffic through Node 2's firewall; it may differ from the
configured coordinator address on a computer with multiple interfaces.

### Ollama services

Run on each node:

```bash
command -v ollama
ollama --version
```

Expected: an executable path and version. `command -v` only finds an installed
command. If missing, follow the [official installation instructions](https://docs.ollama.com/linux).
Installation and initial model downloads need Internet access but no paid service.

For Linux installations using systemd, configure each service with:

```bash
sudo systemctl edit ollama.service
```

Use this override on Node 1:

```ini
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NO_CLOUD=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_CONTEXT_LENGTH=2048"
```

Use the same limits on Node 2, changing `OLLAMA_HOST` to its private LAN address
and port. For example, `Environment="OLLAMA_HOST=192.168.1.20:11434"` is valid
only if that example address is assigned to your worker. Preserve an existing
working LAN binding when merging settings. The [Ollama FAQ](https://docs.ollama.com/faq)
describes service environment variables and network access. Project `.env` values
do not configure or restart Ollama services.

After changing the override, run on each affected node:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama.service
systemctl is-active ollama.service
systemctl show ollama.service --property=Environment
sudo journalctl -u ollama.service -n 50 --no-pager
ss -ltn '( sport = :11434 )'
```

Expected: `active`, the configured environment values, and a cloud-disabled
startup message. Node 1 listens on loopback; Node 2 listens on its LAN address.
Upgrade Ollama if cloud disabling is unsupported. If a firewall is active, allow
TCP 11434 from Node 1's selected source address. No WAN port forwarding is needed.

### Python environment and configuration

Run from the repository root on Node 1:

```bash
python3 -m venv .venv
source .venv/bin/activate
command -v python3
python3 -m pip install -r requirements.txt
python3 -m pip check
cp --no-clobber .env.example .env
```

Expected: Python resolves inside this checkout's `.venv/bin/`, dependencies install,
and `pip check` prints `No broken requirements found.` An existing `.env` is preserved.

Edit `.env`: replace the illustrative addresses with your actual private LAN
addresses, select exact local model tags, and set per-node thread budgets for
your CPUs. Four threads per node suit the baseline. The example uses
`qwen2.5:7b` and `llama3.2:3b`; choose models that fit your available RAM.
Install each configured model on its corresponding node with `ollama pull`.
If that service only listens on its LAN address, set the Ollama CLI's
`OLLAMA_HOST` to that address and port. Check installed tags with `ollama list`.

The live test suite requires Q4_K_M quantization. A model's name alone does not
establish its quantization; the tests inspect `/api/tags` metadata.

Validate settings without creating storage:

```bash
python3 - <<'PY'
from src.config import get_settings
s = get_settings()
print(f"COORDINATOR_HOST={s.coordinator_host}")
for node in ("node1", "node2"):
    print(f"{node}: url={s.node_url(node)} model={s.node_model(node)} threads={s.node_threads(node)}")
print(f"DATABASE_PATH={s.database_path}")
print("CONFIG VALID")
PY
git check-ignore -v .env .venv/ docs/local/
git ls-files .env docs/local/
```

Expected: your configured addresses, models, and budgets, followed by
`CONFIG VALID`. The database path is absolute and resolves from the repository
root. `git check-ignore` reports ignore rules; `git ls-files` prints nothing.

Process environment values override dotenv values, including across legacy
aliases. `NODE1_MODEL` / `NODE2_MODEL` also accept `NODE1_OLLAMA_MODEL` /
`NODE2_OLLAMA_MODEL`; `COORDINATOR_HOST` also accepts `NODE1_HOST_IP`. Use one
name per setting. Configuration validation creates no database or directory.

## 2. Automated test execution

Dedicated test script: `tests/test_phase1_network.py`.

### Offline functional suite

```bash
python3 -m pytest tests/test_phase1_network.py -v -s
```

Expected: **114 passed, 8 skipped**. Network requests are simulated, and tests
isolate shell and dotenv settings. Coverage includes:

- LAN-only addresses, local model tags, timeout bounds, and invalid/cloud settings.
- Configuration aliases, environment precedence, and the reusable example template.
- Repository-relative database paths and validation without file creation.
- Per-node models and CPU budgets, response validation, and latency measurements.
- Connection errors, deadlines, HTTP errors, malformed JSON, and both fallback directions.
- Proxy/redirect disabling and HTTP client cleanup.

### Optional single-node check

While bringing up the cluster, test Node 2 from Node 1:

```bash
python3 -m pytest tests/test_phase1_network.py --run-lan --lan-node node2 -m lan -v -s
```

Expected diagnostics use your configuration:

```text
HEALTH node2 url=<configured-worker-url> version=<version> latency_ms=<number>
MODEL node2 name=<configured-model> quantization=Q4_K_M latency_ms=<number>
INFERENCE node2 model=<configured-model> latency_ms=<number> response=<nonempty-text>
FALLBACK node1 -> node2 latency_ms=<number> response=<nonempty-text>
4 passed, 4 skipped, 114 deselected in <seconds>s
```

The preferred-node outage is simulated; surviving-node inference is real.
This partial check does not establish two-node acceptance.

### Both-node acceptance

```bash
python3 -m pytest tests/test_phase1_network.py --run-lan -m lan -v -s
python3 -m pytest tests/test_phase1_network.py --run-lan -q
python3 -m pip check
```

Expected: **8 passed, 114 deselected** for the live-only command, then
**122 passed, no skips or failures** for the complete suite, followed by
`No broken requirements found.` Both nodes print `HEALTH`, `MODEL`, and
`INFERENCE` lines; both fallback directions print diagnostics.

Each configured tag must be installed with Q4_K_M quantization. Direct inference
must return nonempty text on the requested node. Tests run sequentially with
16 output tokens, a 2,048-token context, and each node's configured thread budget.
Generated text need not be exactly `OK`.

The example timeouts allow 10 seconds for health/inventory and 300 seconds per
inference attempt, including cold loading. Two timed-out attempts can take about
600 seconds. Latencies are measured without an inference-speed acceptance limit.

After installing dependencies and models, repeat the complete suite with WAN
access disconnected while preserving LAN routing. Expected: the same 122 passes.

## 3. Manual HTTP checks

From Node 1, read endpoint URLs from validated settings:

```bash
NODE1_URL=$(python3 -c 'from src.config import get_settings; print(get_settings().node_url("node1"))')
NODE2_URL=$(python3 -c 'from src.config import get_settings; print(get_settings().node_url("node2"))')
for NODE_URL in "$NODE1_URL" "$NODE2_URL"; do
    curl --noproxy '*' --fail --silent --show-error --connect-timeout 5 --max-time 10 "$NODE_URL/api/version"
    curl --noproxy '*' --fail --silent --show-error --connect-timeout 5 --max-time 10 "$NODE_URL/api/tags"
done
```

Expected: version JSON and an inventory containing the configured model tag
for each node. HTTP ping verifies Ollama service availability; ICMP ping alone
does not establish that the service is listening. The live test suite above
also verifies actual generation and both fallback directions.

## 4. Troubleshooting and acceptance

| Failure | Check |
| --- | --- |
| Python resolves outside `.venv` | Activate the environment from the current checkout; recreate it if the checkout moved. |
| Node 1 connection refused | Check its Ollama service and loopback port. |
| Worker unreachable | Check routing, worker bind address, and host/router firewall rules. |
| Missing model or HTTP 404 | Compare the configured tag with that node's inventory. |
| Timeout or HTTP 503 | Inspect model loading, memory pressure, queued inference, and LAN routing. |
| Protocol error | Verify the URL points to Ollama and returns the expected JSON fields. |
| Wrong settings | Check process environment overrides and configuration aliases. |
| LAN tests skipped | Use `--run-lan` with the default `--lan-node both`. |

Collect detailed logs with:

```bash
python3 -m pytest tests/test_phase1_network.py --run-lan -v -s --tb=long
```

Acceptance requires two distinct machines, cloud-disabled servers, local Q4_K_M
models, direct health/inference on each node, both controlled fallback directions,
and **122 passing tests without skips**.

## 5. Commit message

For a Phase 1 implementation step:

```text
feat(phase1): establish local two-node Ollama configuration and health checks

Add validated LAN settings, asynchronous inference, latency metrics, and fallback.
Document generic setup and offline/live verification; exclude local configuration.
```

Before committing, run `git diff --check` and review the staged changes.
Keep `.env`, database files, local reference notes, and virtual environments untracked.

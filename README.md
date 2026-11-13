![DynaGraph Logo](./dynagraph-logo.png)

# DynaGraph

An experimental local framework for studying persistent graph memory in
multi-agent workflows. Inference and storage run on your own computers, without
cloud APIs, API keys, or paid services.

## The experiment and the framework

### The problem: remembering what still matters

In this project, an agent is a program that uses a language model to perform a
task. Several agents can take different roles, such as planning work,
investigating a question, and checking an answer. As they exchange messages, they
need access to earlier findings and decisions.

A straightforward approach includes the accumulated conversation in each new
request. That history grows over time and can contain both current and outdated
information. Repeatedly sending it may consume more prompt tokens and inference
time, while leaving the model to distinguish old facts from new ones.

DynaGraph investigates whether a persistent, selectively retrieved graph can
provide useful shared memory with less repeated context while preserving factual
consistency. The framework and experiment tools are implemented. Research has
progressed from a short A/B case study, through an offline pruning probe and
three incomplete integrated attempts, to a completed four-condition experiment.
The [experiment history below](#experiment-history-original-to-latest) links the
evidence and explains what changed at each step. Results show improvements in
some consistency measures alongside extraction costs, answer-format failures,
and loss of useful facts through pruning; they do not establish universal
efficiency or long-horizon preservation.

### How graph memory works

A graph represents named things, called **entities**, and the relationships
between them. A fact can be written as a **triple**: subject, relationship, object.
For example:

```text
Project Cedar → primary_database → SQLite
```

The finished workflow is designed to:

1. Extract structured facts from agent messages and store them locally.
2. Find facts relevant to the next task or sub-goal and retrieve a small connected
   neighborhood rather than the entire history.
3. Supply that selected memory as part of the next agent's prompt.
4. Mark stale facts as superseded when a new value replaces a single-valued fact.
5. Soft-inactivate eligible low-value memory while protecting pinned information.

If Project Cedar changes its primary database to PostgreSQL, the configured
supersession policy makes the new value active and retain the old fact's
history. This requires an explicit single-valued relationship policy: a project
can legitimately use several databases, so different objects do not automatically
indicate a contradiction.

The underlying idea is **bounded context**: choose a limited amount of relevant
memory for each request. The retrieval layer limits the graph-memory
block to 1,000 text tokens using a conservative bound or a supplied local tokenizer. A token is a unit of text processed by the model; this
limit does not cover the entire task prompt or prove that total context stays
constant. Selective forgetting considers recency, frequency, graph
connectivity, and the number of turns since information was used.

### What the experiment compares

The implemented benchmark uses the same versioned 15–20 turn scenario, including scheduled
information changes, under two conditions:

| Condition | Context strategy |
| --- | --- |
| A: linear history | Supply prior task/answer turns, dropping oldest whole turns when the shared memory allowance is exceeded. |
| B: graph memory | Extract and persist facts, then retrieve bounded relevant graph context. |

The original comparison measures prompt-token consumption, latency, and factual
consistency after updates. Models, hardware, scenario, and generation settings
should remain comparable, and interpretation must include extraction and memory
maintenance costs. Both strategies remain subject to the models' context limits;
the scenario and any history-truncation policy must be documented.

The experiment is driven programmatically by
[experiments/benchmark_harness.py](experiments/benchmark_harness.py). It defaults
to **plan only**: it does not load deployment settings, contact models, or create
experiment files. Execution requires an explicit `--execute` flag and a new output
directory. The default plan has 20 turns, two repetitions, and alternating AB/BA
condition order. Each repetition starts with an independent transcript/graph.

Both conditions use the same role schedule, current observations, questions,
output limit, 2,048-token model context, and at-most-1,000-byte conservative memory
allowance. Graph memory adds input/response extraction, governed writes, recall,
and pruning; analysis reports these costs separately from agent-only usage.
The resulting comparison is a **bounded-window case study**, not proof of
unbounded linear growth versus constant asymptotic cost. Exact-match scoring
checks a declared one-sentence answer contract against an independent oracle.

[tests/test_phase8_benchmark.py](tests/test_phase8_benchmark.py) verifies the
pipeline using simulated HTTP only. Analysis validates complete paired evidence
and checksums before calculating descriptive comparisons. Plotting exports SVG,
PDF, and PNG figures; synthetic test figures are visibly labeled. The completed
measured case study is linked above and kept separate from synthetic verification.

A separate [integrated experiment](docs/results/integrated-004/README.md) completed
all 960 model-driven turns. Supersession improved strict accuracy; pruning reduced
active edges but hurt delayed recall and did not materially reduce pipeline costs.
See the report for answer-format limitations and complete costs. Three earlier attempts
([first](docs/results/integrated-attempt-001/README.md),
[second](docs/results/integrated-attempt-002/README.md),
[third](docs/results/integrated-attempt-003/README.md)) remain excluded as partial
cohorts. Protocol 4 records bounded invalid replies, known server repetition
aborts and exhausted extraction validation as explicit outcomes. Failed
extractions accept no facts; role-answer scoring remains independent. Missing
server counters stay unknown. Ordinary application defaults still raise errors.
It compares bounded history, graph memory alone, graph with supersession, and
graph with supersession plus pruning.
The default plan uses four counterbalanced workload variants, 60 turns per
condition, and explicitly simulated memory aging: **960 scored role turns**.
It records delayed recall, pruning losses, active conflicts, cross-node consumers
and complete pipeline costs. Inspect it without contacting either node:

```bash
python3 -m experiments.integrated_harness
```

This extension preserves the original study; its offline tests do not supply
new measured findings. See the protocol before explicitly starting a new run.

### Experiment history: original to latest

This history includes completed studies, excluded attempts, and unscored
diagnostics. **Only complete measured cohorts contribute to their respective
condition comparisons.** Every full restart used fresh state; partial rows were
never pooled with later runs. Offline tests and simulated aging are identified
separately from measured model inference.

| Order | Study or diagnostic | Scope and status | Finding and subsequent change |
| --- | --- | --- | --- |
| 1 | [Original A/B attempt, scenario v1](docs/results/study-20261111-002/README.md#preserved-unsuccessful-attempt) | **Excluded:** 30 saved turns; stopped during graph run B1 turn 11. | Extraction grounding validation failed. A later isolated diagnostic produced a malformed copied placeholder; the original failed reply was not retained. Scenario v2 replaced literal placeholders with plain-language instructions and improved prospective failure evidence. |
| 2 | [Completed original A/B case study](docs/results/study-20261111-002/README.md) | **Completed:** 80 scored turns; two paired repetitions, 20 turns per condition per repetition. | Graph memory scored 40/40 versus bounded history's 26/40. It used 14.24% fewer role prompt tokens but 2.66 times the pipeline prompt tokens. No triples were pruned, motivating a separate pruning check. |
| 3 | [Deterministic pruning probe](docs/results/pruning-probe-001/README.md) | **Completed offline mechanism test:** two executions per policy, simulated age through 96 hours, no model calls; 12 checks passed. | Pruning retired two of seven triples, including a fact needed later. Frequently consumed and pinned facts survived; an unused cycle remained protected by leaf-only eligibility. Final required-fact availability was 2/3 with pruning versus 3/3 without. This measured graph availability, not answer accuracy. |
| 4 | [First integrated attempt](docs/results/integrated-attempt-001/README.md) | **Excluded:** 178/960 saved turns; stopped at D1 turn 59. Scenario v1, container protocol 2. | A cycle question produced repeated text and truncated JSON on both permitted attempts. Scenario v2 narrowed instructions to direct facts about the requested subject and predicate, retaining observations, oracle, models, policies and output cap. |
| 5 | [Post-failure prompt diagnostics](docs/results/integrated-attempt-001/README.md#prompt-revision-and-unscored-checks) | **Unscored:** three local generation checks using revised instructions. | All returned complete JSON, but none met the strict answer contract. These checks did not establish accuracy or reliable completion and were not added to any cohort. |
| 6 | [Second integrated attempt](docs/results/integrated-attempt-002/README.md) | **Excluded:** 238/960 saved turns; stopped at C1 turn 59. Scenario v2, container protocol 2. | The cycle question again exhausted the output cap. Protocol 3 introduced explicit rejected-reply outcomes: preserve attempts and costs, score the turn incorrect, and continue the schedule after bounded reply validation fails. |
| 7 | [Third integrated attempt](docs/results/integrated-attempt-003/README.md) | **Excluded:** 251/960 saved turns; stopped at B2 turn 12. Scenario v2, container protocol 3. | Response extraction encountered an HTTP 500 token-repetition abort. Protocol 4 added explicit known-abort and exhausted-extraction outcomes across applicable stages; failed extraction accepts no facts, and unreturned counters remain unknown. |
| 8 | [Completed integrated experiment](docs/results/integrated-004/README.md) | **Completed:** 960 scored turns; four conditions, four counterbalanced workload variants, 60 turns per condition per variant. Scenario v2, container protocol 4. | Supersession improved strict accuracy and eliminated active single-valued conflicts. Pruning reduced active graph size but harmed delayed recall, with almost no pipeline prompt-token reduction relative to supersession alone. |

The integrated experiment separates the memory policies that the original A/B
study evaluated together. Its condition labels apply within this study: original
**B** was governed graph memory, whereas integrated **B** disables supersession
and pruning. The different scenarios and protocols also prevent treating the
original and integrated accuracy figures as a single performance trend.

| Latest integrated condition | Strict correct answers | Accuracy |
| --- | ---: | ---: |
| A: bounded linear history | 4/240 | 1.67% |
| B: graph without supersession or pruning | 76/240 | 31.67% |
| C: graph with supersession, pruning off | 112/240 | 46.67% |
| D: graph with supersession and pruning | 102/240 | 42.50% |

Supersession improved C over B by **15 percentage points**, but generated
incorrect assertions could still supersede correct source facts. The invariant
of one active value is distinct from semantic correctness. Pruning reduced D's
mean final active edges by **26.9%** relative to C, while dedicated delayed
probes fell from **8/8 correct to 0/8**. D used only **0.15% fewer pipeline prompt
tokens** than C. Compared with A, D used **20.7% fewer role prompt tokens** but
**124.8% more pipeline prompt tokens**, including extraction and corrections.

Interpret these results with the recorded limitations. A's poor strict score
includes answer-format and instruction-following failures that occurred before
history truncation, so its errors cannot all be attributed to forgetting. Six
rejected role replies remain incorrect outcomes in the complete cohort. Three
server aborts left B's pipeline token total unknown, with complete counters on
237/240 turns; no counters were zero-filled. Memory aging was simulated at two
hours per turn, while inference and latency were measured. Both history and
graph context were capped. The four variants support descriptive comparisons,
not asymptotic proofs or general preservation guarantees. The two-node inference
setup used centralized storage; replicated state and network partitions were
not evaluated.

The [latest report](docs/results/integrated-004/README.md) provides the complete
bundle, answer audit, checksums, six figure sets and commands to reproduce the
analysis without contacting models. Earlier reports preserve the evidence and
limitations of their own protocols, including historical verification counts.
Together, the studies document a consistency, cost and retention tradeoff;
they do not establish every claim of the motivating thesis.

The [post-experiment refactoring notes](docs/refactoring.md) distinguish the
measured source revisions from later readability maintenance and document
the baseline checks that protect published evidence and analysis.

### Potential benefits and limitations

| Potential benefit | Limitation or tradeoff |
| --- | --- |
| Reduce repeatedly supplied history. | Extraction, retrieval, and graph maintenance add work; total latency may not improve. |
| Preserve shared facts across turns and runs. | Extraction can omit or misinterpret information; valid JSON does not establish truth or completeness. |
| Keep current values distinct from superseded ones. | Relationship policies must distinguish genuine replacements from legitimate multiple values. |
| Select a small relevant memory neighborhood. | Lexical matching and pruning can miss useful information. |
| Operate with local models and local storage. | Available RAM and CPU throughput constrain model choice, concurrency, and response time. |

Triples preserve selected facts, not complete source documents or exact wording.
Applications that need quotations, detailed evidence, or audit trails also need
the original material and appropriate provenance. The experiments assess
this design on controlled scenarios; broader application suitability requires
additional evaluation.

### Suitable candidate workloads

The intended fit is multi-step work with relationships and facts that change:

| Workload | Memory that could be useful |
| --- | --- |
| Local IT troubleshooting | Hosts, service dependencies, attempted fixes, and current findings. |
| Investigation support | Relationships among evidence, observations, and evolving hypotheses. |
| Research over supplied notes or documents | Findings, connections, corrections, and unresolved questions. |
| Project planning | Dependencies, assignments, constraints, and revised decisions. |

These are candidate uses for the target framework, not validated application
features. Document ingestion, log collection, and task-specific tools require
application adapters. Short, independent questions offer less opportunity to
benefit from persistent graph memory.

### Current capabilities and extensibility

The framework includes local inference, transactional graph storage, validated
fact extraction, replacement and pruning policies, bounded recall, sequential
agent roles, durable checkpoints, telemetry and reproducible experiment analysis.
The [experiment history](#experiment-history-original-to-latest) records the
completed studies and unsuccessful attempts separately.

The programmatic workflow now connects extraction, storage, governance, and
retrieval. The standalone APIs remain available; calling extraction alone does
not write memory. Start a task with `StateMachine.start()` or `run()`; an
optional terminal dashboard can also start or resume tasks.

Current configuration supports selecting installed model tags, endpoints, and
CPU budgets for the two Ollama nodes. Python callers can provide their own messages
and use the graph APIs in a larger application. Supplying a different `agent_id`
labels a message; it does not create or schedule an agent.

The shared `BaseAgent` contract supports additional roles through a concise
role prompt, registered local read-only tools, and ordered registration in the
workflow controller.
A new role can reuse an existing model and computer. New tools, relationship
policies, or different permissions require additional implementation and tests;
automatic plugin discovery is not currently provided. More agent turns also
increase inference time on the target hardware. Additional worker nodes would
require configuration and routing changes beyond the current two-node baseline.

Explore the [interactive architecture atlas](docs/architecture.html) or read the
[complete architecture](docs/architecture.md) for every specified component and
its connections. The offline atlas supports pan/zoom, search, phase focus,
light/dark themes, and SVG export, with source components and local artifacts labeled.
Open the HTML file directly in a browser; viewing the atlas needs no Ollama server.

## Getting started with DynaGraph

Choose a starting point:

| Your goal | Start here |
| --- | --- |
| Understand the evidence | Read the [experiment history](#experiment-history-original-to-latest) and linked reports. No installation is needed to read them. |
| Check saved results without running models | Install Python dependencies below, then run `python3 docs/testing/verify_refactor_baseline.py`. It checks published evidence and reproduces both completed study summaries without writing results. |
| Preview the interface without models | After installing dependencies, use the synthetic dashboard demo in Step 4. |
| Run your own local task | Configure both nodes, then follow Step 5. |
| Build on the framework | Use the [API examples](docs/usage.md) and [component architecture](docs/architecture.md). |

### 1. Check the hardware and software requirements

The supported baseline uses two computers on a private local network (LAN):

| Computer | What runs there |
| --- | --- |
| Node 1: coordinator | Python 3.11+, DynaGraph, local SQLite storage, and Ollama. All Python coordination runs here. |
| Node 2: worker | Ollama with a locally installed model, reached over the LAN. Node 2 does not need the Python project or access to the database file. |

Each computer should have an Intel N100 or better and 8–16 GB RAM. Choose
quantized models that fit the available memory; quantization reduces the memory
needed to store model weights. Python's SQLite build must be version 3.38+ with
FTS5 text-search support and JSON functions.

Initial dependency and model downloads need Internet access. Once installed,
operation can use only the local network. Keep the database on Node 1's local
disk; the worker communicates through HTTP rather than opening a network-shared
SQLite file.

### 2. Install the Python dependencies

Clone or download the repository and open a terminal in its root folder on Node 1.
The commands below use Linux and Bash. Create a virtual environment to keep the
project's Python packages separate from other software:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip check
cp --no-clobber .env.example .env
```

Expected: dependencies install and `pip check` reports
`No broken requirements found.` The copy command preserves an existing `.env`.
Activate `.venv` again when opening a new terminal for project commands.

### 3. Configure the local models and network

The `.env` file holds settings for your installation and is ignored by Git.
Edit it before running network checks: replace the illustrative LAN addresses
with your nodes' actual private addresses and select exact locally installed
model tags. A tag identifies a particular model/version in Ollama.

| Setting | Purpose |
| --- | --- |
| `COORDINATOR_HOST` | Node 1's private address; this setting does not start a server or change Ollama's listening address. |
| `NODE1_OLLAMA_URL` | Node 1's Ollama endpoint, normally `http://localhost:11434`. |
| `NODE2_OLLAMA_URL` | Node 2's private LAN endpoint, including its Ollama port. |
| `NODE1_MODEL`, `NODE2_MODEL` | Exact local model tags for each node. |
| `DATABASE_PATH` | Local SQLite file owned by Node 1. Use a separate file for practice tasks. |
| `SINGLE_VALUED_PREDICATES` | Relationships with one current value per subject; defaults to `[]`. |
| `CONTEXT_TOKEN_BUDGET` | Graph-context limit, from 1 to 1,000. |

The [example configuration](.env.example) lists CPU, request, retrieval,
retention and workflow limits. The [API guide](docs/usage.md) explains when
these settings matter.

Follow the [Phase 1 setup and verification guide](docs/testing/phase1.md) to
install/configure Ollama on both nodes, disable cloud features, install local
models, and permit Node 1 to reach the worker. Editing the project's `.env` does
not configure or restart an Ollama service. Live acceptance tests require the
configured model tags to have Q4_K_M quantization.

Validate configuration without contacting either server or creating a database:

```bash
python3 -c 'from src.config import get_settings; get_settings(); print("CONFIG VALID")'
```

Expected: `CONFIG VALID`. Settings load the root `.env` regardless of the working
directory. Process environment values override dotenv values. Legacy aliases
`NODE1_HOST_IP`, `NODE1_OLLAMA_MODEL`, and `NODE2_OLLAMA_MODEL` are accepted; use
one name per setting. SQLite WAL mode is required for concurrent local storage.

### 4. Preview the dashboard offline

This example needs Python dependencies, but no `.env`, Ollama or database:

```bash
python3 -m src.telemetry.dashboard --demo --plain --csv .verification/demo.csv
```

Expected: `SIMULATED DEMO OK: 3 turns; no model requests or database writes`,
plus a CSV path. These synthetic numbers illustrate the display; they are not
research measurements. Omit `--plain` to use the interactive terminal view.

### 5. Run a task with your local models

After completing the network setup, choose a separate writable `DATABASE_PATH`
in `.env` for practice tasks. This command writes graph state and checkpoints
there and makes inference requests to your configured nodes:

```bash
python3 -m src.telemetry.dashboard --task "Repeat this fact exactly: Project Cedar uses SQLite." --subgoal "Project Cedar" --turns 3
```

One cycle runs planner, researcher and verifier roles in sequence. Each turn
recalls memory, generates a response, extracts facts and commits accepted updates.
The display reports usage, latency and graph size; completing the cycle does not
prove the response is correct. Press **p** to pause/resume the display and **q**
or **Ctrl+C** to stop execution. Keep the printed `RUN_ID` to resume later:

```bash
python3 -m src.telemetry.dashboard --resume RUN_ID
```

The [usage guide](docs/usage.md#6-watch-execution-and-export-measurements) covers
CSV export, recovery and measurement boundaries. It also provides the longer
extraction, storage, governance, recall and programmatic role-cycle examples.

### 6. Inspect the experiment plan without running it

```bash
python3 -m experiments.benchmark_harness
```

Expected:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
```

The command also prints its scenario hash and plan. Do not add `--execute` during
implementation verification. The [Phase 8 protocol](docs/testing/phase8.md)
explains synthetic tests, comparison boundaries, artifact integrity, and explicit
execution. Existing application data and the private `.env` are preserved.
Matplotlib is pinned for local, headless figure export; no plotting server or
cloud account is needed.

### 7. Verify the installed components

Run the functional suite without network access or Ollama:

```bash
python3 -m pytest -q
```

Expected: **849 passed, 18 skipped**. The skipped checks require physical LAN
nodes or explicitly requested storage/retrieval timing measurements. The guides
below provide their exact commands and acceptance criteria. Ordinary offline
verification makes no model requests.

| Guide | What it explains |
| --- | --- |
| [Network setup and verification](docs/testing/phase1.md) | Ollama services, installed models, HTTP health checks, inference, and controlled fallback. |
| [Storage verification](docs/testing/phase2.md) | SQLite capabilities, transactions, graph operations, prefix search, and the insert benchmark. |
| [Extraction verification](docs/testing/phase3.md) | Defensive parsing, live model checks, resource limits, and validation errors. |
| [Governance verification](docs/testing/phase4.md) | Deterministic decay, atomic replacement, pinned memory, daemon lifecycle, migration, and a reproducible offline smoke check. |
| [Retrieval verification](docs/testing/phase5.md) | Active one/two-hop recall, strict block budgets, explicit usage, snapshot consistency, and the warm retrieval benchmark. |
| [Orchestration verification](docs/testing/phase6.md) | Three-role LAN cycles, local tools, atomic checkpoints, restart/cancellation, migration, and complete workflow usage. |
| [Telemetry verification](docs/testing/phase7.md) | Measured usage, checkpoint snapshots, live controls, CSV integrity/recovery, and a direct two-node telemetry check. |
| [Experiment implementation verification](docs/testing/phase8.md) | Synthetic-only A/B pipeline checks, plan-only CLI, oracle/scoring, artifact validation, paired analysis, and standalone figures. No experiment run. |
| [Integrated experiment verification](docs/testing/phase8_integrated.md) | Synthetic-only four-condition checks, consistent simulated aging, delayed recall, transition/checkpoint validation and six figure sets. Model-driven execution is gated. |
| [Architecture atlas verification](docs/testing/architecture.md) | Optional checks for the documentation's themes, controls, SVG export, and connector geometry. |

### 8. Troubleshoot and build on the APIs

| Symptom | First check |
| --- | --- |
| Connection refused or worker unreachable | Confirm Ollama is running, the endpoint is correct, and routing/firewall rules permit Node 1 to reach Node 2. |
| Configured model not found | Compare the exact `.env` tag with that server's installed model inventory. |
| Slow inference or timeouts | Check model loading, RAM pressure, CPU load, and request budgets. |
| Wrong setting used | Check process environment overrides and legacy aliases. |
| Storage configuration failure | Use a writable local database path and verify SQLite version, FTS5, and JSON support. |
| Invalid extraction | Inspect the error and original message; use the dedicated extraction protocol for detailed logs. |

The client's `ping()` measures an HTTP health request, not an ICMP network ping.
Inference timing includes model loading and generation. The default timeout is
300 seconds per attempted node; two timed-out fallback attempts can take about
600 seconds. Extraction can make a second corrective generation, so consult its
guide for the complete retry budget.

For application development, start with [configuration](src/config.py),
the [Ollama client](src/network/ollama_client.py),
the [extractor](src/extraction/extractor.py), and
the [graph store](src/storage/graph_store.py). Keep their resource lifecycles
explicit: callers own HTTP-client and database cleanup. The
[architecture narrative](docs/architecture.md) explains the implemented integration and extension points. The
[initial measured case study](docs/results/study-20261111-002/README.md) includes results,
limitations, and commands to reproduce the analysis without running the models.

# Sizing a machine

The LLM runs at the provider, so no GPU is needed. Task containers are light: trials spend most of their time waiting on the model.

## Per-trial reservations

Each task's `task.toml` `[environment]` reserves resources. These are reservations, not typical use, but an agent's own builds and tests can briefly use them in full.

| task category | vCPU | RAM | storage |
| --- | --: | --: | --: |
| issue-fixing, single container | 2 | 3 GB | 12 GB |
| issue-fixing with engines | 4 | 8 GB | 20 GB |
| program-construction with engines | 4 | 8 GB | 24 GB |
| program-construction, no engines | 2 | 4 GB | 12 GB |

Keep about 2 vCPU and 4 GB RAM for Harbor and Docker themselves. Each task's `task.toml` `[environment]` is authoritative.

## Rule of thumb

For single-container issue-fixing tasks and program-construction tasks without engines. Tasks with engines reserve about twice the CPU and RAM per trial.

| concurrent trials | vCPU | RAM | free Docker disk |
| --: | --: | --: | --: |
| 2 | 6 | 12 GB | 50 GB |
| 4 | 10 | 20 GB | 75 GB |
| 8 | 18 | 36 GB | 120 GB |
| 16 | 34 | 68 GB | 200 GB |

## Notes

- **Host architecture: x86-64 (amd64) Linux.** The program-construction reference programs are x86-64 Linux executables, and the task images download x86-64 builds of Go, Rust and `crane`, so those images neither build nor run natively on an Arm host. The benchmark is verified on x86-64 only; amd64 emulation on Arm is untested and would slow the timing-sensitive tests ([EVALUATION_PROTOCOL.md](EVALUATION_PROTOCOL.md) §4.10).
- **Counting trials:** concurrent trials = `--n-concurrent` × the number of `run` jobs running at once.
- **Docker network pools cap concurrency,** independently of CPU and RAM. The default pools give about 30 bridge networks per host. Each trial uses one network, and two while a separate verifier runs. Above about 25 concurrent trials, add pools in `/etc/docker/daemon.json` (see [EVALUATION_PROTOCOL.md](EVALUATION_PROTOCOL.md) §4.7).
- **Wall time** is set by LLM latency and the task budgets (1 h per issue-fixing task, 4 h per program-construction task). More hardware helps only together with higher concurrency, and provider quotas (tokens and requests per minute) cap that.
- **Disk:**
  - **Docker data root:** keep it on local disk, not a network filesystem. Each trial's sandbox is removed when it ends; build layers are cached and shared across tasks.
  - **Job folders:** take 1–5 MB per trial, and up to about 70 MB for long trials.
- **Network:** the host needs outbound HTTPS to the LLM provider, and, while task images build, to Docker Hub, the Debian and PyPI mirrors, `github.com` (the AWS SDK for C++ and the `crane` tool), `go.dev` and `proxy.golang.org` (Go), and `static.rust-lang.org`, `index.crates.io` and `static.crates.io` (Rust). Sandboxes themselves have no network access.

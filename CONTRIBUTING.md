# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary information to effectively respond to your bug report or contribution.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass (`uv run --locked pytest`). SWE-Cloud runs from a checkout with [uv](https://docs.astral.sh/uv/); it is not published on PyPI, and installing it with pip is not supported (the dependency pins hold only with `uv --locked`). Two checks need the Git LFS objects (`git lfs pull`) and are skipped in CI: run them locally when you change a program-construction task.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional documentation on [forking a repository](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/fork-a-repo) and [creating a pull request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request).

## Contributing tasks

The task sets live in this repository, in `tasks/issue-fixing/` and `tasks/program-construction/`, one folder per task ([tasks/README.md](tasks/README.md)). To report a problem with a task, open an issue with the task name (for example `swe-cloud/if-...`), your host (CPU, memory, Docker version) and, if you can, the job folder of the run that shows it.

**For the 0.1.0 release, please open an issue rather than a pull request to propose a new or changed task.** Task pull requests will be accepted once the release checks can run automatically. A task proposal follows the format in [docs/TASKS.md](docs/TASKS.md) and passes its release checks. In particular:

* the reference solution passes the oracle run three times on your host, at the concurrency you evaluate with (`swecloud run --preset oracle --n-attempts 3 ...`); say which host in the pull request;
* a run with nothing done scores 0 (`swecloud run --agent nop --n-attempts 1 ...`);
* `uv run --locked pytest` passes (it checks the task layout, network policy, sandbox and file modes);
* the task's `[agent]` and `[verifier]` use `network_mode = "no-network"`, and any network between its services is internal;
* names are neutral;
* no credentials or internal data are included;
* any third-party code keeps its license and is credited in the task's `ATTRIBUTION.md`;
* `task.toml` records the task's source in `[metadata.source]`, and `tasks/SOURCES.md` is regenerated from it (`uv run --locked python -m swecloud.sources`);
* a pull request that adds or removes tasks updates the split's `tasks` count in `tasks/manifest.toml`, and the counts in `README.md`, `tasks/README.md` and `CHANGELOG.md` (`uv run --locked pytest` checks they agree);
* scripts and programs carry their executable bit, and program-construction reference programs are committed through Git LFS (`.gitattributes` does this for `environment/reference/program` and `solution/reference/program`).

Changing a task changes what its scores mean, so describe the change and why in the pull request.

Changes to `protocol.toml` change the evaluation protocol and make new scores incomparable with earlier ones. Please discuss them in an issue first.

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue. See [SECURITY.md](SECURITY.md).

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

# SWE-Cloud front page

A static page: one HTML file, one stylesheet, one script and a few JSON data files. No build step.

```text
index.html             front page
tasks.html             task gallery (60 issue-fixing, 40 program-construction)
css/site.css           styles (AWS console theme)
js/site.js             renders every section from the data files
js/gallery.js          renders the task gallery from the task folders
css/gallery.css        gallery styles
data/leaderboard.json  models, scores, costs, effort, Hugging Face links
data/coverage.json     task categories and AWS services (task counts)
data/faq.json          the questions shown on the front page
data/citation.json     BibTeX and plain-text citation
data/site.json         section order, design variants, and tasks_base (where the gallery reads the tasks)
assets/logo.svg        logo, also used as the favicon
```

## Preview locally

The pages load their data with `fetch`, which browsers block for `file://` pages. Serve the repository root (the gallery also reads `../tasks/`):

```bash
python3 -m http.server 8000     # then open http://localhost:8000/site/
```

Any static host works, as long as it serves `tasks/` next to `site/` or `tasks_base` points at it.

## Task gallery

`tasks.html` bundles no task content. It reads the task list from `../tasks/SOURCES.md`, and each card and task page from that task's own `README.md` and `task-diagram.svg` in `../tasks/issue-fixing/<task>/` or `../tasks/program-construction/<task>/`, when the page is viewed. So it expects this folder to sit at `site/` in the repository, next to `tasks/`, and the repository root to be served:

```bash
python3 -m http.server 8000     # in the repository root; open http://localhost:8000/site/
```

To host the site elsewhere, set `tasks_base` in `data/site.json` to a URL that serves the `tasks/` folder (for example the repository's raw-file URL). Links: `tasks.html#if`, `tasks.html#pc`, and `tasks.html#<task-id>` for one task.

## Update the leaderboard

Edit `data/leaderboard.json`. Scores are fractions from 0 to 1; costs are average API cost in USD per task.

- Per model: `issue_fixing` has `pass@1`, `pass@3` and `cost_usd`; `program_construction` adds `almost@1` (share of attempts that pass at least 95% of the hidden tests).
- The page computes the rest: SWE-Cloud Score = `score.w_if` x issue-fixing pass@1 + `score.w_pc` x program-construction pass@1, and the overall cost per task, weighted by `tasks`.
- `weights` is `open` or `closed`; `weights_url` makes the "Open weights" badge a link.
- `effort` is shown next to the model name; `harness` is the harness column.
- Set `placeholder` to `false` once the numbers are real; the note above the table then shows `updated` instead.

Rows are sorted by the active tab's main metric, so their order in the file does not matter.

## Other content

- `data/faq.json`: front-page questions; the full list is on `/faq/`.
- `data/coverage.json`: counts per category and per AWS service.
- `data/site.json`: `order` lists the sections between the fixed header and the citation; `variants` picks A, B or C per section, as in the mockup.

Section texts that rarely change (the header, the task storyboards, the sources, the run commands) are in `js/site.js`. The run commands were checked against the release repository at commit `db5c4c1`.

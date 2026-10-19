# Contextopus Session Search

![Contextopus](docs/media/contextopus-wide-1200.svg)

Find a previous Codex decision, inspect the original messages and resume the
right session. Session Search adds conversation search to
[Contextopus Core](https://github.com/pikiby/contextopus-core).

- Search by meaning or exact text, with links and byte offsets to source JSONL.
- Select active or archived conversations and filter by session type, project,
  dates, model or size.
- Reuse one Core embedding model and retrieval store across modules.
- Preview archive, restore or delete actions before applying an exact plan.

Indexing leaves source transcripts unchanged. Session merging is not implemented.

## Install

Requires **Core 1.5 or newer (below 2.0)** and Python 3.12+. Install Core first
using its [starter bundle](https://github.com/pikiby/contextopus-core/releases).

Download `session-search-0.8.0.zip` from
[Session Search releases](https://github.com/pikiby/contextopus-session-search/releases)
and extract it. Open a terminal in the extracted `session_search` folder:

```bash
./install.sh --root "/path/to/existing/Contextopus"
```

On Windows PowerShell:

```powershell
.\install.ps1 -Root "C:\path\Contextopus"
```

If Core already has a bound data root, omit `--root` / `-Root`. Add `--plan` /
`-Plan` to inspect without installing. The installers delegate to Core's common
module lifecycle. A direct folder or ZIP install is equivalent:

```bash
contextopus --root "/path/to/existing/Contextopus" modules install session_search --source "/path/to/session-search-0.8.0.zip"
```

An existing module is updated through `modules update`; source installation
refuses to overwrite it. [Configuration, updates and operation reference](docs/module.md).

## Start searching

Connect your agent to Core's shared MCP. Configure `session_search.configure`
with an existing `codex_home`, then call `session_search.start`. Check
`session_search.status` for progress and use `session_search.search` with a
query and `mode="semantic"` or `"literal"`.

Main active sessions are selected by default. Archive and subagent selection is
explicit. The terminal interface opens from Core's module menu and shows
indexing progress, embedding speed and recent search metrics. Read later messages
before treating a found task as unfinished.

[Contextopus Workspace](https://github.com/pikiby/contextopus-workspace) is an
optional companion for structured project knowledge; Session Search also works
without it.

## Build and develop

```bash
python3 scripts/build-module.py /tmp/session-search-build/session_search
python3 scripts/build-module.py --zip /tmp/session-search-build/session-search-0.8.0.zip
```

Builds contain the module, SQL migrations, installers, guide and license files;
no transcripts, indexes or model weights. Outputs are never overwritten.
[Development and tests](docs/development.md) ·
[Bounded standalone measurements](docs/block-comparison.md).

The independent stdio MCP/indexer and `.deb` remain a secondary compatibility
path with their own offline model. [Standalone guide](docs/standalone.md).
The module uses Core's model and lifecycle. Native platform checks are defined
in CI; local verification uses Linux fixtures, and the standalone target is
Linux Mint 22.3.

## License

Apache-2.0. See [LICENSE](LICENSE) and retained attribution in [NOTICE](NOTICE).

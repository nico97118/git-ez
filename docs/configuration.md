# Configuration and providers

[README](../README.md) · [Command manual](man/git-ez.1)

All settings use `git config`. For an OpenAI-compatible Chat Completions API
(vLLM, LM Studio, a hosted service, or a proxy):

```bash
git config --global ez.provider openai
git config --global ez.model exact-model-id
git config --global ez.endpoint https://ai-server.example/v1

# If the server uses an internal CA or requires a token:
git config --global ez.caBundle /absolute/path/to/internal-ca.pem
git config --global ez.apiKeyEnv INTERNAL_AI_TOKEN
```

The selected environment variable supplies the token. `ez.apiKey` also accepts a direct
token and takes precedence over that variable. Git stores it as plaintext in its
configuration file; prefer environment variables for secrets. The token is redacted
from archived configuration and removed from the agent shell environment.

Each key follows native Git precedence: **`git -c` > worktree/local > global > system**.
Git's `include` and `includeIf` mechanisms remain available. Setting names are
case-insensitive. To override a single setting:

```bash
git config --local ez.model project-model
git -c ez.model=another-model ez rebase main
```

See the [manual](man/git-ez.1) CONFIGURATION section for every supported key
and its default, including context, tool-call, timeout, and capture limits.

Booleans and integers use Git formats (`yes`, `false`, `256k`, etc.). `ez.caBundle`
paths support `~`. Relative paths resolve from the repository root, or from the current
directory outside a repository; use an absolute path in global configuration.
No TOML or `.env` files are loaded.

A higher-priority scope replaces `ez.requestOptions` in full. For example, if supported
by the server and model:

```bash
git config --global ez.requestOptions '{"chat_template_kwargs":{"enable_thinking":false}}'
```

See [examples/ez.gitconfig](../examples/ez.gitconfig) for the standard Git configuration format.
Model names, thinking options, and tool parsers must match the actual server configuration.

## Connectors

The `openai` provider explicitly uses Chat Completions and native tool calls, without
Responses or automatic fallback to another server. The endpoint accepts either a base
URL (`…/v1`) or a full URL (`…/v1/chat/completions`).

For native Ollama:

```bash
git config --global ez.provider ollama
git config --global ez.model installed-model:tag
git config --global ez.endpoint http://localhost:11434
```

The model must support tools. The `options` and `think` fields in `ez.requestOptions`
configure Ollama generation and reasoning; unknown options are rejected.
HTTPS is required outside loopback unless `ez.allowInsecureHttp=true`. TLS verification
remains enabled, using the configured CA bundle when provided. Redirects are not followed.

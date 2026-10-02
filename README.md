# lanagent-hermes

The [LANAgent API](https://api.lanagent.net) for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
scraping (basic, stealth, full and real-browser rendering with screenshots), a web archive, video and
audio downloads, transcription, image generation, a code sandbox and about a hundred more paid services,
priced in credits.

The API is already an MCP server, so Hermes could use it with MCP alone. This plugin adds what MCP
cannot do by itself:

- **Two-command setup.** Sign in with a code emailed to you, or use an existing key. Setup creates a key
  for this Hermes and connects the MCP server for you.
- **Fewer tools in context.** It connects in discovery mode: 8 tools instead of about 100 tool schemas
  on every turn. `search_tools`, `describe_tool` and `call_tool` reach every service.
- **Jobs and files.** Downloads run as jobs. `lanagent_wait_job` waits for one to finish and saves its
  file locally, so there is no polling turn after turn and no URL handling. `lanagent_fetch_file` saves
  any other result link, such as a generated image.
- **Spend at a glance.** `lanagent_spend` and the `/lanagent` command show your balance, today's spend
  against your daily limit, and what this session has cost.

## Install

```
hermes plugins install PortableDiag/lanagent-hermes --enable
hermes lanagent setup
```

`setup` asks for your email and the 6-digit code it sends. A new email gets a free account. It then
creates an API key named "Hermes: <host>" for this Hermes, saves it in Hermes' `.env` as
`LANAGENT_API_KEY`, and adds the MCP server `lanagent` to `config.yaml`.

Options:
- `--key gsk_…` uses an existing key instead of signing in.
- `--daily-limit N` sets a credit limit per UTC day for the key. The server enforces it for every
  client.
- `--full-tools` lists every tool instead of discovery mode.

Restart Hermes, then check the connection with `hermes lanagent status`.

## Tools

| Tool | What it does | Cost |
|---|---|---|
| `lanagent_wait_job` | Wait for a `social_download` / `social_audio` job and save its files locally | free |
| `lanagent_fetch_file` | Save a result link (image, download) to a local file | free |
| `lanagent_spend` | Balance, today's spend and limit, this session's spend | free |
| MCP `search_tools` / `describe_tool` / `call_tool` | Find, inspect and run any of the API's services | the service's price |

Every paid result states what it was charged. Prices are listed at
[api.lanagent.net/catalog](https://api.lanagent.net/catalog). Files are saved under
`~/.hermes/lanagent-files/`.

## Privacy and keys

The key stays in Hermes' `.env`, with the file mode set to 600. Revoke it in your
[dashboard](https://api.lanagent.net) to disconnect. Scraped content is marked as untrusted data in
every result.

## Develop

```
python3 -m unittest discover -s tests
```

Standard library only; no dependencies.

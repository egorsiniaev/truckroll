# Contributing

This is a personal learning project, but issues and pull requests are welcome, especially on OCPP behaviour,
incident rules and the agent eval.

- Run the tests and lint for the service you touched: `uv run pytest` and `uvx ruff check src tests` in `csms/` or
  `control/`. CI runs the same.
- Keep secrets out of the repo; keys come from environment variables (see `.env.example`).
- Low voltage only. Nothing in this project may connect to mains or a real EV.
- Found a bug in the simulator or the OCPP library? An upstream issue helps more than a workaround here.

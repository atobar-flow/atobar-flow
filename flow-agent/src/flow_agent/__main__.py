"""CLI entrypoint for running role sandbox instances via python -m flow_agent."""

import asyncio

from flow_agent.main import main

if __name__ == "__main__":
    asyncio.run(main())

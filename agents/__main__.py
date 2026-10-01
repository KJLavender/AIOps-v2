"""Entrypoint: `python -m agents <monitor|diagnose|repair|validate>`."""
from __future__ import annotations

import logging
import sys

from aiops import crd
from aiops.config import Config
from aiops.judge import Judge
from aiops.kube import KubeClient
from aiops.llm import NullAnalyzer, OllamaAnalyzer
from aiops.metrics import Loop, serve
from aiops.websearch import WebSearch

AGENTS = ("monitor", "diagnose", "repair", "validate")


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in AGENTS:
        print(f"usage: python -m agents <{'|'.join(AGENTS)}>", file=sys.stderr)
        return 2
    name = argv[0]
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format=f"%(asctime)s %(levelname)-7s {name}: %(message)s")
    config = Config.from_env()
    kube = KubeClient(config)
    store = crd.Store(kube, config.system_namespace)
    loop = Loop(name, interval=config.poll_interval_seconds)
    routes = {}

    if name == "monitor":
        from .console import console_routes
        from .monitor import Monitor
        agent = Monitor(config, kube, store)
        routes = console_routes(store)
    elif name == "diagnose":
        from .diagnose import Decider, DiagnoseAgent
        llm = OllamaAnalyzer(config) if config.llm_enabled else NullAnalyzer()
        agent = DiagnoseAgent(config, store, Decider(config, llm, WebSearch(config), Judge(config)))
    elif name == "repair":
        from .repair import Repairer
        agent = Repairer(config, kube, store)
    else:
        from .validate import ValidateAgent
        agent = ValidateAgent(config, kube, store)

    serve(config.agent_port, name, routes, healthy=loop.healthy)
    logging.getLogger("agents").info("%s agent started (system namespace %s)", name, config.system_namespace)
    loop.run_forever(agent.step)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

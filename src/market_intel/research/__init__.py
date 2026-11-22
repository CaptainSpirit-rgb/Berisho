"""Research desk (Stage 1 of docs/MULTI_AGENT_BLUEPRINT.md).

Python does everything deterministic: picks the tickers, builds one research packet per
ticker (every number the agents may quote, each with a source id), validates what the
agents write and delivers it. The agents themselves are Claude Code subagents
(``.claude/agents/``) run headless on the PC under the Claude subscription, so the run is
free; see ``runner.py``.
"""

import json
import traceback

from agents.state import AgentState
from agents.llm_client import get_llm
from performance.engine import PerformanceTracker

from agentlens.sdk import trace
from agentlens.logging_config import get_logger

logger = get_logger(__name__)


@trace(name="edge_cases_agent")
def edge_cases_agent(state: AgentState) -> AgentState:

    tracker = PerformanceTracker(label="edge_cases_agent")
    tracker.start()

    try:
        llm = get_llm()

        print("[Edge Cases Agent] Running...")

        task_plan = state.get("task_plan", [])

        LIVE_EDGE_CASE_PROMPT = f"""
What edge cases are unique to a live IoT dashboard?

- What if a sensor goes offline? (widget shows '--' or 'N/A')
- What if data hasn't refreshed yet? (stale timestamp)
- What if websocket disconnects? (fallback UI)
- What if a chart has no data? (empty state)
- What if a user navigates during a live refresh?

You are a QA Automation Expert for Live IoT dashboards.

Task Plan:
{chr(10).join(task_plan)}

Architecture:
{state.get("architecture_notes", "")}

Target URL:
{state.get("target_url", "")}

Generate exactly 6 execution-focused edge cases.

Focus on:

1. Sensor offline / null data
2. Network interruption during widget load
3. Stale timestamp / outdated data
4. Empty chart / empty table
5. Rapid navigation between dashboard pages
6. Widget rendering during live refresh

Rules

Return ONLY valid JSON.

Do NOT wrap the JSON inside ```json``` markdown.

Return exactly this structure:

[
  {{
    "id": "EC-01",
    "title": "Sensor offline",
    "description": "Verify the dashboard displays '--' instead of crashing."
  }}
]

Return exactly 6 objects.

No extra text.
No explanations.
No markdown.
"""

        prompt = LIVE_EDGE_CASE_PROMPT

        response = llm.invoke(prompt)

        text = (
            response.content
            if hasattr(response, "content")
            else str(response)
        ).strip()

        logger.debug("Raw LLM response:\n%s", text)

        # Remove markdown code fences if present
        if text.startswith("```"):
            text = (
                text.replace("```json", "")
                .replace("```JSON", "")
                .replace("```", "")
                .strip()
            )

        logger.debug("Cleaned LLM response:\n%s", text)

        try:

            edge_cases = json.loads(text)

            if not isinstance(edge_cases, list):
                raise ValueError("Expected a JSON array.")

            cleaned = []

            for case in edge_cases:

                if not isinstance(case, dict):
                    continue

                cleaned.append(
                    {
                        "id": case.get("id", ""),
                        "title": case.get("title", ""),
                        "description": case.get("description", ""),
                    }
                )

            edge_cases = cleaned[:6]

        except Exception:

            logger.warning(
                "LLM did not return valid JSON. Falling back to line parser."
            )

            edge_cases = []

            for i, line in enumerate(text.splitlines(), start=1):

                line = line.strip()

                if not line:
                    continue

                line = line.lstrip("-•0123456789. ").strip()

                if not line:
                    continue

                edge_cases.append(
                    {
                        "id": f"EC-{i:02d}",
                        "title": line,
                        "description": line,
                    }
                )

            edge_cases = edge_cases[:6]

        state["edge_cases"] = edge_cases

        print(f"[Edge Cases Agent] Generated {len(edge_cases)} edge cases.")

    except Exception as e:

        print("\n========== EDGE CASES ERROR ==========")
        traceback.print_exc()
        print("======================================\n")

        state["edge_cases"] = [
            {
                "id": "ERROR",
                "title": "Edge case generation failed",
                "description": str(e),
            }
        ]

    finally:

        tracker.stop(agents_completed=1)
        tracker.save("reports/per_agent_perf.json")

    return state
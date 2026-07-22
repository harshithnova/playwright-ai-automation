from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

from langgraph.graph import StateGraph, END

from agents.scraper_adapter import build_selectors_from_crawl
from agents.state import AgentState 
from agents.strategy_agent import strategy_agent
from agents.architecture_agent import architecture_agent
from agents.code_gen_agent import code_gen_agent
from agents.review_agent import review_agent
from agents.edge_cases_agent import edge_cases_agent
from agents.heal_agent import heal_agent

from scraper.scraper import main as scrape_website
from performance.engine import PerformanceTracker


# Maximum number of regeneration attempts
MAX_REGEN = 2
# ----------------------------------------------------
# Week 3 Live App Configuration
# ----------------------------------------------------

LIVE_URL = os.getenv(
    "TARGET_URL",
    "https://live.ideabytesiot.com/demolive"
)


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("strategy", strategy_agent)
    graph.add_node("architecture", architecture_agent)
    graph.add_node("code_gen", code_gen_agent)
    graph.add_node("review", review_agent)
    graph.add_node("edge_cases_agent", edge_cases_agent)
    graph.add_node("heal", heal_agent)

    graph.set_entry_point("strategy")
    
    graph.add_edge("edge_cases_agent", "heal")
    graph.add_edge("heal", END)

    graph.add_edge("strategy", "architecture")
    graph.add_edge("architecture", "code_gen")
    graph.add_edge("code_gen", "review")

    def route_after_review(state: AgentState) -> str:
        # NOTE: this function is a LangGraph conditional-edge router,
        # not a node — any mutation to `state` here is silently
        # discarded by LangGraph (verified empirically). It must stay
        # read-only. The real counter increment lives in
        # agents/code_gen_agent.py, which IS a node and whose returned
        # state actually persists.
        regen_count = state.get("regen_count", 0)
        print(f"[Graph] Current regen count: {regen_count}")

        if not state.get("needs_regen", False):
            return "edge_cases_agent"

        if regen_count >= MAX_REGEN:
            print("[Graph] Maximum regeneration attempts reached.")
            return "edge_cases_agent"

        print(f"[Graph] Regen attempt {regen_count + 1}/{MAX_REGEN}")
        print("[Graph] Routing back to code generation.")
        return "code_gen"

    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "code_gen": "code_gen",
            "edge_cases_agent": "edge_cases_agent",
        },
    )


    return graph.compile()


if __name__ == "__main__":

    Path("reports").mkdir(exist_ok=True)

    print("=" * 70)
    print("STEP 1: Running Website Scraper")
    print("=" * 70)

    crawl_data = scrape_website()

    if not crawl_data:
        print("[Main] No pages returned by crawler.")
        crawl_data = []

    selectors = build_selectors_from_crawl(crawl_data)
    
    if not selectors:
        print("[Main] Warning: No selectors found from crawler.")

    print(f"[Main] Pages crawled : {len(crawl_data)}")
    print(f"[Main] Selectors found: {len(selectors)}")

    print("\n" + "=" * 70)
    print("STEP 2: Starting LangGraph Workflow")
    print("=" * 70)

    app = build_graph()

    # ----------------------------------------------------
    # Initial LangGraph State
    # ----------------------------------------------------

    initial_state: AgentState = {

        # Pipeline State
        "needs_regen": False,
        "regen_count": 0,
        "review_history": [],
        "best_yaml": "",
        "best_code": "",
        "syntax_passed": False,
        "duplicate_generation": False,

        # Design Document
        "design_doc": f"""
Generate Playwright automation tests for the live IoT monitoring application.

Target URL:
{LIVE_URL}

Requirements:
- Login functionality
- Dashboard validation
- Navigation
- Reports module
- Alerts module
- Devices module
- Alarms module
- Users module
- Angular SPA handling
- Responsive UI
""",

        # Scraper Output
        "selectors": selectors,

        # Strategy Agent
        "task_plan": [],

        # Architecture Agent
        "architecture_notes": "",

        # Code Generation
        "generated_yaml": "",
        "yaml_validation": {},
        "generated_code": "",

        # Review Agent
        "review_notes": "",

        # Edge Cases Agent
        "edge_cases": [],
        
        
        
        # ----------------------------------------------------
        # Test Execution Feedback
        # ----------------------------------------------------

        "execution_stdout": "",
        "execution_return_code": 0,
        "execution_failures": [],
        
        # ----------------------------------------------------
        # Week 3 Live App Context
        # ----------------------------------------------------

        # Live application URL
        "target_url": LIVE_URL,

        # Authentication required (confirmed during Day 11)
        "auth_required": True,

        # Angular SPA hydration state
        "spa_hydrated": False,
    }

    tracker = PerformanceTracker(label="full_pipeline_run")
    tracker.start()

    RECURSION_LIMIT = (MAX_REGEN + 2) * 4  # hard safety net

    try:
        result = app.invoke(
            initial_state,
            config={"recursion_limit": RECURSION_LIMIT},
        )
        
        if not result:
            raise RuntimeError("LangGraph returned an empty state.")
    except Exception as e:
        tracker.stop(agents_completed=0)
        print(f"[Main] Graph execution stopped: {e}")
        raise

    metrics = tracker.stop(agents_completed=6)

    tracker.save("reports/perf_baseline.json")

    generated_test = Path("generated_tests/generated_test.py")
    generated_yaml = Path("generated_tests/generated_yaml.yaml")

    if generated_test.exists():
        print(f"[Main] Generated test: {generated_test}")

    if generated_yaml.exists():
        print(f"[Main] Generated YAML: {generated_yaml}")
    
    print("\n" + "=" * 70)
    print("FINAL STATE")
    print("=" * 70)

    for key, value in result.items():
        print(f"\n{key}:")
        print(value)
        print("-" * 70)

    print("\nWorkflow completed successfully.")

    print("\n" + "=" * 70)
    print("PERFORMANCE REPORT")
    print("=" * 70)

    for key, value in metrics.items():
        print(f"{key}: {value}")
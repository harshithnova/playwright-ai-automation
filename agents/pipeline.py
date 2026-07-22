from agents.state import AgentState
from scraper.scraper import main as crawl_site
from agents.scraper_adapter import build_selectors_from_crawl


def run_agent_pipeline(
    design_doc: str,
    target_url: str = None,
    selectors: list = None,
) -> dict:
    """
    Unified entry point for the LangGraph agent pipeline.

    Can be called from:
    - FastAPI
    - CLI
    - main.py
    """

    # --------------------------------------------------
    # Run scraper if URL is provided
    # --------------------------------------------------

    if selectors is None and target_url:

        print(f"[Pipeline] Scraping {target_url}...")

        crawl_data = crawl_site(
            url=target_url,
            max_pages=5
        )

        if crawl_data:
            selectors = build_selectors_from_crawl(crawl_data)
        else:
            selectors = []

    elif selectors is None:

        selectors = []

    # --------------------------------------------------
    # Build LangGraph
    # --------------------------------------------------
    from main import build_graph

    app = build_graph()

    # --------------------------------------------------
    # Initial State
    # --------------------------------------------------

    initial_state: AgentState = {

        # Input
        "design_doc": design_doc,
        "selectors": selectors,

        # Strategy Agent
        "task_plan": [],

        # Architecture Agent
        "architecture_notes": "",

        # Code Generation Agent
        "generated_yaml": "",
        "yaml_validation": {},
        "generated_code": "",

        # Review Agent
        "review_notes": "",
        "needs_regen": False,
        "regen_count": 0,
        "review_history": [],
        "best_yaml": "",
        "best_code": "",
        "syntax_passed": False,

        # Edge Cases Agent
        "edge_cases": []
    }

    # --------------------------------------------------
    # Execute LangGraph
    # --------------------------------------------------

    from main import MAX_REGEN
    RECURSION_LIMIT = (MAX_REGEN + 2) * 4

    result = app.invoke(
        initial_state,
        config={"recursion_limit": RECURSION_LIMIT},
    )
    # --------------------------------------------------
    # Return useful outputs
    # --------------------------------------------------

    return {

        "task_plan": result.get("task_plan", []),

        "architecture_notes": result.get(
            "architecture_notes",
            ""
        ),

        "generated_yaml": result.get(
            "generated_yaml",
            ""
        ),

        "yaml_validation": result.get(
            "yaml_validation",
            {}
        ),

        "generated_code": result.get(
            "generated_code",
            ""
        ),

        "review_notes": result.get(
            "review_notes",
            ""
        ),

        "edge_cases": result.get(
            "edge_cases",
            []
        ),

        "selectors": result.get(
            "selectors",
            []
        )
    }
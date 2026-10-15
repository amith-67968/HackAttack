from app.core.config import Settings
from app.schemas.plan import ResearchPlan
from app.schemas.research import ResearchRequest


async def create_plan(settings: Settings, request: ResearchRequest) -> ResearchPlan:
    """Build a compact validated search plan without waiting for local LLM inference."""
    del settings  # Kept in the signature for compatibility with workflow callers.
    topic = request.query.strip()[:300]
    required_fields = request.options.required_fields or ["key facts relevant to the request"]
    query_parts = [request.query.strip()]
    if request.options.location:
        query_parts.append(request.options.location.strip())

    return ResearchPlan.model_validate({
        "topic": topic,
        "summary": f"Search public sources for: {topic}",
        "required_fields": required_fields,
        "actions": [{
            "action_id": "search_research_topic",
            "action_type": "web_search",
            "goal": f"Find public sources relevant to: {topic}"[:300],
            "query": " ".join(part for part in query_parts if part)[:500],
            "required_tool": "tavily_search",
            "expected_result": {
                "description": f"Relevant public sources for: {topic}"[:500],
                "required_fields": required_fields,
            },
            "verification_rule": "Keep each result linked to its source URL and verify extracted claims against source text.",
        }],
    })

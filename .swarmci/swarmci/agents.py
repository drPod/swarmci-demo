import json
from urllib.parse import urlparse

from openai import AsyncOpenAI

from swarmci.adapters.tracing import attributes, rename, summary, traced
from swarmci.config import settings
from swarmci.models import Action

DEFAULT_OBJECTIVES = [
    "Explore a different visible control. Open menus, inspect options, then return.",
    "Try changing a selection, cancelling, reopening, and using undo or redo.",
    "Try the issue sequence with a different order of operations. Check the expected behavior.",
    "Exercise keyboard navigation, Escape, Back, and reload after a change.",
]


@traced("Gemma: propose exploration objectives")
async def objectives(state, target, count=3, gemma=False):
    if not gemma:
        rename("Plan objectives · Built-in fallback")
        attributes(objectives_source="static")
        options = target.exploration_objectives or DEFAULT_OBJECTIVES
        # Rotate by depth so a broad target's later workflows receive turns too.
        offset = state.get("depth", 0) % len(options)
        options = (options[offset:] + options[:offset])[:count]
        summary(outputs={"objectives": options})
        return options
    rename("Gemma · Plan exploration objectives")
    prompt = {
        "objective": target.objective,
        "controls": state["controls"][:100],
        "count": count,
        "workflow_candidates": target.exploration_objectives,
    }
    async with AsyncOpenAI(
        base_url=settings.gemma_base_url,
        api_key=settings.gemma_api_key,
        timeout=45,
        max_retries=0,
    ) as client:
        response = await client.chat.completions.create(
            model=settings.gemma_model,
            messages=[
                {
                    "role": "system",
                    "content": "Generate diverse UI exploration objectives. Return only a JSON array of short strings. Use only visible controls. Do not instruct shell commands or external communication.",
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
            temperature=0.8,
            max_tokens=600,
        )
        content = (
            (response.choices[0].message.content or "")
            .strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )
        result = json.loads(content)
        if not isinstance(result, list) or not all(isinstance(v, str) and len(v) < 1200 for v in result):
            raise ValueError("Gemma returned invalid objectives")
        attributes(objectives_source="gemma", objectives_count=len(result[:count]))
        summary(outputs={"objectives": result[:count]})
        return result[:count]


def translate(raw, selectors):
    """Compile the supported Browser Use action subset into model-free replay primitives."""
    name, params = next(iter(raw.items()))
    selector = selectors.get(params.get("index"), "")
    if name == "click":
        if not selector and params.get("coordinate_x") is None:
            raise ValueError("Cannot record click: DOM selector unavailable")
        return Action(
            kind="click",
            selector=selector,
            x=params.get("coordinate_x"),
            y=params.get("coordinate_y"),
            label="Click " + (selector or "canvas"),
        )
    if name == "right_click_at":
        return Action(kind="click", x=params["x"], y=params["y"], button="right", label="Open context menu")
    if name == "drag_canvas":
        return Action(
            kind="drag",
            x=params["start_x"],
            y=params["start_y"],
            end_x=params["end_x"],
            end_y=params["end_y"],
            label="Drag on canvas",
        )
    if name == "upload_file":
        if not selector:
            raise ValueError("Cannot record upload without DOM selector")
        return Action(kind="upload", selector=selector, value=params["path"], label="Upload file")
    if name == "input":
        if not selector:
            raise ValueError("Cannot record input without DOM selector")
        return Action(
            kind="fill",
            selector=selector,
            value=params["text"],
            clear=params.get("clear", True),
            label="Fill field",
        )
    if name == "navigate" and not params.get("new_tab"):
        return Action(kind="goto", value=params["url"], label="Navigate")
    if name == "go_back":
        return Action(kind="back", label="Back")
    if name == "send_keys":
        return Action(kind="press", value=params["keys"], label=params["keys"])
    if name == "scroll":
        return Action(
            kind="scroll",
            selector=selector,
            y=(1 if params.get("down", True) else -1) * params.get("pages", 1) * 900,
            label="Scroll",
        )
    if name == "select_dropdown":
        return Action(kind="select", selector=selector, value=params["text"], label="Select option")
    if name == "wait":
        return Action(kind="wait", value=str(params.get("seconds", 1)), label="Wait")
    if name in (
        "done",
        "screenshot",
        "search_page",
        "find_elements",
        "get_dropdown_options",
        "dropdown_options",
        "extract",
        "report_ux_issue",
    ):
        return None
    raise ValueError(f"Action {name} needs a deterministic replay adapter before export")


@traced("Browser Use: explore UI", kind="agent")
async def explore_bu(
    session, target, objective, max_steps, on_action, folder, model_role="bu", on_candidate=None
):
    from browser_use import ActionResult, Agent, Browser, ChatOpenAI, Tools

    rename(f"02 · Explore UI · {'Gemma' if model_role == 'gemma' else 'Browser Use'}")
    attributes(
        model_role=model_role, model_name=settings.gemma_model if model_role == "gemma" else settings.bu_model
    )
    summary(
        inputs={
            "objective": objective,
            "step_limit": max_steps,
            "model": settings.gemma_model if model_role == "gemma" else settings.bu_model,
        }
    )
    llm = ChatOpenAI(
        model=settings.gemma_model if model_role == "gemma" else settings.bu_model,
        base_url=settings.gemma_base_url if model_role == "gemma" else settings.bu_base_url,
        api_key=settings.gemma_api_key if model_role == "gemma" else settings.bu_api_key,
        temperature=0.6,
        top_p=0.95,
        dont_force_structured_output=model_role != "gemma",
    )
    browser = Browser(
        cdp_url=session.cdp_url,
        keep_alive=True,
        allowed_domains=[urlparse(target.url).hostname, *target.allowed_domains],
    )
    tools = Tools(
        exclude_actions=[
            "evaluate",
            "write_file",
            "replace_file",
            "read_file",
            "search",
            "switch",
            "close",
            "save_as_pdf",
        ]
    )
    tools.set_coordinate_clicking(True)

    @tools.action("Right-click a visible point to open its context menu")
    async def right_click_at(x: int, y: int):
        await session.act(Action(kind="click", x=x, y=y, button="right"))
        return ActionResult(extracted_content="Opened context menu")

    @tools.action("Drag on the canvas from one point to another")
    async def drag_canvas(start_x: int, start_y: int, end_x: int, end_y: int):
        await session.act(Action(kind="drag", x=start_x, y=start_y, end_x=end_x, end_y=end_y))
        return ActionResult(extracted_content="Canvas drag completed")

    @tools.action(
        "Record a suspected broken UX flow with expected and observed behavior. Use only after observing a concrete failure, not for guesses or unfinished actions."
    )
    async def report_ux_issue(title: str, expected: str, observed: str):
        if on_candidate:
            await on_candidate(
                {"title": title[:200], "expected": expected[:2000], "observed": observed[:2000]}
            )
        return ActionResult(extracted_content="Recorded as a suspected issue for review, not a verified bug.")

    class TracedAgent(Agent):
        @traced("Browser Use: decide and interact")
        async def step(self, step_info=None):
            step_number = self.state.n_steps
            rename(f"Browser Use: step {step_number}")
            attributes(step=step_number)
            return await super().step(step_info)

    agent = TracedAgent(
        task=f"Test this application UI. Starting goal: {target.objective}\nContinuation: {objective}\nUse the current tab only. Perform one interaction at a time. Do not use evaluate, file tools, external websites, or new tabs. Use report_ux_issue if you observe a concrete broken workflow, then stop. You are testing UX, not security.",
        llm=llm,
        browser=browser,
        tools=tools,
        enable_signal_handler=False,
        directly_open_url=False,
        file_system_path=str(folder / "agent-files"),
        max_actions_per_step=1,
        use_judge=False,
        generate_gif=False,
    )
    selectors = {}

    @traced("Browser Use: observe controls")
    async def before(a):
        state = await a.browser_session.get_browser_state_summary()
        selectors.clear()
        for index, node in state.dom_state.selector_map.items():
            selectors[index] = "xpath=" + node.xpath

    @traced("Browser Use: record step")
    async def after(a):
        attributes(step=len(a.history.history))
        a.save_history(folder / "browser-use-history.json")
        if not a.history.history:
            return
        last = a.history.history[-1]
        if any(r.error for r in last.result):
            raise RuntimeError("Browser Use action failed: " + str([r.error for r in last.result if r.error]))
        if last.model_output:
            for raw in last.model_output.action:
                raw_action = raw.model_dump(exclude_none=True)
                params = next(iter(raw_action.values()))
                interacted = last.state.interacted_element or []
                element = interacted[0] if interacted else None
                if element and params.get("index") is not None:
                    selectors[params["index"]] = "xpath=" + element.x_path
                action = translate(raw_action, selectors)
                if action and element and element.ax_name:
                    action.label = action.kind.title() + " " + element.ax_name
                if action:
                    keep_going = await on_action(action)
                    if not keep_going:
                        a.stop()

    try:
        await agent.run(max_steps=max_steps, on_step_start=before, on_step_end=after)
    finally:
        a_history = folder / "browser-use-history.json"
        agent.save_history(a_history)
        await browser.stop()

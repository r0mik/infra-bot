"""The LangGraph agent: a ReAct loop with a per-question tool budget and short memory.

    START → agent ──tool calls──► tools ─┐
              ▲                          │
              └──────────────────────────┘
            agent ──no tool calls / budget used──► END

State is checkpointed per Telegram chat (thread_id), so follow-up questions see earlier turns.
Earlier turns are replayed to the model as question/answer text only (their tool calls and results
are dropped) to keep each request at a few thousand tokens; the current turn is replayed in full.
"""

from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from .prompts import STEP_LIMIT_NOTE


class State(MessagesState):
    steps: int  # model calls in the current question; the bot resets it to 0 for each new question


def context_window(messages: list[BaseMessage], history_turns: int) -> list[BaseMessage]:
    """Earlier turns as plain Q/A text (last `history_turns` pairs) + the current turn in full."""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    earlier = [m for m in messages[:last_human]
               if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls and m.content)]
    return [*earlier[-2 * history_turns:], *messages[last_human:]] if history_turns else messages[last_human:]


def build_graph(llm: BaseChatModel, tools: list[BaseTool], system_prompt: Callable[[], str], *,
                max_steps: int = 12, history_turns: int = 6, checkpointer: BaseCheckpointSaver | None = None):
    with_tools = llm.bind_tools(tools)
    # The last call still declares the tools (the transcript references them) but may not call them.
    final = llm.bind_tools(tools, tool_choice="none")

    def agent(state: State) -> dict:
        steps = state.get("steps", 0)
        messages = [SystemMessage(system_prompt()), *context_window(state["messages"], history_turns)]
        if steps >= max_steps:
            messages.append(SystemMessage(STEP_LIMIT_NOTE))
            reply = final.invoke(messages)
            if reply.tool_calls:  # a model that ignores tool_choice must not loop forever
                reply = AIMessage(content=reply.content or "Не уложился в лимит шагов, ответа нет.", id=reply.id)
        else:
            reply = with_tools.invoke(messages)
        return {"messages": [reply], "steps": steps + 1}

    def route(state: State) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(State)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)

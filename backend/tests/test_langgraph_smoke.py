from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    trace: list[str]


def node_a(state: State) -> State:
    return {"trace": state["trace"] + ["a"]}


def node_b(state: State) -> State:
    return {"trace": state["trace"] + ["b"]}


def test_two_node_graph_without_checkpointer():
    g = StateGraph(State)
    g.add_node("a", node_a)
    g.add_node("b", node_b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    graph = g.compile()

    assert graph.checkpointer is None
    assert graph.invoke({"trace": []}) == {"trace": ["a", "b"]}

# Sent by the LLM side instead of ChatResponse when it's proposing an action
# that needs human approval before it's carried out. The UI shows this message
# with an Allow button; approving it publishes a matching CtrlCommand.
string id             # same id as the ChatRequest this answers
string text           # human-readable description of the proposed action (shown in the UI)
string resolved_text  # same proposal with map-point labels (e.g. go('A')) resolved to
                       # absolute coordinates -- what bridge_node publishes as
                       # CtrlCommand.text on Allow, instead of the label form shown above.
                       # Identical to text when there's nothing to resolve; empty is
                       # treated as "same as text" by bridge_node.

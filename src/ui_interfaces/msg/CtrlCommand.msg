# Published by the UI side when the user clicks Allow on a CtrlResponse.
# Consumed by the simulation/execution side (not the LLM side).
string id      # same id as the CtrlResponse that was approved
string text    # the approved action's text, echoed back for convenience

# Sent by the UI side (llm_ui) to the LLM side, one per user chat message.
#
# checked_pos_list is empty when the user sent plain chat (no points marked on
# the map). base_pose / manipulator_ee_pos are populated whenever available;
# they are meaningless when checked_pos_list is empty.
string id                        # unique id, echoed back in the matching ChatResponse
string text                      # user's message text

BasePose base_pose               # robot base pose at request time (placeholder until wired to a real topic)
EEPose manipulator_ee_pos        # manipulator end-effector pose at request time (placeholder until wired to a real topic)
NamedPoint[] checked_pos_list    # points marked on the map, in click order (A, B, C, ...); empty for plain chat

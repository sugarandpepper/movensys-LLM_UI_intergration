"""Real LLM-side ROS2 node -- backed by movensys/Mobile_LLM's chat_v2 model.

Implements the ui_interfaces contract, same role as llm_mock_node:
  subscribes to: /llm_ui/chat_request  (ChatRequest)
  publishes to:  /llm_ui/chat_response (ChatResponse)  -- plain chat reply
                 /llm_ui/ctrl_response (CtrlResponse)   -- proposed action, needs UI Allow

Model/prompt logic is lifted from Mobile_LLM/model/chat_v2.py:
  - classify_input / general_chat: Ollama qwen2.5:7b (unchanged)
  - convert_to_commands: Qwen2.5-7B-Instruct + LoRA (qwen-robot-lora-v2) via
    transformers/peft/bitsandbytes (unchanged)
  - resolve_move_base / resolve_move_ee: body-frame delta -> absolute coords
    (unchanged math), but reworked to take pose directly from the incoming
    ChatRequest (msg.base_pose / msg.manipulator_ee_pos) instead of chat_v2's
    separate RobotStateCache subscribed to /robot/base_pose, /robot/ee_pose --
    llm_ui's bridge_node already stamps every ChatRequest with the latest
    known pose, so a second pose channel isn't needed here.

Follow-up ("modify") requests: chat_v2.py's CLI has a review loop where
`commands` is a local variable that survives across turns, so a non-y/n
reply is unambiguously "edit the pending proposal". There's no equivalent
notion of "pending" in the topic-based protocol -- every ChatRequest is
independent -- so this node keeps its own `_last_raw_commands` (the raw,
pre-resolution model output of the last proposal) and asks a 3-way
classifier (chat / robot / modify) instead of the original 2-way one
whenever a proposal is pending. A 'modify' hit re-prompts the model with
MODIFY_SYSTEM_PROMPT + the remembered commands, same shape as
chat_v2.py's modify_commands() and dataset_v2_modify.jsonl.

This node also subscribes to /llm_ui/ctrl_command purely to notice when its
own last proposal was approved (by matching id) and drop it from memory --
it does not act on the command itself; executing it is still the
simulation/execution side's job, same as documented in llm_ui's README.
"""
import json
import math
import re

import ollama
import rclpy
import torch
from peft import PeftModel
from rclpy.node import Node
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from ui_interfaces.msg import (
    BasePose, ChatRequest, ChatResponse, CtrlCommand, CtrlResponse, EEPose, NamedPoint,
)

CHAT_REQUEST_TOPIC = '/llm_ui/chat_request'
CHAT_RESPONSE_TOPIC = '/llm_ui/chat_response'
CTRL_RESPONSE_TOPIC = '/llm_ui/ctrl_response'
CTRL_COMMAND_TOPIC = '/llm_ui/ctrl_command'

BASE_MODEL = 'Qwen/Qwen2.5-7B-Instruct'
LORA_PATH = 'sugarpepper99/qwen-robot-lora-v2-modify'

CLASSIFY_CHAT_MODEL = 'qwen2.5:7b'

SYSTEM_PROMPT = """당신은 모바일 매니퓰레이터 로봇 제어 AI입니다.
사용자의 한국어 명령을 아래 함수 목록만 사용해 JSON 배열로 변환하세요.
다른 텍스트는 절대 출력하지 마세요. 반드시 JSON 배열만 출력하세요.

사용 가능한 함수:
go(x, y, theta)                          # 모바일 베이스를 글로벌 좌표로 이동 (x, y: 미터, theta: 라디안)
go('zone')                               # 지정 구역으로 이동 ('A', 'B', 'C', 'D' 중 하나)
move_base(dx, dy, dyaw)                  # 모바일 베이스 소량 이동 (body 프레임 기준 delta)
move_ee(dx, dy, dz, dr, dp, dyaw)        # 매니퓰레이터 소량 이동 (로봇 베이스 프레임 기준 delta)
pick(object)                             # 지정 물체 집기
open_gripper()                           # 그리퍼 열기
detect(object)                           # 지정 물체 탐지

좌표 기준:
- go(x,y,theta): 글로벌 좌표계 기준 절대 좌표
- go('zone'): 구역 이름으로 이동, 반드시 따옴표 포함
- move_base: +dx=앞, +dy=왼쪽, +dyaw=반시계방향 (body 프레임 기준 delta, 단위: 미터/라디안)
- move_ee: +dx=오른쪽, +dy=앞, +dz=위 (로봇 베이스 프레임 기준 delta, 단위: 미터/라디안)
"""

MODIFY_SYSTEM_PROMPT = """당신은 로봇 명령어 수정 AI입니다.
현재 명령어 배열과 사용자의 수정 요청을 받아 수정된 JSON 배열만 출력하세요.
다른 텍스트는 절대 출력하지 마세요. 반드시 JSON 배열만 출력하세요.

사용 가능한 함수:
go(x, y, theta) 또는 go('zone')
move_base(dx, dy, dyaw)
move_ee(dx, dy, dz, dr, dp, dyaw)
pick(object)
open_gripper()
detect(object)
"""

CLASSIFY_SYSTEM_PROMPT = """
사용자 입력을 로봇 제어 요청인지 일반 대화인지 분류하세요.

판단 기준:
- 로봇에게 동작, 이동, 조작, 탐지, 정지, 취소를 요청하면 'robot'입니다.
- 질문, 설명 요청, 감상, 인사, 상태 확인, 불완전한 발화는 'chat'입니다.
- 한 입력에 대화와 로봇 명령이 함께 있으면 'robot'을 우선합니다.

'robot'으로 분류:
- 좌표나 구역으로 이동하라는 요청
- 베이스나 로봇 팔을 특정 방향, 거리, 각도로 움직이라는 요청
- 물체 탐지, 집기, 그리퍼 열기를 요청하는 표현
- "그만해", "멈춰", "잠깐 멈춰봐"처럼 로봇 동작 중단으로 해석 가능한 표현
- "춤춰줘", "사진 찍어줘", "노래 불러줘"처럼 지원 함수에 없더라도 로봇에게 동작을 요청하는 표현
- "고마워, 그런데 B구역으로도 가줘"처럼 일반 대화와 명령이 섞인 입력

'chat'으로 분류:
- "안녕", "고마워", "잘 작동하네" 같은 인사나 감상
- "로봇 팔이 신기하다", "그리퍼 소리가 시끄럽네" 같은 평가
- "지금 어디 있어?", "배터리 얼마나 남았어?", "그리퍼 열려 있어?" 같은 상태 질문
- "뭘 할 수 있어?", "이 명령이 무슨 뜻이야?" 같은 설명 요청
- "음... 그러니까...", "어... 잠깐"처럼 명확한 동작 의도가 없는 불완전한 발화

명확한 로봇 동작 의도가 없으면 안전을 위해 'chat'으로 분류하세요.
반드시 'robot' 또는 'chat' 중 하나만 출력하세요.
설명, 이유, 문장부호, 따옴표, 코드 블록, 추가 텍스트를 절대 출력하지 마세요.
"""


CLASSIFY_WITH_PENDING_SYSTEM_PROMPT = """
방금 로봇 명령어 목록을 제안했고, 현재 사용자의 승인을 기다리고 있습니다.
새 사용자 입력을 'modify', 'robot', 'chat' 중 하나로 분류하세요.

판단 기준:
- 방금 제안한 목록을 참조해 고치거나 다시 실행하려는 요청은 'modify'입니다.
- 기존 제안과 별개의 새로운 로봇 동작 요청은 'robot'입니다.
- 질문, 감상, 인사, 상태 확인, 불완전한 발화는 'chat'입니다.
- 일반 대화와 명령이 섞이면 'modify' 또는 'robot'을 우선합니다.

'modify'로 분류:
- 항목 추가, 삭제, 교체, 순서 변경, 거리나 각도 변경 요청
- "3번 지워줘", "첫 번째와 두 번째 순서를 바꿔", "마지막에 그리퍼 열기를 추가해"
- "그거 빼줘", "아까 그거 다시 해줘", "방금 말한 이동은 취소해"처럼 직전 제안을 가리키는 표현
- "조금 더 멀리 가게 바꿔줘", "회전 각도만 줄여주세요"처럼 번호 없이 값을 수정하는 표현
- "그만해", "취소해", "잠깐 멈춰"가 방금 제안한 명령이나 실행 절차를 취소하는 의미인 경우

'robot'으로 분류:
- "이번에는 B구역으로 가자", "새로 컵을 찾아서 집어줘"처럼 별개의 새 명령
- "춤춰줘", "사진 찍어줘"처럼 지원 함수에 없더라도 새로운 로봇 동작을 요청하는 표현
- "고마워, 그런데 오른쪽으로 1미터 가줘"처럼 대화와 새 명령이 섞인 입력

'chat'으로 분류:
- "고마워", "잘 작동하네", "로봇 팔이 신기하다" 같은 인사, 감상, 평가
- "지금 어디 있어?", "배터리 얼마나 남았어?", "이 명령이 무슨 뜻이야?" 같은 질문
- "음... 그러니까...", "어... 잠깐"처럼 명확한 수정이나 명령 의도가 없는 발화

직전 제안을 가리키는 표현이 있으면 새로운 명령보다 'modify'를 우선하세요.
반드시 'modify', 'robot', 'chat' 중 하나만 출력하세요.
설명, 이유, 문장부호, 따옴표, 코드 블록, 추가 텍스트를 절대 출력하지 마세요.
"""

GENERAL_CHAT_SYSTEM_PROMPT = """당신은 로봇 제어 시스템의 AI 어시스턴트입니다.
반드시 한국어로만 대답하세요. 영어나 중국어로 절대 답하지 마세요.
친절하게 대화하되, 로봇 제어와 관련된 도움을 제공하세요."""


# ===================== 모델 로드 =====================
def load_model():
    tokenizer = AutoTokenizer.from_pretrained(LORA_PATH, trust_remote_code=True)

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type='nf4',
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    base_model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map='auto',
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base_model, LORA_PATH)
    model.eval()
    return model, tokenizer


# ===================== 추론 =====================
def generate(model, tokenizer, system: str, user: str) -> str:
    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]
    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer(text, return_tensors='pt').to(model.device)
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=256,
            temperature=0.1,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id,
        )
    response = tokenizer.decode(
        outputs[0][inputs['input_ids'].shape[1]:],
        skip_special_tokens=True
    ).strip()
    return response


def parse_commands(raw: str) -> list | None:
    try:
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return None


# ===================== 좌표 해석 =====================
def resolve_move_base(cmd: str, base_pose: BasePose) -> str:
    """move_base(dx, dy, dyaw) body 프레임 delta -> 글로벌 절대 좌표로 변환."""
    m = re.search(r'move_base\(([^)]+)\)', cmd)
    if not m:
        return cmd

    vals = [float(v.strip()) for v in m.group(1).split(',')]
    if len(vals) != 3:
        return cmd

    dx, dy, dyaw = vals
    theta = base_pose.yaw

    global_dx = dx * math.cos(theta) - dy * math.sin(theta)
    global_dy = dx * math.sin(theta) + dy * math.cos(theta)

    target_x = base_pose.x + global_dx
    target_y = base_pose.y + global_dy
    target_theta = base_pose.yaw + dyaw

    return f'move_base({target_x:.4f}, {target_y:.4f}, {target_theta:.4f})'


def resolve_move_ee(cmd: str, ee_pose: EEPose) -> str:
    """move_ee(dx,dy,dz,dr,dp,dyaw) delta -> 현재 ee 상태에 더해 절대 좌표로 변환."""
    m = re.search(r'move_ee\(([^)]+)\)', cmd)
    if not m:
        return cmd

    vals = [float(v.strip()) for v in m.group(1).split(',')]
    if len(vals) != 6:
        return cmd

    dx, dy, dz, dr, dp, dyaw = vals

    return (f'move_ee('
            f'{ee_pose.x+dx:.4f}, {ee_pose.y+dy:.4f}, {ee_pose.z+dz:.4f}, '
            f'{ee_pose.roll+dr:.4f}, {ee_pose.pitch+dp:.4f}, {ee_pose.yaw+dyaw:.4f})')


def resolve_go_zone(cmd: str, checked_pos_list: list[NamedPoint]) -> str:
    """go('label') -> go(x, y, yaw) if label matches a point marked on the map.

    checked_pos_list points use the same A, B, C... labels as the mobile
    manipulator's predefined navigation zones (go('zone')). A marked point
    takes priority over the fixed zone when the label matches, since the
    user pointed at a specific spot on the map rather than asking for the
    general zone. If no marked point has that label, the command is left
    untouched and still resolves to the predefined zone downstream.
    """
    m = re.search(r"go\(\s*['\"]([^'\"]+)['\"]\s*\)", cmd)
    if not m:
        return cmd

    label = m.group(1)
    for p in checked_pos_list:
        if p.label == label:
            return f'go({p.x:.4f}, {p.y:.4f}, {p.yaw:.4f})'
    return cmd


def resolve_commands(commands: list, base_pose: BasePose, ee_pose: EEPose) -> list:
    """Resolve move_base/move_ee deltas to absolute coordinates.

    Deliberately leaves go('label') alone -- this is the "display" pass, used
    for the human-readable CtrlResponse.text the UI shows. Label -> coordinate
    resolution happens separately in resolve_go_labels(), for
    CtrlResponse.resolved_text (what actually gets executed on Allow).
    """
    resolved = []
    for cmd in commands:
        if cmd.startswith('move_base'):
            resolved.append(resolve_move_base(cmd, base_pose))
        elif cmd.startswith('move_ee'):
            resolved.append(resolve_move_ee(cmd, ee_pose))
        else:
            resolved.append(cmd)
    return resolved


def resolve_go_labels(commands: list, checked_pos_list: list[NamedPoint]) -> list:
    """Apply resolve_go_zone to every go(...) command in the list."""
    return [
        resolve_go_zone(cmd, checked_pos_list) if cmd.startswith('go') else cmd
        for cmd in commands
    ]


def format_commands(commands: list) -> str:
    return '다음 동작을 제안합니다:\n' + '\n'.join(f'{i}. {c}' for i, c in enumerate(commands, 1))


# ===================== 입력 분류 / 일반 대화 =====================
def classify_input(user_input: str, has_pending: bool) -> str:
    """Returns 'chat', 'robot', or ('modify', only possible if has_pending)."""
    system = CLASSIFY_WITH_PENDING_SYSTEM_PROMPT if has_pending else CLASSIFY_SYSTEM_PROMPT
    response = ollama.chat(
        model=CLASSIFY_CHAT_MODEL,
        messages=[
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user_input},
        ],
        options={'temperature': 0.1},
    )
    result = response['message']['content'].strip().lower()
    if has_pending and 'modify' in result:
        return 'modify'
    return 'robot' if 'robot' in result else 'chat'


def general_chat(user_input: str) -> str:
    response = ollama.chat(
        model=CLASSIFY_CHAT_MODEL,
        messages=[
            {'role': 'system', 'content': GENERAL_CHAT_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_input},
        ],
        options={'temperature': 0.7},
    )
    return response['message']['content'].strip()


class MobileLlmNode(Node):
    def __init__(self):
        super().__init__('mobile_llm_node')

        self.get_logger().info('모델 로드 중... (Qwen2.5-7B-Instruct + qwen-robot-lora-v2)')
        self.model, self.tokenizer = load_model()
        self.get_logger().info('모델 로드 완료')

        self._chat_pub = self.create_publisher(ChatResponse, CHAT_RESPONSE_TOPIC, 10)
        self._ctrl_pub = self.create_publisher(CtrlResponse, CTRL_RESPONSE_TOPIC, 10)
        self._chat_sub = self.create_subscription(
            ChatRequest, CHAT_REQUEST_TOPIC, self._on_chat_request, 10)
        self._ctrl_cmd_sub = self.create_subscription(
            CtrlCommand, CTRL_COMMAND_TOPIC, self._on_ctrl_command, 10)

        # Last proposal's raw (pre-resolution) commands + the request id it
        # was published under -- lets a follow-up "modify" message edit it.
        # None means no pending proposal. See module docstring.
        self._last_raw_commands: list | None = None
        self._last_ctrl_id: str | None = None

        self.get_logger().info(
            f'mobile_llm_node ready: {CHAT_REQUEST_TOPIC} -> '
            f'{CHAT_RESPONSE_TOPIC} (chat) / {CTRL_RESPONSE_TOPIC} (proposed actions)'
        )

    def _publish_chat(self, req_id: str, text: str) -> None:
        out = ChatResponse()
        out.id = req_id
        out.text = text
        self._chat_pub.publish(out)

    def _propose(self, msg: ChatRequest, raw_commands: list) -> None:
        """Resolve raw model output (deltas/labels) and publish it as a
        CtrlResponse, remembering the raw form so a later 'modify' request
        has something to edit -- the topic-based equivalent of chat_v2.py's
        CLI keeping `commands` alive across loop iterations.
        """
        # Display pass: move_base/move_ee resolved to absolute coordinates,
        # but go('A') stays as a label -- this is what the human sees/approves.
        commands = resolve_commands(raw_commands, msg.base_pose, msg.manipulator_ee_pos)

        # Execution pass: go('A') -> go(x, y, yaw) using the marked map point.
        # This is what actually gets published as CtrlCommand.text on Allow
        # (see bridge_node's get_ctrl_resolved_text) -- the human never sees
        # this form, only the label form above. Plain function calls, one per
        # line, no numbering/preamble -- this is for a downstream executor to
        # parse, not for display.
        resolved_commands = resolve_go_labels(commands, msg.checked_pos_list)

        out = CtrlResponse()
        out.id = msg.id
        out.text = format_commands(commands)
        out.resolved_text = '\n'.join(resolved_commands)
        self._ctrl_pub.publish(out)

        self._last_raw_commands = raw_commands
        self._last_ctrl_id = msg.id

    def _on_ctrl_command(self, msg: CtrlCommand) -> None:
        # The pending proposal was approved (Allow clicked) -- it's no longer
        # "current", so a later modify-looking message shouldn't edit it.
        # We don't act on the command itself; that's still the
        # simulation/execution side's job.
        if self._last_ctrl_id is not None and msg.id == self._last_ctrl_id:
            self._last_raw_commands = None
            self._last_ctrl_id = None

    def _on_chat_request(self, msg: ChatRequest) -> None:
        self.get_logger().info(f'request id={msg.id} text={msg.text!r}')

        has_pending = self._last_raw_commands is not None
        try:
            kind = classify_input(msg.text, has_pending)
        except Exception as e:
            self.get_logger().error(f'classify_input 실패: {e}')
            self._publish_chat(msg.id, f'(mobile_llm_node: 입력 분류 실패 -- {e})')
            return

        if kind == 'chat':
            try:
                reply = general_chat(msg.text)
            except Exception as e:
                self.get_logger().error(f'general_chat 실패: {e}')
                reply = f'(mobile_llm_node: 대화 생성 실패 -- {e})'
            self._publish_chat(msg.id, reply)
            return

        try:
            if kind == 'modify':
                user_msg = (
                    f'현재 명령어 배열:\n{json.dumps(self._last_raw_commands, ensure_ascii=False)}\n\n'
                    f'수정 요청: {msg.text}\n\n수정된 JSON 배열만 출력하세요.'
                )
                raw = generate(self.model, self.tokenizer, MODIFY_SYSTEM_PROMPT, user_msg)
            else:  # kind == 'robot'
                raw = generate(self.model, self.tokenizer, SYSTEM_PROMPT, msg.text)
            raw_commands = parse_commands(raw)
        except Exception as e:
            self.get_logger().error(f'명령어 {"수정" if kind == "modify" else "변환"} 실패: {e}')
            raw_commands = None

        if not raw_commands:
            self._publish_chat(msg.id, '명령어를 이해하지 못했습니다. 다시 말씀해 주세요.')
            return

        self._propose(msg, raw_commands)


def main():
    rclpy.init()
    node = MobileLlmNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

# LLM_UI

로봇을 명령하는 채팅 UI: 한쪽에는 맵 뷰어(클릭해서 지점 찍고, 드래그해서 방향 지정), 다른 쪽에는 LLM
채팅이 있고 둘 다 ROS2로 연결되어 있습니다. 토픽 단위 상세 설명은
[`src/llm_ui/README.md`](src/llm_ui/README.md)를 참고하세요; 이 파일은 "clone해서 바로 실행"만
다룹니다.

```
 browser (map + chat) <--WebSocket/HTTP--> bridge_node <--ROS2 topics--> LLM node / simulation
```

- [`src/llm_ui`](src/llm_ui) — UI 패키지: `bridge_node`(FastAPI/rclpy) + React 프론트엔드
- [`src/ui_interfaces`](src/ui_interfaces) — 공유 ROS2 메시지 규격
- [`src/mobile_llm_node`](src/mobile_llm_node) — 실제 LLM 노드 (Qwen2.5-7B-Instruct +
  qwen-robot-lora-v2-modify, 둘 다 실행 시 HuggingFace에서 다운로드), 동일한 `ui_interfaces`
  규격을 구현

## 사전 요구사항

- Ubuntu 22.04 + `/opt/ros/humble`에 설치된 **ROS2 Humble**
- 시스템 **Python 3.10** (ROS2 Humble의 `rclpy` 빌드와 버전 일치) -- conda/venv 불필요; 이 스크립트들은
  `/opt/ros/humble/setup.bash`를 직접 source 합니다
- **Node.js / npm** (프론트엔드 dev 서버)
- 로컬에서 실행 중인 **[Ollama](https://ollama.com)**, `qwen2.5:7b` pull 되어 있어야 함
  (`mobile_llm_node`가 채팅/명령 분류 및 일반 채팅 응답에 사용)
- `mobile_llm_node`용: CUDA GPU + torch/transformers/peft/bitsandbytes
  (`src/mobile_llm_node/requirements.txt` 참고)

## 최초 1회 설정

```bash
git clone git@github.com:sugarandpepper/movensys-LLM_UI_intergration.git LLM_UI
cd LLM_UI

source /opt/ros/humble/setup.bash

# ui_interfaces의 커스텀 메시지 생성에 colcon/rosidl이 필요로 하는 빌드 툴
pip install empy==3.3.4 lark catkin_pkg colcon-common-extensions numpy

# bridge_node 런타임 의존성 (FastAPI/WebSocket/맵 yaml 파싱)
pip install -r src/llm_ui/requirements.txt

# mobile_llm_node 런타임 의존성 (torch/transformers/peft/bitsandbytes/ollama)
pip install -r src/mobile_llm_node/requirements.txt

# 프론트엔드 의존성
npm install --prefix src/llm_ui/frontend

# mobile_llm_node가 분류/일반 채팅에 사용하는 모델 (실제 LLM 명령 처리는
# Qwen2.5-7B-Instruct + qwen-robot-lora-v2-modify LoRA를 거치며, Ollama가 아님)
ollama pull qwen2.5:7b

# 모든 ROS2 패키지 빌드 (ui_interfaces, llm_ui, mobile_llm_node)
colcon build
```

Python/메시지 파일을 건드리는 변경사항을 pull 받을 때마다 `colcon build`를 다시 실행하세요.

## 실행

저장소 루트에서 터미널 2개 필요:

```bash
# 터미널 1 -- UI 쪽: bridge_node + 프론트엔드 dev 서버를 함께 실행
./run.sh
```

`Uvicorn running`, `VITE ... ready`가 뜨면 **http://localhost:5173** 접속.

```bash
# 터미널 2 -- 실제 LLM 노드
./run_mobile_llm.sh
```

두 스크립트 모두 ROS2/워크스페이스를 자체적으로 source 하므로 수동 `source`는 필요 없습니다.
Ctrl-C를 누르면 각 터미널의 프로세스 트리가 종료됩니다.

세 번째 터미널에서 전체를 종료하려면 (예: 이전 실행에서 포트가 남아있는 경우):

```bash
./stop.sh
```

## 내 컴퓨터 밖으로 노출하기

`./run.sh`는 프론트엔드를 **vite dev 서버**로 실행합니다 -- localhost나 자신의 LAN에서는 괜찮지만,
신뢰할 수 없는 네트워크에 노출하기엔 적합하지 않습니다 (인증 없음, dev 전용이라 보안 강화 안 됨).
UI를 외부(어떤 네트워크든, 라우터 설정 불필요)로 노출하려면 대신 `./run.sh --external`을 사용하세요:

```bash
cp .env.external.example .env.external
# .env.external을 열어 LLM_UI_AUTH_USER / LLM_UI_AUTH_PASS에 실제 인증 정보 입력

./run.sh --external
```

명령 하나로 다 처리됩니다:
1. 프론트엔드 빌드 (`npm run build`).
2. `bridge_node`만 단독 실행하여 빌드된 정적 파일 + API + WebSocket을 포트 하나에서 서빙
   (기본 8080, `LLM_UI_EXTERNAL_PORT`로 변경 가능), `.env.external`의 인증 정보로 HTTP Basic Auth를
   걸어둠. 해당 파일이 없거나 변수 중 하나라도 비어있으면 실행을 거부함.
3. 처음 실행 시 `cloudflared`(root/패키지 매니저 불필요한 독립 바이너리)를 다운로드한 뒤,
   해당 포트로 Cloudflare quick tunnel을 열고 준비되면 공개 URL을 출력:

   ```
   ==================================================================
    External URL:  https://some-random-words.trycloudflare.com
    Login:         movensys / (password from .env.external)
   ==================================================================
   ```

어디서든 그 URL로 접속하면 브라우저가 세션당 한 번 Basic Auth 사용자명/비밀번호를 물어봅니다;
별도 로그인 페이지는 없습니다. Ctrl-C를 누르면 bridge_node와 터널이 함께 종료됩니다.

Quick tunnel은 Cloudflare 계정이 필요 없지만, 스크립트를 재시작할 때마다 URL이 바뀌고 Cloudflare가
가동시간을 보장하지 않습니다 -- 데모/테스트용으로는 괜찮지만 고정 주소가 필요한 용도에는 맞지 않습니다.
`.env.external`은 gitignore 처리되어 있습니다 -- 실제 인증 정보는 절대 커밋하지 마세요.

## ROS2로 실제 통신하는지 확인하기

**실행** 단계의 두 터미널이 모두 떠 있는 상태에서, 세 번째 터미널에서:

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 topic list
```

예상 출력:

```
/llm_ui/chat_request
/llm_ui/chat_response
/llm_ui/ctrl_command
/llm_ui/ctrl_response
/parameter_events
/robot_current_state
/rosout
```

이 토픽들은 모두 `bridge_node` 하나에서 나옵니다 (각 토픽의 publisher이자 subscriber가 모두
bridge_node) — 그래서 `run_mobile_llm.sh`가 떠 있든 아니든 이 목록은 동일하게 보입니다.
`/llm_ui/*`와 `/robot_current_state` 토픽이 아예 안 보인다면 `run.sh` 자체가 안 떠 있는 것입니다.
LLM 노드가 실제로 살아있는지 확인하려면 노드 목록을 확인하세요:

```bash
ros2 node list
```

```
/bridge_node
/mobile_llm_node
```

`/mobile_llm_node`가 안 보인다면 `run_mobile_llm.sh`가 실행되지 않았거나 크래시한 것입니다
(해당 터미널의 출력을 확인하세요).

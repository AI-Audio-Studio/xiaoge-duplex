# 10097 同版本部署与签收采证操作单

责任人：Deployment owner + Cloud owner + Test owner。

## 部署前硬条件

1. 已完成 clean checkout 采证。
2. 10097 部署 commit 与 clean checkout commit 完全一致。
3. 使用同一份依赖解析结果；不得在目标机执行手工 `uv pip install` 补包。
4. Security owner 已轮换旧 API Key，测试使用新 key，key 只通过交互式隐藏输入提供。
5. `XG_WEBPANEL_DEBUG_QUERY_TOKEN=0`、`XIAOGE_WEBPANEL_DEBUG_QUERY_TOKEN=0`。
6. `ROBOT_ACTION_ENABLED=0` 或等价真实动作 gate 关闭。
7. HTTPS/WSS 使用受信证书；内部测试入口暂未换证时，必须由 owner 预先确认并固定证书
   SHA-256，禁止关闭证书校验。

2026-08-10 预检查：当前部署目录为
`/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main-cont`；现网仍是 V4 前快照，
证书为无 SAN 的自签证书。未完成部署前不得执行最终签收；本轮 G3 可用下述固定证书指纹
方式采证，生产放行前仍须换成受信证书。

## 采证命令

以下路径按实际部署目录调整；输出目录不得位于源码目录。

```bash
set -euo pipefail
COMMIT='<40位commit>'
APP_DIR='/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main-cont'
EVIDENCE_DIR="/tmp/g3-signoff-${COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE_DIR"
cd "$APP_DIR"

git rev-parse HEAD | tee "$EVIDENCE_DIR/01_commit.txt"
test "$(git rev-parse HEAD)" = "$COMMIT"
git status --porcelain=v1 | tee "$EVIDENCE_DIR/02_git_status.txt"

uv sync --all-extras --dev 2>&1 | tee "$EVIDENCE_DIR/10_uv_sync.log"
uv run pytest \
  tests/test_ours_live_transcript.py \
  tests/test_ours_g2_r5_2_2_cloud_contract.py \
  tests/test_ours_g3_intent_command_rag.py \
  tests/test_ours_g3_ws_session_protocol.py \
  tests/test_ours_g3_x3_skill_commands.py \
  tests/test_ours_concurrency_m5_admin_routes.py \
  tests/test_ours_knowledge.py \
  tests/test_ours_music_player.py -q \
  2>&1 | tee "$EVIDENCE_DIR/20_pytest.log"

uv run ruff check \
  examples/voice_agents/common/g3_intent.py \
  examples/voice_agents/app/knowledge_index.py \
  examples/voice_agents/app/music_player.py \
  examples/voice_agents/app/music_tools.py \
  examples/voice_agents/app/online_interrupt_host.py \
  examples/voice_agents/app/session_state.py \
  examples/voice_agents/app/setup_taps.py \
  examples/voice_agents/app/web_audio.py \
  examples/voice_agents/gateway/config.py \
  examples/voice_agents/gateway/main.py \
  examples/voice_agents/gateway/proxy.py \
  examples/voice_agents/live_transcript.py \
  examples/voice_agents/web_ui_agent.py \
  examples/voice_agents/webpanel/command_lifecycle.py \
  examples/voice_agents/webpanel/state.py \
  examples/voice_agents/webpanel/server.py \
  examples/voice_agents/webpanel/bridge.py \
  tests/_g2_contract_r5_2_2.py \
  tests/test_ours_concurrency_m5_admin_routes.py \
  tests/test_ours_g2_r5_2_2_cloud_contract.py \
  tests/test_ours_g3_intent_command_rag.py \
  tests/test_ours_g3_ws_session_protocol.py \
  tests/test_ours_g3_x3_skill_commands.py \
  tests/test_ours_knowledge.py \
  tests/test_ours_live_transcript.py \
  tests/test_ours_music_player.py \
  2>&1 | tee "$EVIDENCE_DIR/30_ruff.log"

SMOKE_TOOL='docs/g3_solution_review_package_r2_r5_2_2_20260804/04_cloud_g3_application/cloud_g3_review_v4_correction_candidate/工具/g3_cloud_signoff_smoke.py'
uv run python "$SMOKE_TOOL" \
  --base-url https://60.205.197.165:10097 \
  --tls-cert-sha256 '<owner确认的64位证书SHA-256>' \
  --output "$EVIDENCE_DIR/40_smoke_summary.json"

find "$EVIDENCE_DIR" -type f ! -name 99_evidence_sha256.txt -print0 | sort -z | xargs -0 sha256sum \
  > "$EVIDENCE_DIR/99_evidence_sha256.txt"
```

受信域名入口应省略 `--tls-cert-sha256` 并使用系统 CA/hostname 校验。内部 IP 入口允许证书
pin，但指纹必须由 deployment/security owner 独立确认并写入回执。不得使用 `--insecure`、
`ssl=False` 或空校验上下文作为签收证据。

## 日志脱敏检查

对 gateway、反向代理和 WebPanel 的本次时间窗日志只做计数，不输出匹配内容：

```bash
python - <<'PY'
from pathlib import Path
import re

roots = [Path('/path/to/gateway/logs'), Path('/path/to/reverse-proxy/logs')]
pattern = re.compile(r'access_token=|Authorization:\s*Bearer\s+\S+', re.I)
count = 0
for root in roots:
    for path in root.glob('**/*'):
        if path.is_file():
            count += len(pattern.findall(path.read_text(errors='ignore')))
print(f'sensitive_access_log_hits={count}')
raise SystemExit(1 if count else 0)
PY
```

必须同时检查应用 gateway 和其前置反向代理；应用内 path-only logger 不能替代边缘代理检查。

## 回填

- 将证据目录脱敏后归档到 `证据/签收/10097_<commit12>_<utc>/`。
- Deployment owner 填写 source/manifest SHA-256 和 token 失效模板。
- Test owner 更新 `DEP-01`、`WS-01`、`WS-02`、`WS-03`、`CAPS-01`。

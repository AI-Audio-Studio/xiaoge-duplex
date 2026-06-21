import logging
import os

from dotenv import load_dotenv

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import anthropic

logger = logging.getLogger("kimi-console-agent")

load_dotenv()


class KimiAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "你是一个简洁、直接、可靠的中文助手。"
                "默认使用中文回答，除非用户明确要求其他语言。"
                "回答尽量短，先给结论，再补必要说明。"
            )
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="用中文做一句简短自我介绍，并请用户直接提问。")


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {
        "room_name": ctx.room.name,
        "provider": "anthropic-compatible",
        "model": os.environ.get("ANTHROPIC_MODEL", "kimi-k2.6"),
    }

    session = AgentSession(
        llm=anthropic.LLM(
            model=os.environ.get("ANTHROPIC_MODEL", "kimi-k2.6"),
            base_url=os.environ["ANTHROPIC_BASE_URL"],
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    )

    await session.start(agent=KimiAgent(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
